"""Two-tier embedding backfill + per-org hosted embedder (task 5276207e).

The local tier (bge-m3, ``embedding``) is always written; the hosted tier
(``embedding_hosted`` halfvec) is per-org. Tests inject in-memory fakes
via the override seams (no real model / network) and exercise the
DB-bound write + backfill round-trip + the fail-closed key probe.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from _fake_embedder import FakeEmbedder
from httpx import Response
from sqlalchemy import select, update

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import (
    EmbedResult,
    set_embedder_override,
    set_hosted_embedder_override,
)
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services import embedder_resolver, memory
from mycelium_core.services import embedding_migration as svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


class FakeHostedEmbedder:
    """Deterministic 4000-dim unit vector (the hosted fleet dim)."""

    model_id = "fake-hosted"

    async def embed(self, text: str) -> EmbedResult:
        dim = get_settings().embed_dim_hosted
        vec = [0.0] * dim
        vec[len(text) % dim] = 1.0
        return EmbedResult(vector=vec, model_id=self.model_id, tokens=max(1, len(text.split())))


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    yield
    set_embedder_override(None)
    set_hosted_embedder_override(None)


async def test_local_write_then_hosted_backfill() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EMB")
    org, user = a.org_id, a.user_id
    settings = get_settings()

    # Local-only write (no hosted embedder yet): embedding populated at the
    # local dim, embedding_hosted NULL.
    async with tenant_session(str(org), str(user)) as s:
        blob = await memory.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="hello world embeddings",
            operation_id="op-write",
        )
        row = (
            await s.execute(
                select(MemoryBlob).where(MemoryBlob.id == blob.id, MemoryBlob.org_id == org)
            )
        ).scalar_one()
        assert row.embedding is not None and len(row.embedding) == settings.embed_dim
        assert row.embedding_hosted is None

    # Enable a hosted embedder (fake 4000d) and run the backfill: the
    # hosted tier is now populated, the local tier untouched.
    set_hosted_embedder_override(FakeHostedEmbedder)
    async with tenant_session(str(org), str(user)) as s:
        touched = await svc.run_embedding_backfill(s, org, batch_size=50)
        assert touched >= 1
        row = (
            await s.execute(
                select(MemoryBlob).where(MemoryBlob.id == blob.id, MemoryBlob.org_id == org)
            )
        ).scalar_one()
        assert row.embedding_hosted is not None
        # halfvec reads back as a pgvector HalfVector value object.
        assert len(row.embedding_hosted.to_list()) == settings.embed_dim_hosted
        assert row.model_id_hosted == "fake-hosted"

        status = await svc.migration_status(s)
        assert status["migrated"] >= 1 and status["hosted"] >= 1


@respx.mock
async def test_set_org_embedder_provider_probe_rejects_wrong_dim() -> None:
    # The candidate model returns a 1024-d vector -> below the hosted dim,
    # so the fail-closed probe rejects it (nothing persisted active).
    respx.post("https://api.scaleway.ai/v1/embeddings").mock(
        return_value=Response(
            200, json={"data": [{"embedding": [0.1] * 1024}], "usage": {"total_tokens": 1}}
        )
    )
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EMB")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(DomainError) as exc:
            await embedder_resolver.set_org_embedder_provider(
                s,
                org_id=org,
                actor_id=user,
                provider="scaleway",
                model="qwen3-embedding-8b",
                api_key="scw-bad",
            )
        assert exc.value.code is MessageCode.PROVIDER_KEY_INVALID
        assert await embedder_resolver.get_org_embedder_provider(s, org) is None


@respx.mock
async def test_set_org_embedder_provider_probe_accepts_correct_dim() -> None:
    dim = get_settings().embed_dim_hosted
    respx.post("https://api.scaleway.ai/v1/embeddings").mock(
        return_value=Response(
            200, json={"data": [{"embedding": [0.1] * dim}], "usage": {"total_tokens": 1}}
        )
    )
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EMB")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        row = await embedder_resolver.set_org_embedder_provider(
            s,
            org_id=org,
            actor_id=user,
            provider="scaleway",
            model="qwen3-embedding-8b",
            api_key="scw-good",
        )
        assert row.provider == "scaleway"
        assert row.api_key_ciphertext  # stored (encrypted)
        resolved = await embedder_resolver.resolve_hosted_embedder(s, org)
        assert resolved is not None


async def test_migration_status_returns_counts() -> None:
    """migration_status SELECTs total + local + hosted + stale counters.

    ``stale`` is deliberately not subtracted from ``migrated``: a row embedded by a
    superseded model IS migrated, and is also being skipped by the dense branch. Both
    statements are true and the caller needs both, so the numbers here overlap on purpose
    (60 migrated, 7 of them stale) rather than partitioning."""
    session = AsyncMock()
    total, local_done, hosted_done, stale = (MagicMock() for _ in range(4))
    total.scalar_one.return_value = 100
    local_done.scalar_one.return_value = 60
    hosted_done.scalar_one.return_value = 12
    stale.scalar_one.return_value = 7
    session.execute = AsyncMock(side_effect=[total, local_done, hosted_done, stale])
    out = await svc.migration_status(session)
    assert out == {"total": 100, "migrated": 60, "pending": 40, "hosted": 12, "stale": 7}


# --- the column is one vector space (ADR-0030) ------------------------------------------
#
# Selecting on IS NULL alone converged only a dim change, because that nulls the column. A
# swap to a different model of the SAME dimension left the old vectors in place forever, with
# nothing to re-embed them and nothing to say so, while the kNN went on comparing them against
# queries from the new model.


class OtherLocalEmbedder(FakeEmbedder):  # type: ignore[misc]
    """The same vectors under a different name: a model swap that keeps the dimension."""

    model_id = "fake-embed-v2"


async def test_backfill_reclaims_a_row_left_by_a_previous_model() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EMBSWAP")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        blob = await memory.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body="a row written before the swap",
            operation_id="op-swap",
        )
        row = (await s.execute(select(MemoryBlob).where(MemoryBlob.id == blob.id))).scalar_one()
        assert row.model_id == FakeEmbedder.model_id
        assert row.embedding is not None

    # The swap: the active embedder now stamps a different name. The row's vector is intact
    # and its dimension unchanged, so nothing about it looks wrong.
    set_embedder_override(OtherLocalEmbedder)
    async with tenant_session(str(org), str(user)) as s:
        touched = await svc.run_embedding_backfill(s, org, batch_size=50)
        assert touched >= 1, "a row from the previous model was not eligible for backfill"
        row = (await s.execute(select(MemoryBlob).where(MemoryBlob.id == blob.id))).scalar_one()
        assert row.model_id == OtherLocalEmbedder.model_id

    # And it converges: a second sweep finds nothing left to do, so the predicate is not
    # simply re-embedding the corpus on every pass.
    async with tenant_session(str(org), str(user)) as s:
        assert await svc.run_embedding_backfill(s, org, batch_size=50) == 0


async def test_dense_retrieval_ignores_rows_from_another_model() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EMBMIX")
    org, user = a.org_id, a.user_id
    phrase = "borogoves mimsy outgrabe"

    async with tenant_session(str(org), str(user)) as s:
        stale = await memory.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body=phrase,
            operation_id="op-stale",
        )
        # Same text, so the two rows are identical dense neighbours of the query and only the
        # model id can separate them. Anything the filter lets through, it lets through on
        # merit rather than because the other row was a worse match.
        fresh = await memory.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            text_body=phrase,
            operation_id="op-fresh",
        )
        await s.execute(
            update(MemoryBlob).where(MemoryBlob.id == stale.id).values(model_id="fake-embed-v0")
        )

    async with tenant_session(str(org), str(user)) as s:
        hits, meta = await memory.retrieve_with_meta(
            s,
            org_id=org,
            actor_id=user,
            project_id=None,
            query=phrase,
            limit=10,
            operation_id="op-mixed-read",
        )
    assert meta.query_embedded
    ids = {h.blob.id for h in hits}
    assert fresh.id in ids
    from_dense = {h.blob.id for h in hits if "semantic" in h.scores_by_stage}
    assert stale.id not in from_dense, (
        "a row embedded by another model was ranked as a dense neighbour of a query "
        "it has no relation to"
    )
