"""Task search: keep one memory blob per task in sync with task content.

Tasks become first-class hits in the existing memory pipeline
(``memory_blobs``: FTS generated + pgvector + RRF) without a parallel
index. The binding is 1:1 via ``task_index_pointer``; the blob text is
``title || description || checklist joined`` (one blob per task --
checklist is NOT indexed per-item: a match brings you to the task, the
item is UX).

Mutation tracking
-----------------
Sync SQLAlchemy event listeners on ``Task`` and ``TaskChecklistItem``
(after_insert / after_update / after_delete) push the touched task ids
into ``session.info``. The async helper :func:`flush_task_search_dirty`
drains that set and UPSERTs the blob; it is called from ``db.tenant_session``
just before commit, so every mutation path that opens a tenant session
gets the resync for free (no per-service hook).

Why a separate async flush
~~~~~~~~~~~~~~~~~~~~~~~~~~
SQLAlchemy event listeners on the sync ``Session`` cannot ``await``;
the live connection here is asyncpg, so the resync (embedder call +
ORM I/O) must run on the async path. The listener does the cheap
"remember the id" step, the async flush does the I/O. The net effect
matches the design intent: declarative tracking (fires from every code
path), single async chokepoint, no bus factor in business services.

content_hash
------------
Stored on the pointer. INSERT path: write blob + pointer. UPDATE path:
recompute over the rendered text; unchanged -> skip (state/priority/due
mutations don't touch ``text``, so they cost zero embed). Changed ->
update blob text + re-embed, update pointer hash. The FTS generated
column updates automatically with ``text``.

Embedder timeout
----------------
2 s wrap on the embedder call. On timeout/exception the blob is stored
keyword-only (model_id='none', embedding=NULL); FTS still covers the
text and :func:`run_embedding_backfill` re-embeds it later off the
write path. Race protection in the worker uses the pointer's
``content_hash`` -- if the task was re-resynced between read and
write, the worker skips the stale row.

Project scoping
---------------
Task blobs are stored with ``project_id=NULL`` and live in the org-wide
search surface. ``memory.retrieve`` keeps its per-project predicate for
note blobs; the unified ``/search`` endpoint runs a separate retrieve
with ``project_id=None`` for ``kind=task`` so task hits are not filtered
out by an active project context. Project filtering on task search is
exposed via the project tag id (faceted, not by column).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import delete, event, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, object_session

from mycelium_core.embedder import Embedder, EmbedResult, get_embedder
from mycelium_core.errors import DomainError
from mycelium_core.models.identity import Identity
from mycelium_core.models.index_scope import IndexScope
from mycelium_core.models.memory_blob import EMBED_DIM, BlobSource, MemoryBlob
from mycelium_core.models.note import Note
from mycelium_core.models.note_part import NotePart
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.task import Task
from mycelium_core.models.task_checklist_item import TaskChecklistItem
from mycelium_core.models.task_index_pointer import TaskIndexPointer
from mycelium_core.services.fts_language import detect_fts_language
from mycelium_core.services.note_effective import note_is_effective

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING only: ``memory`` is otherwise imported at
    # call time (see the note in search_unified) to keep the import graph
    # acyclic. At runtime the value is built via ``memory_svc.RetrievalMeta``.
    from mycelium_core.services.memory import RetrievalMeta

logger = logging.getLogger(__name__)

_DIRTY_KEY = "task_search_dirty"
_DELETED_KEY = "task_search_deleted"
_EMBED_TIMEOUT_S = 2.0
# Cross-branch relative floor for the unified merge: keep hits within this
# fraction of the top score. Deliberately its own constant rather than a
# reuse of memory's per-branch ratio -- same shape and same starting value,
# but a different cut (between branches, not inside one) that can be tuned
# on its own evidence.
_CROSS_BRANCH_FLOOR_RATIO = 0.4
_NO_EMBED_MODEL = "none"


# ---------------------------------------------------------------- listeners


def _record_dirty(session: Session | None, task_id: uuid.UUID | None) -> None:
    if session is None or task_id is None:
        return
    session.info.setdefault(_DIRTY_KEY, set()).add(task_id)


def _record_deleted(session: Session | None, task_id: uuid.UUID | None) -> None:
    if session is None or task_id is None:
        return
    session.info.setdefault(_DELETED_KEY, set()).add(task_id)


def mark_task_dirty(session: AsyncSession, task_id: uuid.UUID) -> None:
    """Mark a task for re-index at commit time.

    The mapper listeners below cover the ORM path (``session.add(task)``
    in ``create_task``, ``session.delete(item)`` for checklist items),
    but ``optimistic_update`` / Core ``update``/``delete`` go via the
    Core API and bypass mapper-level events entirely (the SQLAlchemy
    docs note this explicitly: mapper events fire only for the unit-of-
    work flush). The few choke points in ``services.tasks`` and
    ``services.task_checklist`` that issue Core-level mutations call
    this helper so the resync still fires on commit.

    Cheap and idempotent: dedup via a ``set``; nothing is sent to the DB
    here (the actual blob upsert happens in
    :func:`flush_task_search_dirty`).
    """
    _record_dirty(session.sync_session, task_id)


# The decorator side-effects are what matters here; the function names
# are deliberately unused references (pyright can't see the registration).


@event.listens_for(Task, "after_insert")
def _task_after_insert(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: Task
) -> None:
    _record_dirty(object_session(target), target.id)


@event.listens_for(Task, "after_update")
def _task_after_update(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: Task
) -> None:
    # Skip mutations that don't change the searchable text. ``text`` here
    # is the rendered (title + description + items) blob; a state /
    # priority / due-date update leaves it unchanged and the resync will
    # short-circuit on the content_hash. We still enqueue here -- the
    # cheap hash compare in the flush is the right place to decide.
    _record_dirty(object_session(target), target.id)


@event.listens_for(Task, "after_delete")
def _task_after_delete(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: Task
) -> None:
    _record_deleted(object_session(target), target.id)


@event.listens_for(TaskChecklistItem, "after_insert")
def _item_after_insert(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: TaskChecklistItem
) -> None:
    _record_dirty(object_session(target), target.task_id)


@event.listens_for(TaskChecklistItem, "after_update")
def _item_after_update(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: TaskChecklistItem
) -> None:
    _record_dirty(object_session(target), target.task_id)


@event.listens_for(TaskChecklistItem, "after_delete")
def _item_after_delete(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: TaskChecklistItem
) -> None:
    _record_dirty(object_session(target), target.task_id)


# ---------------------------------------------------------------- rendering


def render_task_for_search(task: Task, items: list[TaskChecklistItem]) -> str:
    """Bullet-markdown rendering, no bracket noise (cleaner for the
    multilingual e5 encoder). Items are ordered by position asc + id
    for determinism. Done items are struck through so the model still
    sees the token but with a contextual marker."""
    parts: list[str] = [task.title or ""]
    if task.description:
        parts.append("")
        parts.append(task.description)
    if items:
        ordered = sorted(items, key=lambda i: (i.position, str(i.id)))
        parts.append("")
        for it in ordered:
            label = it.text or ""
            parts.append(f"- ~~{label}~~" if it.done else f"- {label}")
    return "\n".join(parts).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- embedder helper


@dataclass
class _TimeoutEmbedder:
    """Wrap an Embedder so a slow encode degrades to keyword-only rather
    than blocking commit. The downstream ``memory.write_blob`` already
    swallows arbitrary embed failures (keyword-only fallback), so we
    just need to ensure the call returns in bounded time."""

    inner: Embedder
    timeout: float

    async def embed(self, text: str) -> EmbedResult:
        return await asyncio.wait_for(self.inner.embed(text), timeout=self.timeout)


# ---------------------------------------------------------------- flush


async def flush_task_search_dirty(session: AsyncSession) -> None:
    """Process the dirty/deleted sets accumulated by the listeners.

    Called by ``db.tenant_session`` right before commit, so the resync
    is in the same transaction as the task mutation (FTS becomes
    visible atomically with the write; semantic vector best-effort
    inside the 2s timeout, recovered async otherwise).

    Idempotent: an empty session does nothing. Re-entry safe: pops the
    sets so a nested helper doesn't re-process the same ids.

    Robust to a partially-rolled-back outer transaction: if a caller
    swallowed an IntegrityError raised through a nested SAVEPOINT
    (``services.tasks.create_task`` does this for the appointment-
    overlap path) the AsyncSession may be in a state where its outer
    transactional context is marked as closed, and a fresh execute
    raises ``InvalidRequestError``. We then skip the resync entirely:
    the embedding backfill worker will reconcile the index at its
    next tick (the listener-driven path is authoritative when the
    transaction is healthy; this is the same "keyword-only now,
    semantic later" tradeoff as the embed timeout path).
    """
    from sqlalchemy.exc import InvalidRequestError

    info = session.sync_session.info
    deleted_ids: set[uuid.UUID] = info.pop(_DELETED_KEY, set())
    dirty_ids: set[uuid.UUID] = info.pop(_DIRTY_KEY, set())
    # A task that is being deleted shouldn't also be re-indexed.
    dirty_ids -= deleted_ids
    if not deleted_ids and not dirty_ids:
        return
    from mycelium_core.db import index_maintenance_scope

    try:
        # The blobs this maintains are org-scoped by construction, so the
        # request's project perimeter must not filter them: see
        # ``db.index_maintenance_scope``. Inside the ``try`` and after the
        # early return above, so a caller that reaches the flush with an
        # aborted transaction and nothing pending still emits no SQL.
        async with index_maintenance_scope(session):
            for task_id in deleted_ids:
                await _delete_task_blob(session, task_id)
            for task_id in dirty_ids:
                await _resync_task_blob(session, task_id)
    except InvalidRequestError as exc:
        logger.warning(
            "task-search resync skipped (session unusable, likely partial savepoint rollback): %s",
            exc,
        )


# ---------------------------------------------------------------- delete


async def _delete_task_blob(session: AsyncSession, task_id: uuid.UUID) -> None:
    """Remove the blob owned by this task (pointer cascades).

    The task row may already be gone (after_delete fires post-flush),
    so this is org-blind here on purpose: the pointer carries ``org_id``
    and RLS scopes the SELECT to the current tenant; an attempted
    cross-org delete would simply find no row.
    """
    row = (
        await session.execute(
            select(TaskIndexPointer.blob_id, TaskIndexPointer.org_id).where(
                TaskIndexPointer.task_id == task_id
            )
        )
    ).one_or_none()
    if row is None:
        return
    blob_id, org_id = row
    # Deleting the blob cascades to ``blob_sources``, ``memory_blob_tags``
    # and the pointer itself (FK ON DELETE CASCADE on both legs).
    await session.execute(
        delete(MemoryBlob).where(MemoryBlob.id == blob_id, MemoryBlob.org_id == org_id)
    )


# ---------------------------------------------------------------- resync


async def _load_task_with_items(
    session: AsyncSession, task_id: uuid.UUID
) -> tuple[Task, list[TaskChecklistItem]] | None:
    """Load the live task row + its items.

    Soft-deleted and archived tasks ARE returned: the blob stays in
    place so the unified ``/search`` endpoint can opt-in to surface them
    via ``include_deleted`` / ``include_archived`` (the visibility
    filter is applied at search time, not at index time). Only a hard
    row-removal (which the mapper ``after_delete`` listener catches)
    drops the blob.
    """
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None:
        return None
    items = list(
        (
            await session.execute(
                select(TaskChecklistItem).where(TaskChecklistItem.task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    return task, items


async def _resync_task_blob(session: AsyncSession, task_id: uuid.UUID) -> None:
    """UPSERT one blob for the task + maintain the pointer.

    Three paths, gated on (a) pointer existence and (b) content hash:
      - no pointer: INSERT blob (channel='task', source=task) + pointer
      - pointer + same hash: skip (cheap path, no embed)
      - pointer + new hash: UPDATE blob text+embedding, UPDATE hash
    """
    loaded = await _load_task_with_items(session, task_id)
    if loaded is None:
        # Task is gone (soft-delete or hard-delete that didn't go through
        # after_delete -- e.g. a bulk SQL DELETE). Clean the pointer too,
        # otherwise we'd hold a blob with no source-of-truth.
        await _delete_task_blob(session, task_id)
        return
    task, items = loaded
    if task.index_scope == IndexScope.none:
        # Before the content_hash short-circuit below, deliberately. A scope
        # flip does not change the rendered text, so the hash is unchanged and
        # a guard placed after the short-circuit would never run on a row that
        # is already indexed -- which is the whole remedy case. Delete rather
        # than skip, or the stale blob stays retrievable for good. The blob of
        # a task is 1:1 by construction (no chunker on this path), so the
        # pointer resolves the whole set.
        await _delete_task_blob(session, task_id)
        return
    text_body = render_task_for_search(task, items)
    new_hash = content_hash(text_body)

    pointer = (
        await session.execute(select(TaskIndexPointer).where(TaskIndexPointer.task_id == task_id))
    ).scalar_one_or_none()

    if pointer is not None and pointer.content_hash == new_hash:
        return

    embedder = _TimeoutEmbedder(get_embedder(), _EMBED_TIMEOUT_S)
    vector, model_id, _tokens = await _safe_embed(embedder, text_body)

    if pointer is None:
        await _create_blob_and_pointer(
            session=session,
            task=task,
            text_body=text_body,
            content_hash_value=new_hash,
            vector=vector,
            model_id=model_id,
        )
    else:
        await _update_blob_and_pointer(
            session=session,
            pointer=pointer,
            text_body=text_body,
            content_hash_value=new_hash,
            vector=vector,
            model_id=model_id,
        )


async def _safe_embed(
    embedder: _TimeoutEmbedder, text_body: str
) -> tuple[list[float] | None, str, int]:
    """Best-effort embed. On timeout or any embedder failure (missing
    optional extra, model load error, dim mismatch raised upstream),
    return a keyword-only result; the FTS branch will still cover this
    blob and :func:`run_embedding_backfill` retries later."""
    try:
        result = await embedder.embed(text_body)
    except TimeoutError:
        # 2 s wall-clock cap was hit; storing keyword-only is the design
        # contract here, the backfill worker recovers the vector later.
        return None, _NO_EMBED_MODEL, 0
    except Exception as exc:
        logger.debug("task-search embed failed: %s", exc)
        return None, _NO_EMBED_MODEL, 0
    if not result.vector or len(result.vector) != EMBED_DIM:
        return None, _NO_EMBED_MODEL, 0
    return list(result.vector), result.model_id, result.tokens


async def _create_blob_and_pointer(
    *,
    session: AsyncSession,
    task: Task,
    text_body: str,
    content_hash_value: str,
    vector: list[float] | None,
    model_id: str,
) -> None:
    """Insert path. The blob carries channel_key='task' via the
    ``memory_channel`` tag attached below; ``project_id`` stays NULL
    so the task hit is org-wide (see module docstring 'Project
    scoping')."""
    now = dt.datetime.now(tz=dt.UTC)
    blob = MemoryBlob(
        org_id=task.org_id,
        project_id=None,
        namespace="task",
        tier="hot",
        text=text_body,
        fts_language=detect_fts_language(text_body),
        embedding=vector,
        model_id=model_id,
        dim=EMBED_DIM,
        access_count=1,
        last_accessed_at=now,
    )
    session.add(blob)
    await session.flush()
    session.add(
        BlobSource(
            blob_id=blob.id,
            org_id=task.org_id,
            source_kind="task",
            source_id=str(task.id),
        )
    )
    await _attach_inherited_tags(session, org_id=task.org_id, blob_id=blob.id, task_id=task.id)
    await _attach_channel_tag(session, org_id=task.org_id, blob_id=blob.id)
    try:
        async with session.begin_nested():
            session.add(
                TaskIndexPointer(
                    task_id=task.id,
                    org_id=task.org_id,
                    blob_id=blob.id,
                    content_hash=content_hash_value,
                )
            )
            await session.flush()
    except IntegrityError:
        # Concurrent resync just inserted the pointer. Drop the blob we
        # just made: the other transaction's blob is the canonical one,
        # ours would be orphaned (UNIQUE(blob_id) already protects the
        # winner). The duplicate blob is cleaned up here so the row
        # count stays correct.
        await session.execute(
            delete(MemoryBlob).where(MemoryBlob.id == blob.id, MemoryBlob.org_id == task.org_id)
        )


async def _update_blob_and_pointer(
    *,
    session: AsyncSession,
    pointer: TaskIndexPointer,
    text_body: str,
    content_hash_value: str,
    vector: list[float] | None,
    model_id: str,
) -> None:
    """Update path. Direct SQL UPDATE on the blob (preserves
    cluster/access counters); FTS is a generated column over ``text``
    so it follows automatically."""
    await session.execute(
        update(MemoryBlob)
        .where(MemoryBlob.id == pointer.blob_id, MemoryBlob.org_id == pointer.org_id)
        .values(
            text=text_body,
            fts_language=detect_fts_language(text_body),
            embedding=vector,
            model_id=model_id,
            dim=EMBED_DIM,
        )
    )
    pointer.content_hash = content_hash_value


# ---------------------------------------------------------------- tag wiring


async def _attach_inherited_tags(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    blob_id: uuid.UUID,
    task_id: uuid.UUID,
) -> None:
    """Copy the task's tags onto its blob (same spirit as
    ``memory._inherited_tag_ids``, but here we know the single source
    so we don't go through the generic helper)."""
    from mycelium_core.models.memory_blob import MemoryBlobTag
    from mycelium_core.models.task_tag import TaskTag

    rows = (
        (await session.execute(select(TaskTag.tag_id).where(TaskTag.task_id == task_id)))
        .scalars()
        .all()
    )
    for tid in set(rows):
        session.add(MemoryBlobTag(blob_id=blob_id, org_id=org_id, tag_id=tid))
    if rows:
        await session.flush()


async def _attach_channel_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    blob_id: uuid.UUID,
) -> None:
    """Pin the blob to the ``memory_channel`` tag with system_key='task'.
    Seeded lazily by ``taxonomy.ensure_default_memory_channels`` for
    every tenant; we ensure-seed here to keep the resync self-contained
    (a write that landed before anyone listed channels would otherwise
    fail to find the tag)."""
    from mycelium_core.models.memory_blob import MemoryBlobTag
    from mycelium_core.models.tag import Tag, TagKind
    from mycelium_core.services import taxonomy

    await taxonomy.ensure_default_memory_channels(session, org_id=org_id)
    channel_id = (
        await session.execute(
            select(Tag.id).where(Tag.kind == TagKind.memory_channel, Tag.system_key == "task")
        )
    ).scalar_one_or_none()
    if channel_id is None:
        return
    try:
        async with session.begin_nested():
            session.add(MemoryBlobTag(blob_id=blob_id, org_id=org_id, tag_id=channel_id))
            await session.flush()
    except IntegrityError:
        # Tag already attached (e.g. inherited path picked it up): no-op.
        return


# ---------------------------------------------------------------- backfill worker


@dataclass(frozen=True)
class UnifiedHit:
    """Result row for the unified /search endpoint.

    The entity ref depends on ``kind``:
      - ``task``: ``task_id`` set (resolved via ``task_index_pointer``).
      - ``note``: ``note_id`` + ``part_id`` set (resolved via
        ``note_part_index_pointer``); ``title`` is the note title.
      - ``blob``: none of the above set -- an opaque memory row.
    """

    kind: str  # 'task' | 'note' | 'blob'
    blob_id: uuid.UUID
    task_id: uuid.UUID | None
    title: str | None
    snippet: str | None
    score: float
    # Which boundary this hit was retrieved under: 'org' (whole workspace)
    # or 'project' (the caller's project). The task branch is org-wide by
    # default even when project_id is set -- so an agent that passes
    # project_id can SEE that its task hits are NOT project-scoped, instead
    # of silently believing they are (audit finding #5).
    scope: str = "org"
    # Backing blob's embedding model id; 'none' (or None) means the hit is
    # keyword-only (no dense vector) -- surfaced so a caller can see when a
    # result rode FTS alone (audit #8, task 4f3c2207).
    model_id: str | None = None
    note_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    # Why this hit ranked here, carried through from ``memory.Hit``: the
    # per-branch entries are 1-BASED RANKS (lexical_exact / lexical_stem /
    # semantic / semantic_hosted / humus), not scores, and only ``rrf`` is a
    # score -- ``score`` above is that same fused value. Without the
    # breakdown a caller cannot tell a hit the lexical branch found from one
    # only the dense branch reached, and the fused number cannot tell them
    # apart either: RRF fuses by rank, so every dense-only hit scores exactly
    # ``0.2/(60+rank)`` whatever its cosine was. ``memory_search`` has
    # surfaced this from the start; the unified search dropped it here.
    # If the cross-encoder ran, it also carries ``rerank``: that one IS a
    # score, the raw logit, on a different scale, and ``score`` above is then
    # the logit rather than the fused RRF (see ``stages/rerank.py``).
    # Empty when no ranked retrieval ran at all, as on the entity-code path.
    scores_by_stage: dict[str, float] = field(default_factory=dict)


def _aggregate_unified_meta(metas: list[RetrievalMeta], final: list[UnifiedHit]) -> RetrievalMeta:
    """Fold the per-branch recall metas into one unified meta. ``abstained`` is
    True ONLY when abstention shaped an EMPTY result -- a thin-but-nonempty
    unified result (some branch returned hits) is never mislabelled as "empty
    due to abstain" (audit A-5). Module-level + pure so the invariant is
    unit-testable without calibrating live RRF scores against the floor."""
    from mycelium_core.services import memory as memory_svc

    abstained_metas = [m for m in metas if m.abstained]
    abstained = bool(abstained_metas) and not final
    return memory_svc.RetrievalMeta(
        query_embedded=any(m.query_embedded for m in metas),
        dense_branch_contributed=any(m.dense_branch_contributed for m in metas),
        dense_rejected_by_floor=sum(m.dense_rejected_by_floor for m in metas),
        keyword_only_hits=sum(1 for h in final if (h.model_id or "none") == "none"),
        abstained=abstained,
        abstain_reason=abstained_metas[0].abstain_reason if abstained else None,
        rerank_failed=any(m.rerank_failed for m in metas),
    )


async def search_unified_with_meta(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_id: uuid.UUID | None,
    query: str,
    kinds: list[str],
    tag_ids: list[uuid.UUID],
    channel_keys: list[str],
    limit: int,
    include_archived: bool,
    include_deleted: bool,
    operation_id: str,
    rerank: bool = False,
    task_scope: str = "org",
    due_before: dt.datetime | None = None,
    assignee_handles: list[str] | None = None,
    task_state_id: uuid.UUID | None = None,
) -> tuple[list[UnifiedHit], RetrievalMeta]:
    """Unified search across tasks, notes and memory blobs.

    Project scoping is split per kind and each hit carries a ``scope``
    ('org' | 'project') saying which boundary it was retrieved under, so a
    caller passing ``project_id`` is never misled about what was scoped.
    ``note`` / ``blob`` hits run project-scoped against the caller's
    project (scope 'project' when ``project_id`` is set). The ``task``
    branch is org-wide by default (task entities have no ``project_id``
    column; project filtering rides the project tag) -- pass
    ``task_scope='project'`` to AND the caller's project tag into the task
    branch so its hits are project-scoped too. ``due_before`` /
    ``assignee_handles`` / ``task_state_id`` are post-retrieval facets on
    the task branch (so 'tasks due today assigned to X' is answerable via
    search too). Each kind is an independent retrieve; results merge by
    score descending (RRF already applied inside each branch), then dedup
    so a blob that resolved to a task/note isn't also surfaced as an opaque
    ``blob`` row.
    """
    # Local imports break a static cycle: memory imports nothing from
    # task_search, but task_search imports memory only at call time.
    from mycelium_core.services import lookup as lookup_svc
    from mycelium_core.services import memory as memory_svc

    # An entity code is a LOOKUP, not a similarity question. Searching
    # ``f62ff51d`` asks "which entity is this, and where is it cited", and
    # the only meaningful match for either half is EXACT. Left to the normal
    # pipeline the token gets embedded, and since 8 hex digits carry no
    # meaning its nearest neighbours are arbitrary: five confident, unrelated
    # results for a query that should have returned one row or none.
    #
    # So an id query runs the same branches with ``exact_only``: verbatim FTS
    # only, no embedding, no stemming, no cross-encoder. The prefix
    # resolution is added on top, because that is the half no text retrieval
    # can produce (a note does not contain its own id).
    code_query = lookup_svc.looks_like_entity_code(query)

    want_task = "task" in kinds
    want_note = "note" in kinds
    want_blob = "blob" in kinds

    # Per-branch recall meta, aggregated at the end so the caller can tell a
    # genuinely-empty result from silently-degraded recall (task 4f3c2207).
    metas: list[RetrievalMeta] = []

    def _aggregate(final: list[UnifiedHit]) -> RetrievalMeta:
        return _aggregate_unified_meta(metas, final)

    if not want_task and not want_note and not want_blob:
        return [], _aggregate([])

    hits: list[UnifiedHit] = []

    if want_task:
        # Task blobs carry project_id=NULL (org-wide memory channel), so
        # project scoping rides the project TAG, not the blob predicate.
        # ``project_id`` IS the project tag id here (the X-Project-Id the
        # SPA focuses on), so task_scope='project' just ANDs it into the
        # task channel's tag_ids. The 'task' channel tag is required so
        # notes/manual blobs don't leak into the task surface; user-supplied
        # tag_ids are ANDed on top.
        task_project_scoped = task_scope == "project" and project_id is not None
        task_tag_ids = list(tag_ids)
        if task_project_scoped and project_id is not None:
            task_tag_ids.append(project_id)
        task_hits, task_rmeta = await memory_svc.retrieve_with_meta(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=None,
            query=query,
            operation_id=operation_id,
            limit=max(limit * 2, limit),
            tag_ids=task_tag_ids,
            channel_key="task",
            rerank=rerank,
            include_deleted_sources=include_deleted,
            exact_only=code_query,
        )
        metas.append(task_rmeta)
        if task_hits:
            blob_ids = [h.blob.id for h in task_hits]
            id_to_task = await _resolve_task_ids(session, blob_ids)
            task_meta = await _task_filter_meta(
                session,
                task_ids=list(id_to_task.values()),
                include_archived=include_archived,
                include_deleted=include_deleted,
                due_before=due_before,
                assignee_handles=assignee_handles,
                state_id=task_state_id,
            )
            snippets = await _ts_headlines(session, blob_ids=blob_ids, query=query)
            task_hit_scope = "project" if task_project_scoped else "org"
            for h in task_hits:
                tid = id_to_task.get(h.blob.id)
                if tid is None:
                    # Stale blob with no live pointer (e.g. orphan from
                    # a race); skip rather than surface a hit that won't
                    # resolve in the SPA.
                    continue
                meta = task_meta.get(tid)
                if meta is None:
                    # Task gone, hidden (archived/deleted), or filtered out
                    # by a due/assignee/state facet.
                    continue
                hits.append(
                    UnifiedHit(
                        kind="task",
                        blob_id=h.blob.id,
                        task_id=tid,
                        title=meta.title,
                        snippet=snippets.get(h.blob.id),
                        score=h.rrf,
                        scores_by_stage=dict(h.scores_by_stage),
                        scope=task_hit_scope,
                        model_id=h.blob.model_id,
                    )
                )

    if want_note:
        # Note search mirrors the task branch: the 'note' channel tag
        # scopes the retrieve to note-part blobs, then we resolve each
        # blob to its note via ``note_part_index_pointer`` and surface a
        # titled hit that the SPA can route to /notes/:id.
        #
        # A caller that named a project gets that project. A caller that
        # named none gets EVERY perimeter, not the NULL one, and that is the
        # repair rather than a widening for its own sake. Note blobs carry
        # their note's project, so ``project_id=None`` -- which means "the
        # NULL perimeter" here and in three other services -- could never
        # reach a note that belonged to a project. Task blobs live at NULL by
        # design and were therefore always visible, which is why a gold set
        # of twenty questions on 2026-09-07 scored 3 of 6 on tasks and
        # **0 of 14 on notes**: the same one-line predicate, read twice.
        note_scope = memory_svc.ANY_PROJECT if project_id is None else project_id
        note_hits, note_rmeta = await memory_svc.retrieve_with_meta(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=note_scope,
            query=query,
            operation_id=operation_id,
            limit=max(limit * 2, limit),
            tag_ids=tag_ids,
            channel_key="note",
            rerank=rerank,
            include_deleted_sources=include_deleted,
            exact_only=code_query,
        )
        metas.append(note_rmeta)
        if note_hits:
            blob_ids = [h.blob.id for h in note_hits]
            blob_to_ref = await _resolve_note_refs(session, blob_ids)
            note_meta = await _note_filter_meta(
                session,
                note_ids=[nid for nid, _pid in blob_to_ref.values()],
                include_archived=include_archived,
                include_deleted=include_deleted,
            )
            snippets = await _ts_headlines(session, blob_ids=blob_ids, query=query)
            for h in note_hits:
                ref = blob_to_ref.get(h.blob.id)
                if ref is None:
                    # Blob on the note channel with no live pointer (e.g.
                    # a legacy explicit note write, or an orphan): skip
                    # rather than surface a hit that won't route.
                    continue
                note_id, part_id = ref
                note_m = note_meta.get(note_id)
                if note_m is None:
                    # Note gone or filtered out (soft-deleted/archived).
                    continue
                hits.append(
                    UnifiedHit(
                        kind="note",
                        blob_id=h.blob.id,
                        task_id=None,
                        note_id=note_id,
                        part_id=part_id,
                        title=note_m.title,
                        snippet=snippets.get(h.blob.id),
                        score=h.rrf,
                        scores_by_stage=dict(h.scores_by_stage),
                        scope="project" if project_id is not None else "org",
                        model_id=h.blob.model_id,
                    )
                )

    if want_blob:
        # Channel filter for blob search: if the caller asked for a
        # specific channel via channel_keys, narrow to it; otherwise
        # span every memory channel. We resolve each key one at a time
        # because retrieve() takes a single channel_key (channels are a
        # tag facet ANDed inside the retrieve; for OR-across-channels
        # we'd need multiple retrieves -- out of scope for v1, callers
        # pass at most one channel).
        single_channel = channel_keys[0] if channel_keys else None
        # Same perimeter repair as the note branch above, and for the same
        # reason: a caller that named no project is asking across projects,
        # not asking for the blobs that happen to belong to none.
        blob_scope = memory_svc.ANY_PROJECT if project_id is None else project_id
        blob_hits, blob_rmeta = await memory_svc.retrieve_with_meta(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=blob_scope,
            query=query,
            operation_id=operation_id,
            limit=max(limit * 2, limit),
            tag_ids=tag_ids,
            channel_key=single_channel,
            rerank=rerank,
            include_deleted_sources=include_deleted,
            exact_only=code_query,
        )
        metas.append(blob_rmeta)
        if blob_hits:
            blob_ids = [h.blob.id for h in blob_hits]
            snippets = await _ts_headlines(session, blob_ids=blob_ids, query=query)
            for h in blob_hits:
                # Skip blobs that also have a task pointer when 'task'
                # was already requested: they were emitted as kind=task
                # above. When only kind='blob' is requested, surface them
                # as opaque blobs (the namespace='task' is informative
                # but doesn't change the row shape).
                hits.append(
                    UnifiedHit(
                        kind="blob",
                        blob_id=h.blob.id,
                        task_id=None,
                        title=None,
                        snippet=snippets.get(h.blob.id),
                        score=h.rrf,
                        scores_by_stage=dict(h.scores_by_stage),
                        scope="project" if project_id is not None else "org",
                        model_id=h.blob.model_id,
                    )
                )

    # Dedup by blob: the catch-all 'blob' branch (channel=None) spans
    # every channel, so a task/note blob also surfaces there as an opaque
    # row. When the same blob was already emitted as a titled kind
    # (task/note), drop the 'blob' duplicate -- keep the row that routes.
    typed_blob_ids = {h.blob_id for h in hits if h.kind in ("task", "note")}
    if typed_blob_ids:
        hits = [h for h in hits if not (h.kind == "blob" and h.blob_id in typed_blob_ids)]

    if code_query:
        # The entity the code NAMES, which no text retrieval can find: a note
        # does not contain its own id. Scored above every retrieved hit (the
        # fused RRF scale tops out around 0.03), so it leads and the literal
        # citations follow.
        resolved = await _entity_code_matches(
            session,
            prefix=query,
            want_task=want_task,
            want_note=want_note,
            include_archived=include_archived,
            include_deleted=include_deleted,
        )
        if resolved:
            named = {(h.kind, h.task_id, h.note_id) for h in resolved}
            hits = resolved + [h for h in hits if (h.kind, h.task_id, h.note_id) not in named]

    hits.sort(key=lambda r: (-r.score, r.kind, str(r.blob_id)))
    if not code_query:
        # Cross-branch relative floor. Each branch already drops its own tail
        # (RelativeFloorStage), but that floor is relative to the branch's OWN
        # top -- so a branch holding no real match has a FLAT score profile,
        # where the floor is deliberately a no-op to protect recall on
        # conceptual queries, and it contributes its full quota of weak hits
        # to a merge that had no floor at all. That is how a query with one
        # strong note match came back padded with four unrelated tasks.
        #
        # The branches share one RRF scale (same k, same weights), so their
        # scores are directly comparable. Same shape as the per-branch rule:
        # a wide gap means real hits set the top and the tail is noise; a flat
        # all-semantic profile is untouched.
        top = hits[0].score if hits else 0.0
        if top > 0.0:
            floor = _CROSS_BRANCH_FLOOR_RATIO * top
            hits = [h for h in hits if h.score >= floor]
    final = hits[:limit]
    return final, _aggregate(final)


async def _entity_code_matches(
    session: AsyncSession,
    *,
    prefix: str,
    want_task: bool,
    want_note: bool,
    include_archived: bool,
    include_deleted: bool,
) -> list[UnifiedHit]:
    """The entities whose id starts with ``prefix``, shaped as search hits.

    An entity is surfaced only when it has an index blob, because
    ``UnifiedHit`` is keyed on one: everything indexable is indexed, and an
    entity created microseconds ago and not yet flushed is better absent
    than surfaced as a row the click telemetry cannot key.
    """
    from mycelium_core.services import lookup as lookup_svc

    kinds = tuple(k for k, want in (("task", want_task), ("note", want_note)) if want)
    if not kinds:
        return []
    try:
        p = lookup_svc.normalise_prefix(prefix)
    except DomainError:
        # Unreachable via ``looks_like_entity_code`` (stricter on both ends);
        # cheap insurance for any other caller.
        return []
    matches = await lookup_svc.resolve_prefix(
        session,
        prefix=p,
        kinds=kinds,
        include_archived=include_archived,
        include_deleted=include_deleted,
    )
    if not matches:
        return []

    task_ids = [m.id for m in matches if m.kind == "task"]
    note_ids = [m.id for m in matches if m.kind == "note"]
    task_blob: dict[uuid.UUID, uuid.UUID] = {}
    if task_ids:
        task_rows = (
            await session.execute(
                select(TaskIndexPointer.task_id, TaskIndexPointer.blob_id).where(
                    TaskIndexPointer.task_id.in_(task_ids)
                )
            )
        ).all()
        task_blob = {tid: bid for tid, bid in task_rows}
    note_blob: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
    if note_ids:
        # Route to the note's FIRST part: the top of the document is where a
        # reader who followed an id wants to land.
        note_rows = (
            await session.execute(
                select(
                    NotePartIndexPointer.note_id,
                    NotePartIndexPointer.blob_id,
                    NotePartIndexPointer.part_id,
                )
                .join(NotePart, NotePart.id == NotePartIndexPointer.part_id)
                .where(NotePartIndexPointer.note_id.in_(note_ids))
                .order_by(NotePartIndexPointer.note_id, NotePart.ord)
            )
        ).all()
        for nid, bid, pid in note_rows:
            note_blob.setdefault(nid, (bid, pid))

    out: list[UnifiedHit] = []
    for i, m in enumerate(matches):
        # Preserve the resolver's deliberate order (tasks first, then most
        # recently updated) through the score sort below, which would
        # otherwise re-break ties on kind and blob id.
        #
        # ``scores_by_stage`` stays empty on this path and that is the
        # honest answer, not an omission: nothing was ranked. An entity
        # code is an exact lookup, so there are no branches to report and
        # ``score`` is a position marker rather than a relevance value.
        score = 1.0 - i * 1e-6
        if m.kind == "task":
            bid = task_blob.get(m.id)
            if bid is None:
                continue
            out.append(
                UnifiedHit(
                    kind="task",
                    blob_id=bid,
                    task_id=m.id,
                    title=m.title,
                    snippet=None,
                    score=score,
                    scope="org",
                )
            )
        else:
            ref = note_blob.get(m.id)
            if ref is None:
                continue
            bid, pid = ref
            out.append(
                UnifiedHit(
                    kind="note",
                    blob_id=bid,
                    task_id=None,
                    note_id=m.id,
                    part_id=pid,
                    title=m.title,
                    snippet=None,
                    score=score,
                    scope="org",
                )
            )
    return out


async def search_unified(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_id: uuid.UUID | None,
    query: str,
    kinds: list[str],
    tag_ids: list[uuid.UUID],
    channel_keys: list[str],
    limit: int,
    include_archived: bool,
    include_deleted: bool,
    operation_id: str,
    rerank: bool = False,
    task_scope: str = "org",
    due_before: dt.datetime | None = None,
    assignee_handles: list[str] | None = None,
    task_state_id: uuid.UUID | None = None,
) -> list[UnifiedHit]:
    """Unified search -> the ranked hits. Thin wrapper over
    :func:`search_unified_with_meta` for callers that don't need the recall
    diagnostics (REST /search, the assistant, the agent runtime); the meta
    variant powers the MCP ``search`` tool."""
    hits, _meta = await search_unified_with_meta(
        session,
        org_id=org_id,
        actor_id=actor_id,
        project_id=project_id,
        query=query,
        kinds=kinds,
        tag_ids=tag_ids,
        channel_keys=channel_keys,
        limit=limit,
        include_archived=include_archived,
        include_deleted=include_deleted,
        operation_id=operation_id,
        rerank=rerank,
        task_scope=task_scope,
        due_before=due_before,
        assignee_handles=assignee_handles,
        task_state_id=task_state_id,
    )
    return hits


async def _resolve_task_ids(
    session: AsyncSession, blob_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    if not blob_ids:
        return {}
    rows = (
        await session.execute(
            select(TaskIndexPointer.blob_id, TaskIndexPointer.task_id).where(
                TaskIndexPointer.blob_id.in_(blob_ids)
            )
        )
    ).all()
    return {bid: tid for bid, tid in rows}


async def _resolve_note_refs(
    session: AsyncSession, blob_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]]:
    """Map each note-part blob id to ``(note_id, part_id)`` via the
    ``note_part_index_pointer`` (note_id is denormalised on the pointer,
    so this is a single SELECT with no join to ``note_part``)."""
    if not blob_ids:
        return {}
    rows = (
        await session.execute(
            select(
                NotePartIndexPointer.blob_id,
                NotePartIndexPointer.note_id,
                NotePartIndexPointer.part_id,
            ).where(NotePartIndexPointer.blob_id.in_(blob_ids))
        )
    ).all()
    return {bid: (nid, pid) for bid, nid, pid in rows}


@dataclass(frozen=True)
class _NoteMeta:
    title: str | None


async def _note_filter_meta(
    session: AsyncSession,
    *,
    note_ids: list[uuid.UUID],
    include_archived: bool,
    include_deleted: bool,
) -> dict[uuid.UUID, _NoteMeta]:
    """Batched SELECT of note ids + titles, applying the same
    soft-delete / archived visibility filters as the task branch so a
    hidden note is not surfaced even if its part blob ranked high."""
    if not note_ids:
        return {}
    stmt = select(Note.id, Note.title, Note.deleted_at, Note.is_archived, Note.review_state).where(
        Note.id.in_(note_ids)
    )
    rows = (await session.execute(stmt)).all()
    out: dict[uuid.UUID, _NoteMeta] = {}
    for nid, title, deleted_at, is_archived, review_state in rows:
        # The row-level form of the effective-note predicate: a 'proposed'
        # note is never surfaced through unified search even if its part blob
        # ranked high (belt-and-suspenders with the retrieve-level filter),
        # and the bin needs the explicit opt-in.
        if not note_is_effective(
            review_state=review_state, deleted_at=deleted_at, include_deleted=include_deleted
        ):
            continue
        if is_archived and not include_archived:
            continue
        out[nid] = _NoteMeta(title=title)
    return out


@dataclass(frozen=True)
class _TaskMeta:
    title: str


async def _task_filter_meta(
    session: AsyncSession,
    *,
    task_ids: list[uuid.UUID],
    include_archived: bool,
    include_deleted: bool,
    due_before: dt.datetime | None = None,
    assignee_handles: list[str] | None = None,
    state_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, _TaskMeta]:
    """Single batched SELECT that returns task ids + titles, applying the
    soft-delete / archived filters (and the optional due/assignee/state
    facets) as a WHERE so a hit that ranked high but does not match is
    dropped from the result -- the facets run here, on the rows this pass
    already batch-loads, not as a second query."""
    if not task_ids:
        return {}
    stmt = select(Task.id, Task.title, Task.deleted_at, Task.is_archived).where(
        Task.id.in_(task_ids)
    )
    if state_id is not None:
        stmt = stmt.where(Task.state_id == state_id)
    if due_before is not None:
        # Half-open upper bound; a NULL due_date never matches a due facet.
        stmt = stmt.where(Task.due_date < due_before)
    if assignee_handles:
        stmt = stmt.join(Identity, Identity.id == Task.assignee_id).where(
            Identity.handle.in_(assignee_handles)
        )
    rows = (await session.execute(stmt)).all()
    out: dict[uuid.UUID, _TaskMeta] = {}
    for tid, title, deleted_at, is_archived in rows:
        if deleted_at is not None and not include_deleted:
            continue
        if is_archived and not include_archived:
            continue
        out[tid] = _TaskMeta(title=title)
    return out


async def _ts_headlines(
    session: AsyncSession, *, blob_ids: list[uuid.UUID], query: str
) -> dict[uuid.UUID, str]:
    """Postgres-native snippet: ``ts_headline`` over the blob ``text``
    in the row's OWN language (task b1baaf52) so a stemmed hit is actually
    highlighted, not just an exact one. One pass, no Python-side
    highlighting -- the SPA can re-emphasise tokens if it wants.
    ``MaxFragments=1, MaxWords=20`` keeps the snippet UI-sized."""
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


async def run_embedding_backfill(session: AsyncSession, *, batch_size: int = 20) -> int:
    """Re-embed task blobs whose first write timed out.

    Selects blobs flagged ``model_id='none'`` AND tied to a task pointer
    (other blob origins -- e.g. note snapshots -- are out of scope for
    this safety net; the memory service handles them through its own
    write path).

    Race protection: the UPDATE compares against the pointer
    ``content_hash`` captured at SELECT time; if the task was resynced
    between read and write, the pointer hash will have changed and the
    UPDATE matches zero rows (the listener-driven path is authoritative,
    we just skip this round). Returns the number of blobs re-embedded.
    """
    rows = (
        await session.execute(
            select(
                MemoryBlob.id,
                MemoryBlob.org_id,
                MemoryBlob.text,
                TaskIndexPointer.content_hash,
            )
            .join(
                TaskIndexPointer,
                (TaskIndexPointer.blob_id == MemoryBlob.id)
                & (TaskIndexPointer.org_id == MemoryBlob.org_id),
            )
            .where(MemoryBlob.model_id == _NO_EMBED_MODEL)
            .limit(batch_size)
        )
    ).all()
    if not rows:
        return 0
    embedder = _TimeoutEmbedder(get_embedder(), _EMBED_TIMEOUT_S)
    updated = 0
    for blob_id, org_id, body, original_hash in rows:
        if not body:
            continue
        vector, model_id, _tokens = await _safe_embed(embedder, body)
        if vector is None:
            continue
        result: CursorResult[tuple[uuid.UUID, ...]] = await session.execute(  # type: ignore[assignment]
            update(MemoryBlob)
            .where(
                MemoryBlob.id == blob_id,
                MemoryBlob.org_id == org_id,
                MemoryBlob.id.in_(
                    select(TaskIndexPointer.blob_id).where(
                        TaskIndexPointer.blob_id == blob_id,
                        TaskIndexPointer.content_hash == original_hash,
                    )
                ),
            )
            .values(embedding=vector, model_id=model_id, dim=EMBED_DIM)
        )
        if result.rowcount > 0:
            updated += 1
    return updated


async def run_pointer_backfill(session: AsyncSession, *, batch_size: int = 50) -> int:
    """Index tasks that don't have a ``task_index_pointer`` yet.

    The listener path catches every NEW mutation, but tasks that
    pre-date the deploy of task-search never went through it -- they
    live in the DB without a pointer/blob, so /search misses them.
    This sweep picks the first ``batch_size`` unindexed tasks (skipping
    soft-deleted: they would just be resync'd and immediately cleaned
    up by the pointer ``_load_task_with_items`` invariant, and skipping
    ``index_scope='none'``: those have no pointer by design) and runs
    the same ``_resync_task_blob`` the listener would have.

    Returns the count of tasks indexed in this batch. The worker calls
    this every tick so a large backlog drains gradually
    (1538 tasks at batch=50/tick * 60s = ~30 min to full coverage);
    callers that want immediate coverage can hit the
    ``POST /search/reindex`` admin endpoint, which runs the same
    helper synchronously with a larger batch.
    """
    rows = (
        await session.execute(
            select(Task.id)
            .outerjoin(TaskIndexPointer, TaskIndexPointer.task_id == Task.id)
            .where(
                TaskIndexPointer.task_id.is_(None),
                Task.deleted_at.is_(None),
                # A task at ``none`` has no pointer by definition, so without
                # this term it stays a candidate on every tick, fills the
                # batch, and starves the real backlog. The guard in the resync
                # makes it harmless; this makes it free.
                Task.index_scope != IndexScope.none,
            )
            .limit(batch_size)
        )
    ).all()
    if not rows:
        return 0
    indexed = 0
    for (task_id,) in rows:
        try:
            await _resync_task_blob(session, task_id)
            indexed += 1
        except Exception:
            logger.exception("task-search pointer backfill failed for task_id=%s", task_id)
    return indexed
