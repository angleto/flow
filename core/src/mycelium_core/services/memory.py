"""Hierarchical memory service (docs/adr/0005, 0007, 0016, FR-8).

Hard isolation: every query carries the mandatory (org_id, project_id)
predicate on top of RLS + partitioning, never relevance. Retrieval is
deterministic hybrid RRF (semantic cosine + lexical tsvector),
oversampled per branch, fused with k=60 and a stable tiebreak. Writing
an embedding is a metered cost operation (ADR-0019). The cold tier is
always queryable; the tier is a latency hint, never retention. GDPR
erasure cascades by provenance. Consolidation never crosses
org/project (ADR-0007).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import ColumnElement, delete, func, select, true, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.embed_dims import EMBED_DIM, EMBED_DIM_HOSTED
from mycelium_core.embedder import Embedder, EmbedResult, EmbedSide, get_embedder
from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.billing import CostBasis
from mycelium_core.models.identity import Identity, IdentityKind
from mycelium_core.models.membership import Role
from mycelium_core.models.memory_blob import BlobSource, MemoryBlob, MemoryBlobTag
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.services import audit, billing, taxonomy
from mycelium_core.services.chunker import Chunker
from mycelium_core.services.rbac import require_role

# The perimeter type lives in the retrieval layer, which is where it is
# carried (``RetrievalContext.project_id``) and where the predicate that
# reads it is built. Re-exported here because this module is the address
# every caller already knows it by, and because a caller naming a
# perimeter is asking this service a question, not the pipeline.
from mycelium_core.services.retrieval.types import (
    ANY_PROJECT as ANY_PROJECT,
)
from mycelium_core.services.retrieval.types import (
    ProjectScope,
    _AnyProject,
)

logger = logging.getLogger(__name__)

_RRF_K = 60
_OVERSAMPLE = 50
# Tiered fusion weights. The signals are ranked by precision: an EXACT
# term match (``simple`` FTS) is the strongest; a STEM-only match
# (``italian`` FTS) is fuzzy and over-matches proper nouns ('marzia' vs
# 'marzo'); a SEMANTIC neighbour is the weakest (bge-m3 packs unrelated
# same-language text into a high cosine band). Weighting them apart makes
# an exact hit always outrank a stem-only or semantic-only one under the
# otherwise rank-only RRF, so a keyword/name query is not flooded by
# fuzzy noise. A blob that matches several tiers sums their weights.
_LEXICAL_EXACT_WEIGHT = 1.0
_LEXICAL_STEM_WEIGHT = 0.2
_SEMANTIC_RRF_WEIGHT = 0.2
# Relative-score floor: after fusion, drop candidates below this fraction
# of the top score. A keyword query has a wide gap (lexical hits high,
# weighted-down semantic noise far below) so the noise is cut; a
# conceptual query has a flat all-semantic profile so nothing is cut
# (recall preserved). See RelativeFloorStage.
_RELATIVE_FLOOR_RATIO = 0.4
# Humus read-path (ADR-0034, task 06fbf2a7). Archived material that the
# decomposition pipeline flagged (``notes.humus_flag``) re-enters the
# focused walk as a PARALLEL source, late-fused with a SMALL boost and
# hard-capped so it cannot crowd out live notes.
#   * ``_HUMUS_RRF_BOOST`` (0.2): the branch weight (ADR: "a small
#     boost"). On the same precision tier as the semantic/stem signals,
#     NOT the exact tier (1.0): a humus atom gets a nudge above an
#     equivalent live note, but never overrides an exact lexical match,
#     and the fused scale stays low enough that the relative-floor
#     pruning of live results is unaffected. (The ADR's illustrative
#     "k=10 in RRF" assumed a standalone two-list fusion; this pipeline
#     fuses every branch at one k=_RRF_K, so the boost is the weight.)
#   * ``_HUMUS_FOCUSED_CAP`` (0.3): hard cap = 30% of the focused slots.
_HUMUS_RRF_BOOST = 0.2
_HUMUS_FOCUSED_CAP = 0.3

# Provenance-erase statement chunk (task c5da112c). Each (kind, id) pair
# compiles to 2 binds on asyncpg (hard cap 32767 args): 5000 pairs keeps
# a single statement an order of magnitude below the cap, so a large
# empty-trash / retention batch can never wedge on parameter count.
_ERASE_CHUNK = 5000
# Per-org key (Organization.settings JSONB) for the semantic-similarity
# floor applied in SemanticDenseStage. 0.0 = disabled (historical
# behaviour). Tuned live from the admin GUI; see services.retrieval.
SEMANTIC_MIN_SIM_KEY = "retrieval_semantic_min_similarity"
# Per-org key for the grader/abstain floor applied in GraderMinStage to
# the fused RRF score (WS-B1). When the top hit's fused score falls below
# it the search abstains ([]) instead of returning the first weak hit --
# "decide what to ignore like a person". 0.0 = disabled (historical
# behaviour). Sequenced after WS-A: a floor on pure-lexical scores would
# suppress real hits, so it only earns its keep once the dense tier is
# populated. Tuned live from the admin GUI like the semantic floor.
GRADER_MIN_RRF_KEY = "retrieval_grader_min_rrf"
# The grader floor is compared against the FUSED RRF score, whose achievable
# magnitude is tiny: with k=60 and the default weights (exact 1.0 + four 0.2
# branches) the best possible fused score is ~1.8/61 ~= 0.03. A [0,1] domain
# (correct for the cosine semantic floor) is a footgun here -- e.g. 0.5 would
# make EVERY query abstain. Clamp the grader floor to its real domain instead.
GRADER_MIN_RRF_MAX = 0.05


async def _org_setting_float(
    session: AsyncSession, org_id: uuid.UUID, key: str, *, hi: float = 1.0
) -> float | None:
    """Read a numeric per-org setting from the workspace settings bag,
    clamped to [0, ``hi``]. Returns None when absent / malformed / <= 0 (the
    gate-off sentinel shared by the retrieval floors)."""
    from mycelium_core.models.organization import Organization

    raw = (
        await session.execute(select(Organization.settings).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if not isinstance(raw, dict):
        return None
    try:
        val = float(raw.get(key, 0.0))
    except (TypeError, ValueError):
        return None
    val = max(0.0, min(hi, val))
    return val if val > 0.0 else None


async def semantic_min_similarity(session: AsyncSession, org_id: uuid.UUID) -> float:
    """Read the per-org semantic-similarity floor from the workspace
    settings bag, clamped to [0, 1]. Absent / malformed -> 0.0 (gate
    off)."""
    return await _org_setting_float(session, org_id, SEMANTIC_MIN_SIM_KEY) or 0.0


async def grader_min_rrf_floor(session: AsyncSession, org_id: uuid.UUID) -> float | None:
    """Read the per-org grader/abstain floor (on the fused RRF score) from
    the workspace settings bag, clamped to [0, ``GRADER_MIN_RRF_MAX``] (the
    metric's real domain; see the constant). Absent / malformed / <= 0 -> None
    (no abstain, historical behaviour)."""
    return await _org_setting_float(session, org_id, GRADER_MIN_RRF_KEY, hi=GRADER_MIN_RRF_MAX)


# Per-org key for the QUALITY grader/abstain floor keyed off the reranker's
# cross-encoder logit (task f0d24fdb / N3). The RRF grader floor above is
# rank-based and was MEASURED useless for honest abstention (note 3276b266
# §5): a floor on the fused score is a step function -- it encodes only
# cross-branch consensus, not relevance, so no value separates "answerable"
# from "not in memory". The cross-encoder logit DOES score query-doc
# relevance, so the honest abstain keys off it instead.
#
# STORED/COMPARED AS A PROBABILITY in [0, 1], NOT a raw logit (despite the
# ``_logit`` in the key, kept for continuity with the task): the top hit's
# logit is squashed through the sigmoid and the search abstains when that
# relevance probability is below the floor. Rationale: a 0..1 probability is
# interpretable in the admin GUI ("abstain below 0.5 relevance") and fits the
# shared ``_org_setting_float`` [0, 1] clamp, while sigmoid is monotone so a
# probability sweep is equivalent to a logit sweep. 0 / absent = off.
GRADER_MIN_RERANK_LOGIT_KEY = "retrieval_grader_min_rerank_logit"


async def grader_min_rerank_logit_floor(session: AsyncSession, org_id: uuid.UUID) -> float | None:
    """Read the per-org reranker-logit quality floor as a PROBABILITY in
    [0, 1] (see GRADER_MIN_RERANK_LOGIT_KEY). Absent / malformed / <= 0 ->
    None (no abstain). Only bites when the reranker actually ran (see
    GraderMinStage): the logit exists only then."""
    return await _org_setting_float(session, org_id, GRADER_MIN_RERANK_LOGIT_KEY)


# Sentinel model id recorded on a blob written while the embedder is
# unavailable (missing optional extra / load failure): the row is kept
# valid and FTS-searchable, just without a semantic vector. The SPA
# uses this to show "keyword-only" provenance (see /memory/status).
_NO_EMBED_MODEL = "none"


@dataclass(frozen=True)
class Hit:
    blob: MemoryBlob
    rrf: float
    # Winning chunk index when the source is multi-chunk (paragraph-split
    # via ParagraphChunker); 0 for whole-doc / single-vector blobs. The
    # SPA uses this to scroll to the matching paragraph of a long note.
    chunk_index: int = 0
    # ts_headline snippet over the chunk text, populated only when the
    # source is multi-chunk (whole-doc blobs already have a usable
    # preview from blob.summary / blob.text head). ``None`` means
    # "no targeted snippet, fall back to whatever the caller renders".
    chunk_snippet: str | None = None
    # Provenance marker (ADR-0034): "humus" when the hit was surfaced via
    # the parallel humus source (archived material), else None. Drives
    # the leaf icon + the "from archived material" footer in the SPA.
    provenance: str | None = None
    # Per-stage scores carried from the winning Candidate (lexical_exact /
    # lexical_stem / semantic / semantic_hosted / humus / rrf / rerank): lets
    # an MCP caller SEE why a hit ranked where it did, not just the final
    # score. Empty dict for callers that don't surface it.
    scores_by_stage: dict[str, float] = field(default_factory=dict)


# Sentinel model_id for a blob stored keyword-only (embed unavailable/failed):
# FTS covers it but it carries no dense vector. Mirrors task_search.
_KEYWORD_ONLY_MODEL = "none"


@dataclass(frozen=True)
class RetrievalMeta:
    """Why a retrieval returned what it did -- so a caller can tell a
    genuinely-empty result from silently-degraded recall (task 4f3c2207).
    The byte-identical empty response hid three caller-invisible collapses:
    an un-embeddable query, a per-org floor that rejected every neighbour
    (that mis-calibration shipped to prod once), and keyword-only blobs.

    - ``query_embedded``: the query produced an embedding (a dense tier
      ran). False => keyword-only by construction.
    - ``dense_branch_contributed``: the dense branch added >=1 candidate.
    - ``dense_rejected_by_floor``: kNN neighbours dropped by the per-org
      similarity floor (high + not contributed => floor mis-calibrated).
    - ``keyword_only_hits``: returned hits backed by a keyword-only blob.
    - ``abstained``: a grader/abstain floor dropped an otherwise-present top
      hit, so an EMPTY result means "no answer above the quality bar", not
      "nothing indexed". ``abstain_reason`` names the gate that fired (None
      otherwise): ``grader_min_rerank_logit`` (the quality floor on the
      cross-encoder logit, task f0d24fdb) or ``grader_min_rrf`` (the coarse
      RRF floor, WS-B1).
    - ``rerank_failed``: the cross-encoder reranker was requested but errored
      (model missing / OOM) and the pipeline degraded to RRF order. Results are
      still returned, but the precision uplift is absent -- surface it so the
      degradation is not silent (the same failure mode A1 exists to expose).
    """

    query_embedded: bool
    dense_branch_contributed: bool
    dense_rejected_by_floor: int
    keyword_only_hits: int
    abstained: bool = False
    abstain_reason: str | None = None
    rerank_failed: bool = False


async def _safe_embed(emb: Embedder, text: str, *, side: EmbedSide) -> EmbedResult | None:
    """Embed defensively. The local model depends on an optional extra
    (``sentence-transformers``); if it is missing or fails to load,
    ``embed`` raises (ImportError/RuntimeError/...). Memory must still
    work in keyword-only mode, so any failure (or an empty vector) is
    swallowed here and the caller degrades to FTS-only. Never raises
    because the embedder is unavailable.

    This helper serves both halves of the system -- the write that stores a
    blob and the search that looks for one -- so ``side`` is the caller's to
    state and cannot be defaulted here: defaulting it would silently make
    one of the two wrong for every instruction-tuned model."""
    try:
        result = await emb.embed(text, side=side)
    except Exception:
        # Optional dependency / model load is best-effort: any failure
        # degrades to keyword-only, never propagates to the caller. It is
        # LOGGED because the degradation is otherwise invisible from the
        # outside -- a workspace whose model is misconfigured (a refused
        # width, a checkpoint that will not load) writes blobs with no
        # vector and searches keyword-only, and every one of those looks
        # like an ordinary result. The blob id is not known yet; the
        # request's correlation id carries the attribution.
        logger.warning("embed failed; this write degrades to keyword-only", exc_info=True)
        return None
    if not result.vector:
        return None
    return result


async def _resolve_channel_tag_id(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    channel_tag_id: uuid.UUID | None,
    channel_key: str | None = None,
) -> uuid.UUID | None:
    """Resolve the optional memory channel for a write/search.

    Two ways to address a channel, both RLS-scoped to the tenant:

    - ``channel_tag_id``: an explicit tag id. It must be a tag visible
      in this tenant (RLS scopes ``tags`` to the org) AND of kind
      ``memory_channel`` (NotFound when absent, TAG_KIND_MISMATCH when
      the wrong kind -- behaviour preserved for existing callers).
    - ``channel_key``: the DETERMINISTIC stable slug an integration
      writes into. Resolved to the tenant's *enabled* ``memory_channel``
      tag with that ``system_key`` (CHANNEL_NOT_FOUND if absent or
      disabled, or it belongs to another org -- RLS makes it invisible).

    If BOTH are given they must resolve to the SAME tag, else a domain
    error (an integration must not be ambiguous about its target).
    Manual writes stay channel-OPTIONAL: both None -> None (no forced
    default)."""
    by_key: uuid.UUID | None = None
    if channel_key is not None:
        key_tag = await taxonomy.resolve_channel_by_key(
            session, org_id=org_id, channel_key=channel_key
        )
        by_key = key_tag.id
    if channel_tag_id is None:
        return by_key
    tag = (await session.execute(select(Tag).where(Tag.id == channel_tag_id))).scalar_one_or_none()
    if tag is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    if tag.kind is not TagKind.memory_channel:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    if by_key is not None and by_key != tag.id:
        # An explicit id and a key that point at different channels: the
        # caller is ambiguous about its target, refuse rather than guess
        # (docs/adr/0021: confirm, never guess).
        raise DomainError(MessageCode.DOMAIN_ERROR)
    return tag.id


def _project_pred(project_id: ProjectScope) -> ColumnElement[bool]:
    if isinstance(project_id, _AnyProject):
        # No perimeter clause at all. Org scoping is applied by the caller and
        # is what bounds the search; a project is a retrieval perimeter and
        # never an access boundary, so widening it here cannot surface a blob
        # the caller could not already read by id.
        return true()
    if project_id is None:
        return MemoryBlob.project_id.is_(None)
    return MemoryBlob.project_id == project_id


async def _visible_tag_ids(session: AsyncSession, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Subset of ``ids`` that are tags visible in the current tenant
    (RLS scopes ``tags`` to the org): guards explicit tags so a caller
    cannot link a blob to another workspace's tag."""
    if not ids:
        return set()
    rows = await session.execute(select(Tag.id).where(Tag.id.in_(list(ids))))
    return set(rows.scalars().all())


async def _inherited_tag_ids(
    session: AsyncSession, sources: Sequence[tuple[str, str]]
) -> set[uuid.UUID]:
    """Tags inherited from the provenance: a blob derived from tagged
    sources keeps their tags. Only kinds that actually carry tags in
    the schema contribute (today: ``task`` via ``task_tags``)."""
    task_ids: list[uuid.UUID] = []
    for kind, sid in sources:
        if kind != "task":
            continue
        try:
            task_ids.append(uuid.UUID(sid))
        except ValueError:
            continue
    if not task_ids:
        return set()
    rows = await session.execute(select(TaskTag.tag_id).where(TaskTag.task_id.in_(task_ids)))
    return set(rows.scalars().all())


async def _attach_blob_tags(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    blob_id: uuid.UUID,
    tag_ids: set[uuid.UUID],
) -> None:
    """Link a freshly written blob to a deduplicated tag set."""
    for tid in tag_ids:
        session.add(MemoryBlobTag(blob_id=blob_id, org_id=org_id, tag_id=tid))
    if tag_ids:
        await session.flush()


async def tags_by_blob(
    session: AsyncSession, *, blob_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Tag]]:
    """Batched blob -> tags (chips in search results, no N+1)."""
    out: dict[uuid.UUID, list[Tag]] = {}
    if not blob_ids:
        return out
    rows = await session.execute(
        select(MemoryBlobTag.blob_id, Tag)
        .join(Tag, Tag.id == MemoryBlobTag.tag_id)
        .where(MemoryBlobTag.blob_id.in_(list(blob_ids)))
    )
    for bid, tag in rows.all():
        out.setdefault(bid, []).append(tag)
    return out


async def write_blob(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_id: uuid.UUID | None,
    text_body: str,
    operation_id: str,
    namespace: str = "note",
    sources: Sequence[tuple[str, str]] = (),
    importance: Decimal = Decimal(0),
    tag_ids: Sequence[uuid.UUID] = (),
    channel_tag_id: uuid.UUID | None = None,
    channel_key: str | None = None,
    embedder: Embedder | None = None,
    chunker: Chunker | None = None,
    language: str | None = None,
    created_by_identity_id: uuid.UUID | None = None,
    origin_model_id: str | None = None,
) -> MemoryBlob:
    """Write a memory blob (or N blobs if the text is chunked).

    Long ``namespace='note'`` text is split into paragraph-sized
    chunks (task `bbc21aa1`); each chunk gets its own blob + own
    BlobSource(chunk_index=i). The function returns the FIRST chunk's
    blob -- callers that need every chunk can read them back via
    ``blob_sources.source_id``. Short text and other namespaces stay
    single-vector (WholeChunker), so the legacy contract is preserved.

    ``chunker`` is the explicit override: pass a Chunker instance to
    force a strategy regardless of the namespace/length heuristic.

    ``language`` is the explicit text-search config for the row's stemmed
    FTS (task b1baaf52); when omitted it is detected per chunk and falls
    back to 'simple' (no stemming) for short/ambiguous text.
    """
    from mycelium_core.services.chunker import pick_chunker
    from mycelium_core.services.fts_language import (
        detect_fts_language,
        normalize_fts_language,
    )

    await require_role(session, org_id, actor_id, Role.member)
    # Default the authoring identity to the actor's USER identity (migration
    # 0085). The MCP server overrides this with the ai_assistant identity when
    # the write came in through an agent token, so a blob's ``created_by`` is
    # the real author, not merely the token-owner. NULL only if the actor has
    # no materialised identity (legacy / backfill).
    if created_by_identity_id is None:
        created_by_identity_id = (
            await session.execute(
                select(Identity.id).where(
                    Identity.org_id == org_id,
                    Identity.user_id == actor_id,
                    Identity.kind == IdentityKind.user,
                )
            )
        ).scalar_one_or_none()
    channel_id = await _resolve_channel_tag_id(
        session, org_id=org_id, channel_tag_id=channel_tag_id, channel_key=channel_key
    )
    from mycelium_core.services.embedder_resolver import resolve_hosted_embedder

    emb = embedder or get_embedder()
    expected = EMBED_DIM
    expected_hosted = EMBED_DIM_HOSTED
    selected = chunker or pick_chunker(namespace=namespace, text=text_body)
    pieces = selected.chunks(text_body)
    # Cache tag computation outside the loop: explicit/channel/inherited
    # don't change per chunk so we compute them once.
    explicit = await _visible_tag_ids(session, tag_ids)
    inherited = await _inherited_tag_ids(session, sources)
    channel = {channel_id} if channel_id is not None else set()
    all_tags = explicit | inherited | channel

    # Optional HOSTED tier: when the org has a hosted embedder, every write
    # also populates embedding_hosted (metered on the org's basis) so search
    # fuses both tiers immediately. None => local-only writes.
    hosted = await resolve_hosted_embedder(session, org_id)
    hosted_emb = hosted[0] if hosted is not None else None
    hosted_basis = hosted[1] if hosted is not None else CostBasis.local

    first_blob: MemoryBlob | None = None
    for piece in pieces:
        result = await _safe_embed(emb, piece.text, side=EmbedSide.document)
        if result is not None and len(result.vector) != expected:
            raise DomainError(MessageCode.MEMORY_DIM_MISMATCH, expected=str(expected))
        if result is not None:
            await billing.meter_if_billable(
                session,
                org_id=org_id,
                actor_id=actor_id,
                operation_id=operation_id,
                op="embed",
                model_id=result.model_id,
                units_in=Decimal(result.tokens),
                basis=CostBasis.local,
            )
        # Hosted tier: populate embedding_hosted when the org has a hosted
        # embedder. A dim mismatch is a misconfiguration (the column type is
        # fixed); surface it. The local write above already succeeded so the
        # row stays searchable in keyword + local semantic regardless.
        result_hosted = (
            await _safe_embed(hosted_emb, piece.text, side=EmbedSide.document)
            if hosted_emb is not None
            else None
        )
        if result_hosted is not None and len(result_hosted.vector) != expected_hosted:
            raise DomainError(MessageCode.MEMORY_DIM_MISMATCH, expected=str(expected_hosted))
        if result_hosted is not None:
            await billing.meter_if_billable(
                session,
                org_id=org_id,
                actor_id=actor_id,
                operation_id=operation_id,
                op="embed_hosted",
                model_id=result_hosted.model_id,
                units_in=Decimal(result_hosted.tokens),
                basis=hosted_basis,
            )
        now = dt.datetime.now(tz=dt.UTC)
        fts_lang = (
            normalize_fts_language(language)
            if language is not None
            else detect_fts_language(piece.text)
        )
        blob = MemoryBlob(
            org_id=org_id,
            project_id=project_id,
            namespace=namespace,
            tier="hot",
            text=piece.text,
            fts_language=fts_lang,
            embedding=result.vector if result is not None else None,
            model_id=result.model_id if result is not None else _NO_EMBED_MODEL,
            dim=len(result.vector) if result is not None else expected,
            embedding_hosted=result_hosted.vector if result_hosted is not None else None,
            model_id_hosted=result_hosted.model_id if result_hosted is not None else None,
            dim_hosted=len(result_hosted.vector) if result_hosted is not None else None,
            access_count=1,
            last_accessed_at=now,
            importance=importance,
            access_score=importance,
            created_by=created_by_identity_id,
            origin_model_id=origin_model_id,
        )
        session.add(blob)
        await session.flush()
        for kind, sid in sources:
            session.add(
                BlobSource(
                    blob_id=blob.id,
                    org_id=org_id,
                    source_kind=kind,
                    source_id=sid,
                    chunk_index=piece.index,
                )
            )
        await session.flush()
        await _attach_blob_tags(session, org_id=org_id, blob_id=blob.id, tag_ids=all_tags)
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="memory_blob",
            entity_id=blob.id,
            action="write",
            diff={"chunk_index": str(piece.index)} if len(pieces) > 1 else None,
        )
        if first_blob is None:
            first_blob = blob
    if first_blob is None:
        # pick_chunker guarantees a non-empty list (WholeChunker
        # returns [Chunk(text=input, 0)] even for empty input), so
        # this branch is unreachable; raise rather than return a
        # spurious value so a future refactor can't silently corrupt
        # the contract.
        raise RuntimeError("chunker produced no chunks")
    return first_blob


async def retrieve_with_meta(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_id: ProjectScope,
    query: str,
    operation_id: str,
    limit: int = 10,
    grader_min_rrf: float | None = None,
    grader_min_rerank_logit: float | None = None,
    tag_ids: Sequence[uuid.UUID] | None = None,
    channel_tag_id: uuid.UUID | None = None,
    channel_key: str | None = None,
    created_by: uuid.UUID | None = None,
    embedder: Embedder | None = None,
    rerank: bool = False,
    humus: bool | None = None,
    humus_kinds: frozenset[str] | None = None,
    exclude_humus_from_base: bool = False,
    probe: bool = False,
    include_deleted_sources: bool = False,
    exact_only: bool = False,
) -> tuple[list[Hit], RetrievalMeta]:
    """Like :func:`retrieve` but also returns a :class:`RetrievalMeta` so the
    caller can distinguish 'nothing relevant' from 'dense recall silently
    collapsed to keyword-only' (task 4f3c2207). ``retrieve`` is the thin
    hits-only wrapper over this.

    ``probe`` marks measurement traffic (the eval harness): a probe run
    is never recorded in ``retrieval_trace`` (Fase 0, task 561c6aca), so
    eval sweeps cannot forge the search demand the graph aggregation
    reads. Everything else about the pipeline is identical.

    Humus knobs (the empirical-gate levers, task 4836a6cc; all default to the
    historical behaviour so an untouched caller is byte-identical):
    ``humus`` None -> ``settings.humus_enabled`` (default True); an explicit
    bool overrides per-call (like ``rerank``). ``humus_kinds`` restricts the
    humus BRANCH to given ``humus_kind`` values (None = all) -- branch
    attribution only, the atoms stay retrievable via the base branches. When
    ``exclude_humus_from_base`` the humus-atom blobs are removed from EVERY
    branch (Config C: atoms as if never written), used to prove a
    consolidation query is answered by no raw blob; it implies the humus
    branch off (the two knobs are mutually exclusive -- a branch over an
    excluded set could only be empty)."""
    await require_role(session, org_id, actor_id, Role.member)
    channel_id = await _resolve_channel_tag_id(
        session, org_id=org_id, channel_tag_id=channel_tag_id, channel_key=channel_key
    )
    from mycelium_core.services.embedder_resolver import resolve_hosted_embedder

    emb = embedder or get_embedder()
    # ``exact_only``: verbatim matching, nothing else. An entity-code lookup
    # (see ``lookup.looks_like_entity_code``) has no similarity question to
    # ask, so the query is never embedded -- no dense branch, no humus, no
    # cross-encoder, and no embed cost metered for a query that could only
    # have returned its arbitrary nearest neighbours.
    qres = None if exact_only else await _safe_embed(emb, query, side=EmbedSide.query)
    if qres is not None:
        # The query embedding is also a metered cost op. Skipped when
        # the embedder is unavailable: there is no semantic branch and
        # thus no embedding cost (keyword-only retrieval).
        await billing.meter_if_billable(
            session,
            org_id=org_id,
            actor_id=actor_id,
            operation_id=operation_id,
            op="embed_query",
            model_id=qres.model_id,
            units_in=Decimal(qres.tokens),
            basis=CostBasis.local,
        )
    # Optional hosted query embed for the dual-tier fused read: when the
    # org has a hosted embedder we embed the query with it too so
    # SemanticDenseStage hits both tiers in one pass. Metered on the
    # org's basis (our_key/byok), not local.
    hosted = await resolve_hosted_embedder(session, org_id)
    hosted_emb = hosted[0] if hosted is not None else None
    hosted_basis = hosted[1] if hosted is not None else CostBasis.local
    qres_hosted = (
        await _safe_embed(hosted_emb, query, side=EmbedSide.query)
        if hosted_emb is not None and not exact_only
        else None
    )
    if qres_hosted is not None:
        await billing.meter_if_billable(
            session,
            org_id=org_id,
            actor_id=actor_id,
            operation_id=operation_id,
            op="embed_query_hosted",
            model_id=qres_hosted.model_id,
            units_in=Decimal(qres_hosted.tokens),
            basis=hosted_basis,
        )

    # Build the per-call context (predicates pre-computed once) and run
    # the canonical retrieval pipeline. The pipeline is the extension
    # point: rerankers / HyDE / chunking-dedupe land as additional
    # stages without touching this function.
    from mycelium_core.config import get_settings as _get_settings
    from mycelium_core.services.retrieval import RetrievalContext, RetrievalPipeline
    from mycelium_core.services.retrieval.stages import (
        AccessCounterStage,
        CrossEncoderRerankerStage,
        DedupeBySourceStage,
        GraderMinStage,
        HumusCapStage,
        HumusStage,
        LexicalFTSStage,
        LimitStage,
        OrderingStage,
        RelativeFloorStage,
        RerankGate,
        RetrievalTraceStage,
        RRFFusionStage,
        SemanticDenseStage,
        humus_note_blob_exclusion,
        ineffective_source_blob_exclusion,
    )

    pred = _project_pred(project_id)
    # Tags are a facet *inside* the (org, project) boundary, never a
    # way past it: ANDed into both branches, never replacing the hard
    # predicate. A blob must carry every requested tag (faceted AND).
    # The optional memory channel is just one more required tag (it does
    # not get a special SQL path; it folds into the AND set).
    wanted = set(tag_ids or ())
    if channel_id is not None:
        wanted.add(channel_id)
    # The effective-source gate, folded into every retrieval stage at once
    # (the lexical/semantic stages fold ``tag_clauses`` into their WHERE, and
    # the humus stage inherits this base context): ADR-0043 D2 withholds
    # autonomously-generated, not-yet-approved notes (``review_state=
    # 'proposed'``), and task c5da112c withholds SOFT-DELETED sources -- a
    # trashed note/task must not keep surfacing through its index blobs
    # (restore brings them back with no re-index; hard deletion instead
    # erases the blobs by provenance, see ``erase_blobs_for_sources``).
    # ``include_deleted_sources`` is the unified /search "show the bin"
    # opt-in: it lifts only the soft-delete legs, never the proposed one.
    # Unconditional + a no-op when every source is effective.
    tag_clauses: tuple[ColumnElement[bool], ...] = (
        ineffective_source_blob_exclusion(org_id, include_deleted=include_deleted_sources),
    )
    # Config C of the humus A/B (task 4836a6cc): remove humus-atom blobs from
    # every branch (lexical/dense inherit tag_clauses, and the humus branch's
    # own predicate AND-ed with this NOT IN yields empty), so the pipeline
    # behaves as if the atoms were never written.
    if exclude_humus_from_base:
        tag_clauses = (*tag_clauses, humus_note_blob_exclusion(org_id))
    if wanted:
        tagged = (
            select(MemoryBlobTag.blob_id)
            .where(
                MemoryBlobTag.org_id == org_id,
                MemoryBlobTag.tag_id.in_(wanted),
            )
            .group_by(MemoryBlobTag.blob_id)
            .having(func.count(func.distinct(MemoryBlobTag.tag_id)) == len(wanted))
        )
        tag_clauses = (*tag_clauses, MemoryBlob.id.in_(tagged))
    if created_by is not None:
        # Provenance-filtered recall (migration 0085): fold the author predicate
        # into every stage's WHERE like the tag facets, so "recall only what
        # identity X wrote" holds across the lexical and semantic branches. A
        # first-class column filter, not the tag-lane convention it supersedes.
        tag_clauses = (*tag_clauses, MemoryBlob.created_by == created_by)

    ctx = RetrievalContext(
        session=session,
        org_id=org_id,
        actor_id=actor_id,
        project_id=project_id,
        operation_id=operation_id,
        embedder=emb,
        project_pred=pred,
        tag_clauses=tag_clauses,
        query_embedding=qres,
        extras={"query_embedding_hosted": qres_hosted} if qres_hosted is not None else {},
    )
    # Reranker stage is added between RRF and the final ordering only
    # when the caller asked for it (``rerank=True``) OR the workspace
    # has it enabled globally. The stage itself further gates on
    # query length and candidate count (see RerankGate); a gated-off
    # stage is a no-op so the pipeline cost is bounded by RRF.
    settings = _get_settings()
    use_rerank = (rerank or settings.reranker_enabled) and not exact_only
    # Humus branch on/off: an explicit ``humus=`` wins; else the workspace
    # default (``settings.humus_enabled``, historically True). When off, both
    # the humus source stage and its cap are dropped from the pipeline.
    # ``exclude_humus_from_base`` forces the branch off: the branch predicate
    # ANDs the base tag_clauses (which then contain the exclusion), so a
    # branch over an excluded set could only run empty -- short-circuit it.
    use_humus = settings.humus_enabled if humus is None else humus
    if exclude_humus_from_base or exact_only:
        # Humus is a dense branch; there is nothing for it to do without a
        # query embedding.
        use_humus = False
    sem_min_sim = await semantic_min_similarity(session, org_id)
    # The grader/abstain floor: an explicit caller value wins; otherwise
    # fall back to the per-org setting (WS-B1). Resolving it here means
    # every surface -- /search, MCP search, memory retrieve -- abstains
    # consistently without exposing a per-call knob.
    effective_grader_min = grader_min_rrf
    if effective_grader_min is None:
        effective_grader_min = await grader_min_rrf_floor(session, org_id)
    # The reranker-logit quality floor (task f0d24fdb): an explicit caller
    # value wins, else the per-org setting. Only bites when the reranker ran
    # (GraderMinStage); resolving it here keeps every surface consistent.
    effective_grader_min_rerank = grader_min_rerank_logit
    if effective_grader_min_rerank is None:
        effective_grader_min_rerank = await grader_min_rerank_logit_floor(session, org_id)
    from mycelium_core.services.retrieval.types import Stage as _Stage

    stages: list[_Stage] = [LexicalFTSStage(oversample=_OVERSAMPLE, exact_only=exact_only)]
    if not exact_only:
        stages.append(SemanticDenseStage(oversample=_OVERSAMPLE, min_similarity=sem_min_sim))
    if use_humus:
        # Parallel humus source (ADR-0034): archived material re-enters the
        # focused walk, late-fused below with a fixed boost + small k, then
        # hard-capped (HumusCapStage) so it never crowds out live notes.
        stages.append(
            HumusStage(oversample=_OVERSAMPLE, min_similarity=sem_min_sim, kinds=humus_kinds)
        )
    stages.append(
        RRFFusionStage(
            k=_RRF_K,
            weights={
                "lexical_exact": _LEXICAL_EXACT_WEIGHT,
                "lexical_stem": _LEXICAL_STEM_WEIGHT,
                "semantic": _SEMANTIC_RRF_WEIGHT,
                "semantic_hosted": _SEMANTIC_RRF_WEIGHT,
                # Harmless when the humus branch is off (no candidate carries a
                # ``humus`` stage score, so the weight applies to nothing).
                "humus": _HUMUS_RRF_BOOST,
            },
        )
    )
    if use_rerank:
        stages.append(
            CrossEncoderRerankerStage(
                gate=RerankGate(
                    min_query_tokens=settings.reranker_min_query_tokens,
                    min_candidates=settings.reranker_min_candidates,
                ),
            )
        )
    stages.extend(
        [
            OrderingStage(),
            # DedupeBySourceStage runs after ordering so the candidate
            # kept per source is the highest-scored chunk (the order is
            # already RRF/rerank-DESC by this point). It also runs
            # BEFORE Limit so the truncation is on unique sources, not
            # on chunks (otherwise top-10 could collapse to 3 sources
            # with 7 chunks of the same parent).
            DedupeBySourceStage(),
            # Cut the weighted-down semantic tail once the real (lexical)
            # hits have set the top score. No-op for flat all-semantic
            # (conceptual) queries, so recall there is unchanged.
            RelativeFloorStage(ratio=_RELATIVE_FLOOR_RATIO),
            GraderMinStage(
                min_score=effective_grader_min,
                min_rerank_prob=effective_grader_min_rerank,
            ),
        ]
    )
    if use_humus:
        # Hard cap on humus slots (ADR-0034): runs after ordering so the
        # kept humus are the most relevant; freed slots fall to live
        # candidates ranked just below, then LimitStage truncates.
        stages.append(HumusCapStage(ratio=_HUMUS_FOCUSED_CAP, limit=limit))
    stages.extend(
        [
            LimitStage(k=limit),
            AccessCounterStage(),
        ]
    )
    # Fase 0 (task 561c6aca): trace the served top-m for the offline
    # edge-usage aggregation. Mounted last so it records exactly what
    # the caller gets; never mounted for probe (eval) traffic so
    # measurement runs don't forge search demand.
    if settings.retrieval_trace_enabled and not probe:
        stages.append(RetrievalTraceStage())
    pipeline = RetrievalPipeline(stages=stages)
    top = await pipeline.run(query, ctx)

    def _meta(hits: list[Hit]) -> RetrievalMeta:
        # The semantic stage records its diagnostics into ctx.extras (the
        # designated per-call scratch slot); read them back here.
        diag = ctx.extras.get("semantic_diag") or {}
        abstained = bool(ctx.extras.get("grader_abstained"))
        # The stage records WHICH floor fired (rrf vs rerank-logit); fall back
        # to the RRF reason for the legacy path that didn't set it.
        reason = ctx.extras.get("grader_abstain_reason") or "grader_min_rrf"
        return RetrievalMeta(
            query_embedded=qres is not None or qres_hosted is not None,
            dense_branch_contributed=bool(diag.get("contributed", False)),
            dense_rejected_by_floor=int(diag.get("floor_rejected", 0)),
            keyword_only_hits=sum(
                1 for h in hits if (h.blob.model_id or _KEYWORD_ONLY_MODEL) == _KEYWORD_ONLY_MODEL
            ),
            abstained=abstained,
            abstain_reason=reason if abstained else None,
            rerank_failed=bool(ctx.extras.get("rerank_failed")),
        )

    if not top:
        # Empty is exactly the case the meta exists for: an empty result with
        # query_embedded=False (or dense_rejected_by_floor>0) means 'recall
        # was degraded', not 'nothing relevant'.
        return [], _meta([])
    # Load full blobs for the result (the pipeline carries only ids +
    # rank; the caller still expects ``Hit(blob=MemoryBlob, rrf=score)``).
    blobs = {
        b.id: b
        for b in (
            await session.execute(
                select(MemoryBlob).where(MemoryBlob.id.in_([c.blob_id for c in top]))
            )
        )
        .scalars()
        .all()
    }
    # Multi-chunk detection + targeted ts_headline (task d46833bb): a
    # source is "multi-chunk" when its BlobSource rows count > 1. For
    # those, run a single batched ts_headline over the WINNING chunk
    # text (blob.text already holds the chunk after ParagraphChunker)
    # so the SPA can render a snippet from the right paragraph. Whole-
    # doc / single-vector blobs skip the SQL entirely.
    multi_chunk_sources = await _multi_chunk_source_ids(
        session, sources=[(c.source_kind, c.source_id) for c in top]
    )
    snippet_blob_ids = [
        c.blob_id
        for c in top
        if c.blob_id in blobs and (c.source_kind, c.source_id) in multi_chunk_sources
    ]
    snippets = await _ts_headlines(session, blob_ids=snippet_blob_ids, query=query)
    hits = [
        Hit(
            blob=blobs[c.blob_id],
            rrf=c.score,
            chunk_index=c.chunk_index,
            chunk_snippet=snippets.get(c.blob_id),
            provenance=c.provenance,
            scores_by_stage=dict(c.scores_by_stage),
        )
        for c in top
        if c.blob_id in blobs
    ]
    return hits, _meta(hits)


async def retrieve(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_id: ProjectScope,
    query: str,
    operation_id: str,
    limit: int = 10,
    grader_min_rrf: float | None = None,
    grader_min_rerank_logit: float | None = None,
    tag_ids: Sequence[uuid.UUID] | None = None,
    channel_tag_id: uuid.UUID | None = None,
    channel_key: str | None = None,
    created_by: uuid.UUID | None = None,
    embedder: Embedder | None = None,
    rerank: bool = False,
    humus: bool | None = None,
    humus_kinds: frozenset[str] | None = None,
    exclude_humus_from_base: bool = False,
    probe: bool = False,
    include_deleted_sources: bool = False,
) -> list[Hit]:
    """Hybrid RRF retrieval -> the ranked hits. Thin wrapper over
    :func:`retrieve_with_meta` for the callers that don't need the recall
    diagnostics (the majority); the meta variant powers the MCP search
    tools. See :func:`retrieve_with_meta` for the humus knobs."""
    hits, _meta = await retrieve_with_meta(
        session,
        org_id=org_id,
        actor_id=actor_id,
        project_id=project_id,
        query=query,
        operation_id=operation_id,
        limit=limit,
        grader_min_rrf=grader_min_rrf,
        grader_min_rerank_logit=grader_min_rerank_logit,
        tag_ids=tag_ids,
        channel_tag_id=channel_tag_id,
        channel_key=channel_key,
        created_by=created_by,
        embedder=embedder,
        rerank=rerank,
        humus=humus,
        humus_kinds=humus_kinds,
        exclude_humus_from_base=exclude_humus_from_base,
        probe=probe,
        include_deleted_sources=include_deleted_sources,
    )
    return hits


async def _multi_chunk_source_ids(
    session: AsyncSession,
    *,
    sources: list[tuple[str | None, str | None]],
) -> set[tuple[str, str]]:
    """Return the set of ``(source_kind, source_id)`` that have more than
    one BlobSource row (i.e. were paragraph-split). One batched SELECT.
    ``None`` entries are dropped silently (legacy blobs without
    provenance)."""
    valid: list[tuple[str, str]] = [(k, s) for (k, s) in sources if k is not None and s is not None]
    if not valid:
        return set()
    from sqlalchemy import text as sa_text

    # Postgres tuple-IN works via ROW(...) IN (VALUES (...), ...). Using
    # parametrized binds keeps it injection-safe and lets the planner
    # treat the value list as a small constant.
    placeholders = ", ".join(f"(:k{i}, :s{i})" for i in range(len(valid)))
    params: dict[str, str] = {}
    for i, (k, s) in enumerate(valid):
        params[f"k{i}"] = k
        params[f"s{i}"] = s
    sql = sa_text(
        f"SELECT source_kind, source_id"  # noqa: S608 (placeholders are generated bind-param names)
        f"  FROM blob_sources"
        f" WHERE (source_kind, source_id) IN ({placeholders})"
        f" GROUP BY source_kind, source_id"
        f" HAVING COUNT(*) > 1"
    )
    rows = (await session.execute(sql, params)).all()
    return {(row.source_kind, row.source_id) for row in rows}


async def _ts_headlines(
    session: AsyncSession, *, blob_ids: list[uuid.UUID], query: str
) -> dict[uuid.UUID, str]:
    """Postgres-native snippet over ``memory_blobs.text``. Mirrors the
    helper in ``task_search`` so each service stays self-contained.
    Highlights in the row's OWN language (task b1baaf52) so a stemmed hit
    is actually emphasised (it: fatture->fattura), MaxFragments=1 /
    MaxWords=20 to fit the SPA inline preview."""
    if not blob_ids:
        return {}
    from sqlalchemy import text as sa_text

    sql = sa_text(
        "SELECT id, ts_headline(fts_language::regconfig, text,"
        " plainto_tsquery(fts_language::regconfig, :q),"
        " 'MaxFragments=1, MaxWords=20') AS snippet"
        " FROM memory_blobs"
        " WHERE id = ANY(:ids)"
    )
    rows = (await session.execute(sql, {"q": query, "ids": blob_ids})).all()
    return {row.id: row.snippet for row in rows}


async def erase_blobs_for_sources(
    session: AsyncSession,
    *,
    sources: Sequence[tuple[str, str]],
) -> int:
    """Bulk provenance erase: drop the ``blob_sources`` rows for every
    ``(source_kind, source_id)`` pair, then delete any blob left with no
    provenance at all. Returns the number of blobs deleted.

    The shared primitive behind :func:`gdpr_erase` AND the SOVEREIGN
    hard-delete path (``trash.empty_trash``, task c5da112c). Rationale:
    the index blobs are cleaned by ORM/mapper listeners on the ORM
    paths, but a bulk ``DELETE`` bypasses them -- without this call a
    hard-deleted note/task would leave its full-text blobs orphaned AND
    retrievable forever. Erasing by provenance mirrors ``gdpr_erase``:
    a derived memory whose ONLY provenance was a purged source dies with
    it. That is deliberate for the sovereign paths (a human explicitly
    destroying data) and exactly why the AUTONOMOUS retention sweep does
    NOT use whole-entity pairs (see ``entity_revisions.
    hard_delete_soft_deleted``: §12, the timer must not destroy
    independent memories that merely cite a purged row). Internal
    primitive: no RBAC and no audit here -- callers gate and log. RLS
    scopes every statement to the tenant. Statements are chunked so a
    large trash purge cannot exceed the driver's bind-parameter cap."""
    pairs = sorted({(k, s) for k, s in sources})
    if not pairs:
        return 0
    affected: set[uuid.UUID] = set()
    for i in range(0, len(pairs), _ERASE_CHUNK):
        chunk = pairs[i : i + _ERASE_CHUNK]
        pair_cond = tuple_(BlobSource.source_kind, BlobSource.source_id).in_(chunk)
        affected.update(
            (await session.execute(select(BlobSource.blob_id).where(pair_cond))).scalars().all()
        )
        await session.execute(delete(BlobSource).where(pair_cond))
    if not affected:
        return 0
    await session.flush()
    deleted = 0
    affected_ids = sorted(affected, key=str)
    for i in range(0, len(affected_ids), _ERASE_CHUNK):
        chunk_ids = affected_ids[i : i + _ERASE_CHUNK]
        orphaned = (
            (
                await session.execute(
                    select(MemoryBlob.id).where(
                        MemoryBlob.id.in_(chunk_ids),
                        ~select(BlobSource.blob_id)
                        .where(BlobSource.blob_id == MemoryBlob.id)
                        .exists(),
                    )
                )
            )
            .scalars()
            .all()
        )
        if orphaned:
            await session.execute(delete(MemoryBlob).where(MemoryBlob.id.in_(orphaned)))
            deleted += len(orphaned)
    await session.flush()
    return deleted


async def gdpr_erase(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_kind: str,
    source_id: str,
) -> int:
    """Remove the provenance link; delete any blob left with no
    provenance (cascading its embedding/sources/cluster membership)."""
    await require_role(session, org_id, actor_id, Role.member)
    deleted = await erase_blobs_for_sources(session, sources=[(source_kind, source_id)])
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=None,
        action="gdpr_erase",
        diff={"source": f"{source_kind}:{source_id}", "deleted": str(deleted)},
    )
    return deleted


async def recompute_tier(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    half_life_days: float = 30.0,
    hot_threshold: float = 5.0,
    warm_threshold: float = 1.0,
    now: dt.datetime | None = None,
) -> dict[str, int]:
    """Deterministic tier from a decayed access score + importance.
    Never deletes: a rare blob is demoted to cold, still queryable
    (ADR-0016 invariant)."""
    ref = now or dt.datetime.now(tz=dt.UTC)
    blobs = list(
        (await session.execute(select(MemoryBlob).where(MemoryBlob.org_id == org_id)))
        .scalars()
        .all()
    )
    counts = {"hot": 0, "warm": 0, "cold": 0}
    for b in blobs:
        age_days = 0.0
        if b.last_accessed_at is not None:
            age_days = (ref - b.last_accessed_at).total_seconds() / 86400.0
        decay = 0.5 ** (age_days / half_life_days)
        score = float(b.importance) + float(b.access_count) * decay
        tier = "hot" if score >= hot_threshold else "warm" if score >= warm_threshold else "cold"
        b.tier = tier
        b.access_score = Decimal(str(round(score, 6)))
        counts[tier] += 1
    await session.flush()
    return counts


async def consolidate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_id: uuid.UUID | None,
    blob_ids: Sequence[uuid.UUID],
    embedder: Embedder | None = None,
    operation_id: str,
) -> MemoryBlob:
    """Deterministically merge same-(org, project) blobs into one
    concept, preserving every source's provenance. Never crosses
    org/project (ADR-0007). LLM summarization is an optional later
    refinement; v1 concatenates deterministically."""
    await require_role(session, org_id, actor_id, Role.member)
    members = list(
        (await session.execute(select(MemoryBlob).where(MemoryBlob.id.in_(list(blob_ids)))))
        .scalars()
        .all()
    )
    if not members:
        raise NotFoundError(MessageCode.MEMORY_NOT_FOUND)
    for m in members:
        if m.org_id != org_id or m.project_id != project_id:
            raise DomainError(MessageCode.MEMORY_CROSS_SUBJECT)
    cluster_id = uuid.uuid4()
    merged_text = "\n\n".join(sorted((m.text or "") for m in members))
    consolidated = await write_blob(
        session,
        org_id=org_id,
        actor_id=actor_id,
        project_id=project_id,
        text_body=merged_text,
        operation_id=operation_id,
        namespace="consolidated",
        embedder=embedder,
    )
    consolidated.cluster_id = cluster_id
    member_ids = [m.id for m in members]
    await session.execute(
        update(MemoryBlob).where(MemoryBlob.id.in_(member_ids)).values(cluster_id=cluster_id)
    )
    # Preserve provenance: copy every member source onto the concept.
    src_rows = (
        await session.execute(
            select(BlobSource.source_kind, BlobSource.source_id)
            .where(BlobSource.blob_id.in_(member_ids))
            .distinct()
        )
    ).all()
    for kind, sid in src_rows:
        session.add(
            BlobSource(
                blob_id=consolidated.id,
                org_id=org_id,
                source_kind=kind,
                source_id=sid,
            )
        )
    await session.flush()
    # The concept inherits the union of its members' tags (same spirit
    # as the provenance union above; deterministic, never cross-subject
    # since all members share (org, project)).
    member_tags = set(
        (
            await session.execute(
                select(MemoryBlobTag.tag_id).where(MemoryBlobTag.blob_id.in_(member_ids)).distinct()
            )
        )
        .scalars()
        .all()
    )
    await _attach_blob_tags(session, org_id=org_id, blob_id=consolidated.id, tag_ids=member_tags)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=consolidated.id,
        action="consolidate",
        diff={"members": str(len(members))},
    )
    return consolidated


async def get_blob(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    blob_id: uuid.UUID,
    include_ineffective_sources: bool = False,
) -> MemoryBlob:
    """One memory entry by id, with the SAME effective-source perimeter
    ``retrieve`` applies (task a186c989).

    ``ineffective_source_blob_exclusion`` used to be folded into the
    retrieval branches only, so a blob id captured from an earlier search
    kept serving the indexed text of a note that has since gone to the bin
    or is awaiting review -- the by-id read was the way around the
    perimeter, which is the same shape of hole the note-part surfaces had.

    ``include_ineffective_sources`` is for the callers that must reach a
    blob regardless of what its source is doing now, namely ``delete_blob``:
    withholding a blob from reading must never mean it can no longer be
    thrown away.
    """
    stmt = select(MemoryBlob).where(MemoryBlob.id == blob_id)
    if not include_ineffective_sources:
        # Local import for the same reason ``retrieve`` does it: the stage
        # package imports back into this module.
        from mycelium_core.services.retrieval.stages import (
            ineffective_source_blob_exclusion,
        )

        stmt = stmt.where(ineffective_source_blob_exclusion(org_id))
    b = (await session.execute(stmt)).scalar_one_or_none()
    if b is None:
        raise NotFoundError(MessageCode.MEMORY_NOT_FOUND)
    return b


async def delete_blob(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    blob_id: uuid.UUID,
) -> None:
    """Hard-delete one memory entry and everything that hangs off it.

    The blob is loaded org-scoped (RLS already constrains ``select`` to
    the tenant; a foreign / unknown id is MEMORY_NOT_FOUND -- same guard
    as ``get_blob``, so cross-org isolation is preserved). Deleting the
    ``memory_blobs`` row cascades by FK ON DELETE CASCADE to its
    ``blob_sources`` and ``memory_blob_tags`` (same composite-FK cascade
    ``gdpr_erase`` relies on); the embedding vector and the generated
    ``fts`` column live on the blob row itself and go with it. This is
    the user deleting a single entry directly (distinct from
    ``gdpr_erase``, which removes a provenance link and only deletes a
    blob left with no provenance)."""
    await require_role(session, org_id, actor_id, Role.member)
    # A blob whose source is trashed or awaiting review is hidden from
    # reading, never from deletion (task a186c989).
    blob = await get_blob(session, org_id=org_id, blob_id=blob_id, include_ineffective_sources=True)
    await session.execute(
        delete(MemoryBlob).where(MemoryBlob.id == blob.id, MemoryBlob.org_id == org_id)
    )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=blob_id,
        action="delete",
    )


async def rechunk_legacy_sources(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_kind: str = "note",
    batch_size: int = 50,
    dry_run: bool = False,
    operation_id: str | None = None,
) -> dict[str, int]:
    """Re-index legacy whole-doc sources through the paragraph chunker.

    Task 2149e753 (follow-up to bbc21aa1). At deploy time every note
    was indexed as a single vector (chunk_index=0). After the chunker
    landed only NEW writes are paragraph-split; the old long notes
    stay single-vector and so miss the precision gain. This admin
    path finds those sources, deletes their old blob, and re-writes
    the same text via ``write_blob`` -- which now picks
    ``ParagraphChunker`` for ``namespace='note'`` text above the
    threshold, fanning out one ``MemoryBlob`` per paragraph.

    A source is a *rechunk candidate* iff every BlobSource row for
    it has ``chunk_index=0`` (so it was never chunked) AND its blob
    text exceeds the chunker threshold (otherwise the chunker would
    still pick ``WholeChunker``, so re-indexing changes nothing).

    Idempotent (sources whose blobs are already chunked are skipped
    by the SELECT). Each source is rewritten in the same transaction
    that loaded it: the old row's tags / project / importance are
    preserved on the new chunk-blobs.

    ``dry_run=True`` returns the candidate count without touching any
    rows -- useful to scope a rollout. ``batch_size`` caps the work
    per call (default 50): if ``rechunked == batch_size`` the caller
    should re-invoke until it drops below.
    """
    from mycelium_core.services.chunker import (
        approx_tokens,
        get_chunk_threshold_tokens,
    )

    # No service-level role gate: the only caller is the admin endpoint
    # ``/memory/rechunk`` which already enforces ``tenant_admin_ctx``
    # (platform admin capability + active X-Admin-Mode elevation). The
    # session is still RLS-scoped to the tenant org, so the SELECT and
    # mutations below can only see the caller's workspace.

    # Find candidate (source_kind, source_id) groups: every BlobSource
    # row for the source has chunk_index = 0 (NOT yet chunked). The
    # HAVING bool_or trick keeps it to a single batched pass.
    cand_rows = (
        await session.execute(
            select(
                BlobSource.source_kind,
                BlobSource.source_id,
            )
            .where(BlobSource.source_kind == source_kind)
            .group_by(BlobSource.source_kind, BlobSource.source_id)
            .having(func.bool_and(BlobSource.chunk_index == 0))
        )
    ).all()

    scanned = 0
    rechunked = 0
    skipped_short = 0
    for kind, sid in cand_rows:
        # Cap the work per call so the endpoint stays bounded.
        if rechunked >= batch_size:
            break
        # Load the legacy blob behind this source (there is exactly
        # one when every chunk_index is 0; defensively pick the
        # first if multiple rows pointed at the same blob).
        bs_row = (
            (
                await session.execute(
                    select(BlobSource).where(
                        BlobSource.source_kind == kind,
                        BlobSource.source_id == sid,
                    )
                )
            )
            .scalars()
            .first()
        )
        if bs_row is None:
            continue
        blob = (
            await session.execute(select(MemoryBlob).where(MemoryBlob.id == bs_row.blob_id))
        ).scalar_one_or_none()
        if blob is None:
            continue
        scanned += 1
        # Skip blobs with no text (defensive; the model column is
        # NOT NULL but the type hint admits Optional).
        original_text = blob.text or ""
        if not original_text:
            continue
        # Skip blobs the chunker would still leave whole (paragraph
        # split is only useful above the threshold). Counted separately
        # so callers can see why the candidate list shrank.
        if approx_tokens(original_text) < get_chunk_threshold_tokens():
            skipped_short += 1
            continue
        if dry_run:
            rechunked += 1
            continue
        # Snapshot the row's per-source metadata before deletion.
        original_project_id = blob.project_id
        original_importance = blob.importance
        original_namespace = blob.namespace
        original_tag_ids = [
            row.tag_id
            for row in (
                await session.execute(
                    select(MemoryBlobTag.tag_id).where(MemoryBlobTag.blob_id == blob.id)
                )
            ).all()
        ]
        # Cascade-delete the legacy blob (FK ON DELETE CASCADE clears
        # blob_sources + memory_blob_tags + embedding vector + fts).
        await session.execute(
            delete(MemoryBlob).where(MemoryBlob.id == blob.id, MemoryBlob.org_id == org_id)
        )
        await session.flush()
        # Re-write the same text. ``write_blob`` re-runs the chunker;
        # the new BlobSource rows reuse ``(kind, sid)`` so any external
        # cross-link to the source survives the rechunk.
        await write_blob(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=original_project_id,
            text_body=original_text,
            operation_id=operation_id or f"rechunk-{sid}",
            namespace=original_namespace,
            sources=[(kind, sid)],
            importance=original_importance,
            tag_ids=original_tag_ids,
        )
        rechunked += 1

    return {
        "scanned": scanned,
        "rechunked": rechunked,
        "skipped_short": skipped_short,
        "batch_size": batch_size,
    }


async def attach_blob_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    blob_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    """Curate memory by hand / by the AI: add an explicit tag to an
    existing blob. Idempotent (re-adding is a no-op)."""
    await require_role(session, org_id, actor_id, Role.member)
    await get_blob(session, org_id=org_id, blob_id=blob_id)
    if not await _visible_tag_ids(session, [tag_id]):
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    try:
        async with session.begin_nested():
            session.add(MemoryBlobTag(blob_id=blob_id, org_id=org_id, tag_id=tag_id))
            await session.flush()
    except IntegrityError:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=blob_id,
        action="attach_tag",
    )


async def detach_blob_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    blob_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(MemoryBlobTag).where(
            MemoryBlobTag.blob_id == blob_id, MemoryBlobTag.tag_id == tag_id
        )
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="memory_blob",
        entity_id=blob_id,
        action="detach_tag",
    )
