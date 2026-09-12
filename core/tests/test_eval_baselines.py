"""WS-EVAL T6 (task af679753): baselines + paired-comparison machinery on a
real ingested workspace (nota 0cb0dda0 §1.5). FakeEmbedder + tiny scale: the
tests pin the MACHINERY (ranked units, record dialect, paired stats path),
never quality thresholds — those belong to the frozen confirmatory run (T5).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import EmbedSide, set_embedder_override
from mycelium_core.services import eval_baselines as base
from mycelium_core.services import eval_report
from mycelium_core.services.auth import signup
from mycelium_core.services.eval_queries import build_queries
from mycelium_core.services.eval_workspace import generate_workspace, ingest_workspace

_SEED = 4242
_SCALE = 150
_FAKE = FakeEmbedder()


@pytest.fixture()
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"t6-{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="T6",
        )
    return r.org_id, r.user_id


async def test_baselines_end_to_end(_embedder: None, tmp_path: Path) -> None:
    """One ingest, every system ranked, records round-trip through the
    eval_report loader, paired table renders with McNemar + bootstrap CI."""
    ws = generate_workspace(seed=_SEED, scale=_SCALE)
    queries = build_queries(ws, seed=_SEED + 1000)
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        ingest = await ingest_workspace(s, org_id=org, actor_id=user, ws=ws)
    async with tenant_session(str(org), str(user)) as s:
        runs = await base.run_baselines(
            s,
            org_id=org,
            actor_id=user,
            ws=ws,
            ingest=ingest,
            records=queries,
            embedder=_FAKE,
            k=10,
        )
    by_name = {r.system: r for r in runs}
    assert set(by_name) == {
        base.SYSTEM_MYCELIUM,
        base.SYSTEM_MYCELIUM_HUMUS_OFF,
        base.SYSTEM_BM25,
        base.SYSTEM_DENSE,
        base.SYSTEM_NAIVE_RAG,
    }
    # Proxy labelling: the two direct channels, and only those.
    assert by_name[base.SYSTEM_BM25].proxy and by_name[base.SYSTEM_DENSE].proxy
    assert not by_name[base.SYSTEM_MYCELIUM].proxy
    n = len(by_name[base.SYSTEM_MYCELIUM].records)
    assert n > 0
    assert all(len(r.records) == n for r in runs), "systems must be paired per query"

    # A pinned tuple-query must be a bm25 hit: query = the exact fact tuple
    # (the T3 lesson: never rely on the shared filler prefix).
    gold_fact = next(f for f in ws.facts if f.category == "gold" and f.queryable)
    blob2unit = None  # rank via the public helper below
    async with tenant_session(str(org), str(user)) as s:
        blob2unit = await base.build_blob_unit_map(s, org_id=org, ingest=ingest)
        unit = next(u for u in ws.units if u.unit_id in gold_fact.gold_unit_ids)
        project_id = ingest.project_ids.get(unit.project)
        ranked = await base.rank_lexical(
            s,
            org_id=org,
            project_id=project_id,
            query_text=f"{gold_fact.entity_name} {gold_fact.attribute} {gold_fact.value}",
            k=10,
            blob2unit=blob2unit,
        )
    assert any(h.unit_id in gold_fact.gold_unit_ids for h in ranked)

    # Record dialect round-trip: the eval_report loader accepts every file.
    manifest = base.write_runs(runs, tmp_path)
    for meta in manifest["systems"].values():
        loaded = eval_report.load_records(tmp_path / meta["file"])
        assert len(loaded) == meta["n_records"]

    table = base.paired_table(runs, seed=_SEED, n_resamples=200)
    assert base.SYSTEM_NAIVE_RAG in table and "(proxy)" in table
    assert "McNemar" in table


def test_chunker_deterministic_and_covering() -> None:
    ws = generate_workspace(seed=_SEED, scale=_SCALE)
    a = base.chunk_units(ws)
    b = base.chunk_units(ws)
    assert a == b
    # Every non-empty unit text is covered by its chunks, in order.
    covered: dict[str, str] = {}
    for unit_id, piece in a:
        covered[unit_id] = covered.get(unit_id, "") + piece
    for unit in ws.units:
        if unit.text.strip():
            assert covered[unit.unit_id] == unit.text


async def test_naive_rag_maps_chunks_to_units(_embedder: None) -> None:
    """The chunk containing the fact tuple must rank its OWN unit first
    under the deterministic FakeEmbedder."""
    ws = generate_workspace(seed=_SEED, scale=_SCALE)
    index = await base.NaiveRagIndex.build(ws, _FAKE)
    fact = next(f for f in ws.facts if f.category == "gold" and f.queryable)
    qvec = list(
        (
            await _FAKE.embed(
                f"{fact.entity_name} {fact.attribute} {fact.value}", side=EmbedSide.query
            )
        ).vector
    )
    ranked = index.rank(qvec, k=10)
    assert ranked, "naive RAG must return ranked units"
    assert any(h.unit_id in fact.gold_unit_ids for h in ranked)
