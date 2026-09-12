"""Embedding backfill (task 5276207e).

Re-embeds missing vectors for the current tenant, both tiers, so a dim
rebuild or a per-org hosted-embedder opt-in converges in the background
instead of a write-blocking big-bang:

- LOCAL ``embedding``: rows where it is NULL (e.g. after the 0028 dim
  rebuild, or a keyword-only task-search write), OR where it was written
  by a model that is no longer the active one. Uses the local embedder.
- HOSTED ``embedding_hosted``: the same, only when the org has a hosted
  embedder configured (``resolve_hosted_embedder``).

The stale-model half is why this converges at all. Selecting on IS NULL
alone means a swap to a different model OF THE SAME DIMENSION leaves the
old vectors in the column permanently, with no signal: nothing re-embeds
them, and the kNN compares them against queries from the new model, which
is the failure ADR-0030 exists to prevent. Only a dim change converged,
and only because it nulls the column.

Race protection: every UPDATE keeps the same eligibility predicate as its
guard, so a concurrent worker or a fresh write that already put the active
model on the row makes this UPDATE a no-op (no double work). The periodic
loop wrapper lives in ``worker/embedding_migration.py``, budget-paused and
batched, so a swap converges in the background rather than as one bill.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.embed_dims import EMBED_DIM, EMBED_DIM_HOSTED
from mycelium_core.embedder import Embedder, EmbedSide, embed_batch, get_embedder
from mycelium_core.models.memory_blob import MemoryBlob

logger = logging.getLogger(__name__)


async def _backfill_tier(
    session: AsyncSession,
    *,
    embedder: Embedder,
    expected_dim: int,
    stale: Any,
    set_values: Callable[..., Mapping[str, Any]],
    batch_size: int,
    tier: str,
) -> int:
    """Embed a batch of rows whose tier vector is missing or stale, and UPDATE
    under the same predicate as a guard. ``stale`` is the eligibility predicate
    and ``set_values(result)`` builds the UPDATE values."""
    rows = (
        await session.execute(
            select(MemoryBlob.id, MemoryBlob.org_id, MemoryBlob.text)
            .where(stale, MemoryBlob.text.is_not(None))
            .limit(batch_size)
        )
    ).all()
    # Skip empty/whitespace bodies (text IS NOT NULL still admits ""): they
    # embed to nothing and would just churn the same rows every sweep.
    candidates = [(bid, borg, txt) for bid, borg, txt in rows if txt and txt.strip()]
    if not candidates:
        return 0
    # Embed the whole batch in ONE forward pass instead of N sequential
    # ``embed`` calls: SentenceTransformer batches internally, ~an order of
    # magnitude faster (per-call tokenizer/Python overhead dominates at
    # small N). ``embed_batch`` falls back to a sequential loop for embedders
    # without a batch method (e.g. the CI fake / a future hosted provider).
    try:
        results = await embed_batch(
            embedder, [txt for _, _, txt in candidates], side=EmbedSide.document
        )
    except Exception:
        # Fail LOUD, not silent: a systemic embedder failure (e.g. the model
        # extra missing from this process image) used to be swallowed at
        # ``debug`` per row, so the backfill no-opped invisibly and the dense
        # tier stayed empty with no signal. Surface it and skip this sweep.
        logger.warning(
            "%s backfill: embedder failed on %d eligible row(s) "
            "(model unavailable in this process?); skipping this sweep",
            tier,
            len(candidates),
            exc_info=True,
        )
        return 0
    done = 0
    for (blob_id, blob_org, _), result in zip(candidates, results, strict=False):
        if not result.vector or len(result.vector) != expected_dim:
            logger.warning(
                "%s backfill dim mismatch for blob_id=%s (got %d, expected %d)",
                tier,
                blob_id,
                len(result.vector) if result.vector else 0,
                expected_dim,
            )
            continue
        upd = await session.execute(
            update(MemoryBlob)
            .where(MemoryBlob.id == blob_id, MemoryBlob.org_id == blob_org, stale)
            .values(**set_values(result))
        )
        if (upd.rowcount or 0) > 0:  # type: ignore[attr-defined]
            done += 1
    return done


async def run_embedding_backfill(
    session: AsyncSession, org_id: uuid.UUID, *, batch_size: int = 50
) -> int:
    """Backfill both tiers for the current tenant; returns rows touched."""
    settings = get_settings()
    embedder = get_embedder()
    # The active model is whatever the embedder ABOUT TO WRITE will stamp on the row, not
    # what configuration names. They agree in production, where LocalEmbedder is constructed
    # from settings.embed_model; they do not under a test or a debug override, and taking the
    # setting there would mark every row stale and re-embed the corpus on every sweep.
    # LocalEmbedder keeps its name privately, so the setting is the fallback rather than a
    # guess: an unknown active model must not make `is_distinct_from` true for everything.
    local_model = getattr(embedder, "model_id", None) or settings.embed_model
    done = await _backfill_tier(
        session,
        embedder=embedder,
        expected_dim=EMBED_DIM,
        # Missing, or written by a model that is not the active one. The IS NULL
        # arm is kept explicitly rather than folded into the model comparison:
        # a dim rebuild nulls the vector without necessarily clearing model_id,
        # and folding it would leave exactly those rows ineligible forever.
        stale=or_(
            MemoryBlob.embedding.is_(None),
            MemoryBlob.model_id.is_distinct_from(local_model),
        ),
        set_values=lambda r: {
            "embedding": r.vector,
            "model_id": r.model_id,
            "dim": len(r.vector),
        },
        batch_size=batch_size,
        tier="local",
    )
    from mycelium_core.services.embedder_resolver import resolve_hosted_embedder

    hosted = await resolve_hosted_embedder(session, org_id)
    if hosted is not None:
        # The resolved embedder names its own model. Falling back to None rather
        # than to a guessed string matters: an unknown active model would make
        # `is_distinct_from` true for every row and re-embed the whole hosted
        # tier, which is a bill rather than a repair.
        hosted_model = getattr(hosted[0], "model_id", None)
        done += await _backfill_tier(
            session,
            embedder=hosted[0],
            expected_dim=EMBED_DIM_HOSTED,
            stale=(
                MemoryBlob.embedding_hosted.is_(None)
                if hosted_model is None
                else or_(
                    MemoryBlob.embedding_hosted.is_(None),
                    MemoryBlob.model_id_hosted.is_distinct_from(hosted_model),
                )
            ),
            set_values=lambda r: {
                "embedding_hosted": r.vector,
                "model_id_hosted": r.model_id,
                "dim_hosted": len(r.vector),
            },
            batch_size=batch_size,
            tier="hosted",
        )
    return done


async def migration_status(session: AsyncSession) -> dict[str, int]:
    """Backfill coverage for the current tenant. ``migrated`` is the always-on LOCAL tier;
    ``hosted`` is the optional hosted tier.

    ``stale`` counts rows that HAVE a local vector written by a model that is not the active
    one. They are counted in ``migrated`` as well, because they are embedded: the number is
    not a correction to that one, it is the answer to a different question. Without it this
    function reports a fully migrated corpus while the dense branch is ignoring part of it,
    which is exactly the state a deploy that widened the backfill's eligibility should be
    watched through. It falls to zero as the sweep converges."""
    total = (
        await session.execute(
            select(func.count()).select_from(MemoryBlob).where(MemoryBlob.text.is_not(None))
        )
    ).scalar_one()
    local_done = (
        await session.execute(
            select(func.count()).select_from(MemoryBlob).where(MemoryBlob.embedding.is_not(None))
        )
    ).scalar_one()
    hosted_done = (
        await session.execute(
            select(func.count())
            .select_from(MemoryBlob)
            .where(MemoryBlob.embedding_hosted.is_not(None))
        )
    ).scalar_one()
    settings = get_settings()
    active = getattr(get_embedder(), "model_id", None) or settings.embed_model
    stale = (
        await session.execute(
            select(func.count())
            .select_from(MemoryBlob)
            .where(
                MemoryBlob.embedding.is_not(None),
                MemoryBlob.model_id.is_distinct_from(active),
            )
        )
    ).scalar_one()
    return {
        "total": int(total),
        "stale": int(stale),
        "migrated": int(local_done),
        "pending": int(total) - int(local_done),
        "hosted": int(hosted_done),
    }
