"""WS-EVAL T6 (task af679753, nota 0cb0dda0 §1.5): external baselines and
paired ablations on the SAME ingested workspace and the SAME query set.

The protocol's answer to "home-made benchmark where the home system wins":
every number mycelium publishes on the native workload is paired, per query,
with (a) BM25/FTS out-of-the-box, (b) bare pgvector dense top-k, (c) a naive
fixed-chunk RAG — all reading the very blobs mycelium ingested — plus
mycelium's own ablations. Deltas are tested with McNemar on the paired
hit/miss discordants and cluster-bootstrap CIs on per-query reciprocal-rank
differences (clusters = fact_id), via ``eval_stats`` — no new statistics
here.

Ablation honesty: ``retrieve_with_meta`` exposes per-call knobs for humus
(``humus=False``) and the reranker (``rerank=True``) but NOT for switching
off the lexical or dense branches. Rather than adding pipeline knobs in an
eval task, ``dense_only`` and ``lexical_only`` are served by the direct
scoring paths (same expressions as the T2 hardness gate) and labelled
``proxy=True`` in every record and in the manifest: they measure the
channel, not the pipeline minus one branch. Real branch knobs are a freeze
(T5) decision.

Scoring conventions mirror ``eval_queries`` (the hardness gate): lexical =
``GREATEST(ts_rank(fts, simple), ts_rank(fts_lang, per-row config))``;
dense = cosine via negated ``max_inner_product`` over L2-normalized
vectors. Perimeter mirrors ``memory._project_pred`` (None = project IS
NULL), resolved per query exactly like the T3 scenario runner: the query's
``context_project`` if declared, else the gold unit's project.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from random import Random
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.embedder import EmbedSide
from mycelium_core.services import memory
from mycelium_core.services.eval_queries import QueryRecord
from mycelium_core.services.eval_stats import cluster_bootstrap, mcnemar_exact
from mycelium_core.services.eval_workspace import (
    IngestResult,
    Workspace,
    resolve_unit_blobs,
)

CHUNK_CHARS = 512

SYSTEM_MYCELIUM = "mycelium"
SYSTEM_MYCELIUM_HUMUS_OFF = "mycelium_humus_off"
SYSTEM_MYCELIUM_RERANK = "mycelium_rerank"
SYSTEM_BM25 = "bm25"
SYSTEM_DENSE = "dense_only"
SYSTEM_NAIVE_RAG = "naive_rag"

# The two direct-channel systems double as PROXY ablations (see module
# docstring): the label travels with every record so no downstream table
# can silently present them as pipeline ablations.
_PROXY_SYSTEMS = frozenset({SYSTEM_DENSE, SYSTEM_BM25})

_LEX_RANK_SQL = text(
    """
    SELECT id, GREATEST(
        ts_rank(fts, plainto_tsquery('simple', :q)),
        ts_rank(fts_lang, plainto_tsquery(fts_language::regconfig, :q))
    ) AS score
    FROM memory_blobs
    WHERE org_id = :org
      AND ((CAST(:proj AS uuid) IS NULL AND project_id IS NULL)
           OR project_id = CAST(:proj AS uuid))
    ORDER BY score DESC, id
    LIMIT :lim
    """
)

_DENSE_RANK_SQL = text(
    """
    SELECT id, -(embedding <#> CAST(:qvec AS vector)) AS score
    FROM memory_blobs
    WHERE org_id = :org
      AND embedding IS NOT NULL
      AND ((CAST(:proj AS uuid) IS NULL AND project_id IS NULL)
           OR project_id = CAST(:proj AS uuid))
    ORDER BY embedding <#> CAST(:qvec AS vector), id
    LIMIT :lim
    """
)


@dataclasses.dataclass(frozen=True)
class RankedUnit:
    unit_id: str
    score: float


@dataclasses.dataclass(frozen=True)
class SystemRun:
    """One system's pass over the query set, in the eval_report dialect
    (records carry the extra ``system``/``proxy`` fields, which the report
    loader ignores)."""

    system: str
    proxy: bool
    records: list[dict[str, Any]]
    skipped_non_note: int


async def build_blob_unit_map(
    session: AsyncSession, *, org_id: uuid.UUID, ingest: IngestResult
) -> dict[uuid.UUID, str]:
    """blob_id -> unit_id for every NOTE unit (task units carry no note
    index pointers; queries whose gold is a task unit are skipped and
    counted, consistently with the T2 hardness gate)."""
    out: dict[uuid.UUID, str] = {}
    for unit_id, meta in ingest.units.items():
        if meta.get("kind") != "note":
            continue
        for blob_id in await resolve_unit_blobs(
            session, org_id=org_id, note_id=uuid.UUID(meta["note_id"])
        ):
            out[blob_id] = unit_id
    return out


def resolve_query_project(
    record: QueryRecord, ws: Workspace, ingest: IngestResult
) -> uuid.UUID | None:
    """The retrieval perimeter for a query: its declared context project if
    any, else the first gold unit's project (same resolution as the T3
    scenario runner — reimplemented because core cannot import mycelium_mcp)."""
    if record.context_project:
        return ingest.project_ids.get(record.context_project)
    units_by_id = {u.unit_id: u for u in ws.units}
    for gold in record.gold_unit_ids:
        unit = units_by_id.get(gold)
        if unit is not None:
            return ingest.project_ids.get(unit.project)
    return None


def _dedup_to_units(
    rows: Sequence[tuple[Any, float]], blob2unit: dict[uuid.UUID, str], k: int
) -> list[RankedUnit]:
    """Collapse a ranked blob list to a ranked UNIT list (best blob wins),
    the same note-granularity dedup the protocol demands everywhere."""
    seen: set[str] = set()
    out: list[RankedUnit] = []
    for blob_id, score in rows:
        unit = blob2unit.get(blob_id)
        if unit is None or unit in seen:
            continue
        seen.add(unit)
        out.append(RankedUnit(unit_id=unit, score=float(score)))
        if len(out) >= k:
            break
    return out


async def rank_lexical(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None,
    query_text: str,
    k: int,
    blob2unit: dict[uuid.UUID, str],
) -> list[RankedUnit]:
    rows = (
        await session.execute(
            _LEX_RANK_SQL,
            {
                "org": str(org_id),
                "proj": str(project_id) if project_id else None,
                "q": query_text,
                "lim": k * 4,
            },
        )
    ).all()
    return _dedup_to_units([(r[0], r[1]) for r in rows], blob2unit, k)


async def rank_dense(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None,
    query_vector: Sequence[float],
    k: int,
    blob2unit: dict[uuid.UUID, str],
) -> list[RankedUnit]:
    rows = (
        await session.execute(
            _DENSE_RANK_SQL,
            {
                "org": str(org_id),
                "proj": str(project_id) if project_id else None,
                "qvec": "[" + ",".join(f"{x:.8f}" for x in query_vector) + "]",
                "lim": k * 4,
            },
        )
    ).all()
    return _dedup_to_units([(r[0], r[1]) for r in rows], blob2unit, k)


def chunk_units(ws: Workspace, *, chunk_chars: int = CHUNK_CHARS) -> list[tuple[str, str]]:
    """Deterministic fixed-size chunking of every unit text (the naive-RAG
    strawman): (unit_id, chunk_text) in unit order, no overlap."""
    chunks: list[tuple[str, str]] = []
    for unit in ws.units:
        body = unit.text
        for start in range(0, max(len(body), 1), chunk_chars):
            piece = body[start : start + chunk_chars]
            if piece.strip():
                chunks.append((unit.unit_id, piece))
    return chunks


class NaiveRagIndex:
    """In-memory chunk index: fixed chunks embedded once through the same
    embedder seam mycelium uses. Honest strawman: no metadata, no fusion,
    no graph — cosine over chunk vectors mapped back to unit ids."""

    def __init__(self, entries: list[tuple[str, list[float]]]) -> None:
        self._entries = entries

    @classmethod
    async def build(
        cls, ws: Workspace, embedder: Any, *, chunk_chars: int = CHUNK_CHARS
    ) -> NaiveRagIndex:
        entries: list[tuple[str, list[float]]] = []
        for unit_id, piece in chunk_units(ws, chunk_chars=chunk_chars):
            res = await embedder.embed(piece, side=EmbedSide.document)
            entries.append((unit_id, list(res.vector)))
        return cls(entries)

    def rank(self, query_vector: Sequence[float], *, k: int) -> list[RankedUnit]:
        best: dict[str, float] = {}
        for unit_id, vec in self._entries:
            score = sum(a * b for a, b in zip(query_vector, vec, strict=True))
            if score > best.get(unit_id, float("-inf")):
                best[unit_id] = score
        ranked = sorted(best.items(), key=lambda it: (-it[1], it[0]))[:k]
        return [RankedUnit(unit_id=u, score=s) for u, s in ranked]


def _tokens(texts: Sequence[str]) -> int:
    return sum(len(t) for t in texts) // 4


def _record(
    r: QueryRecord,
    *,
    system: str,
    rank: int | None,
    top_score: float,
    served_tokens: int,
    gold_tokens: int,
    abstained: bool,
) -> dict[str, Any]:
    return {
        "qid": r.query_id,
        "category": r.category,
        "fact_id": r.fact_id or r.query_id,
        "rank": rank,
        "impossible": r.expected_empty,
        "abstained": abstained,
        "top_score": round(top_score, 6),
        "served_tokens": served_tokens,
        "gold_tokens": gold_tokens,
        "event": False,
        "system": system,
        "proxy": system in _PROXY_SYSTEMS,
    }


def _score_ranked(
    r: QueryRecord, ranked: Sequence[RankedUnit], texts_by_unit: dict[str, str]
) -> tuple[int | None, float, int]:
    gold = set(r.gold_unit_ids)
    rank = next((i + 1 for i, hit in enumerate(ranked) if hit.unit_id in gold), None)
    top = ranked[0].score if ranked else 0.0
    served = _tokens([texts_by_unit.get(h.unit_id, "") for h in ranked])
    return rank, top, served


async def run_baselines(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    ws: Workspace,
    ingest: IngestResult,
    records: Sequence[QueryRecord],
    embedder: Any,
    k: int = 10,
    rerank: bool = False,
) -> list[SystemRun]:
    """Run every system over the same queries. Skips (counted per system)
    queries with no note-kind gold, mirroring the hardness gate. The
    ``abstained`` field is meaningful only for mycelium (baselines have no
    abstain mechanism; theirs is always False — documented, not hidden)."""
    texts_by_unit = {u.unit_id: u.text for u in ws.units}
    blob2unit = await build_blob_unit_map(session, org_id=org_id, ingest=ingest)
    note_units = {uid for uid, m in ingest.units.items() if m.get("kind") == "note"}
    rag_index = await NaiveRagIndex.build(ws, embedder)

    systems: list[str] = [SYSTEM_MYCELIUM, SYSTEM_MYCELIUM_HUMUS_OFF]
    if rerank:
        systems.append(SYSTEM_MYCELIUM_RERANK)
    systems += [SYSTEM_BM25, SYSTEM_DENSE, SYSTEM_NAIVE_RAG]

    recs_by_system: dict[str, list[dict[str, Any]]] = {name: [] for name in systems}
    skipped = 0

    for r in records:
        scoreable_gold = [u for u in r.gold_unit_ids if u in note_units]
        if not scoreable_gold and not r.expected_empty:
            skipped += 1
            continue
        project_id = resolve_query_project(r, ws, ingest)
        gold_tokens = _tokens([texts_by_unit.get(u, "") for u in r.gold_unit_ids])
        qvec = list((await embedder.embed(r.query_text, side=EmbedSide.query)).vector)

        for name in systems:
            if name.startswith("mycelium"):
                hits, meta = await memory.retrieve_with_meta(
                    session,
                    org_id=org_id,
                    actor_id=actor_id,
                    project_id=project_id,
                    query=r.query_text,
                    operation_id=f"t6-{name}-{r.query_id}",
                    limit=k,
                    embedder=embedder,
                    humus=False if name == SYSTEM_MYCELIUM_HUMUS_OFF else None,
                    rerank=name == SYSTEM_MYCELIUM_RERANK,
                    probe=True,
                )
                ranked = _dedup_to_units([(h.blob.id, h.rrf) for h in hits], blob2unit, k)
                rank, top, served = _score_ranked(r, ranked, texts_by_unit)
                rec = _record(
                    r,
                    system=name,
                    rank=rank,
                    top_score=top,
                    served_tokens=served,
                    gold_tokens=gold_tokens,
                    abstained=bool(meta.abstained),
                )
            else:
                if name == SYSTEM_BM25:
                    ranked = await rank_lexical(
                        session,
                        org_id=org_id,
                        project_id=project_id,
                        query_text=r.query_text,
                        k=k,
                        blob2unit=blob2unit,
                    )
                elif name == SYSTEM_DENSE:
                    ranked = await rank_dense(
                        session,
                        org_id=org_id,
                        project_id=project_id,
                        query_vector=qvec,
                        k=k,
                        blob2unit=blob2unit,
                    )
                else:
                    ranked = rag_index.rank(qvec, k=k)
                rank, top, served = _score_ranked(r, ranked, texts_by_unit)
                rec = _record(
                    r,
                    system=name,
                    rank=rank,
                    top_score=top,
                    served_tokens=served,
                    gold_tokens=gold_tokens,
                    abstained=len(ranked) == 0,
                )
            recs_by_system[name].append(rec)
    return [
        SystemRun(
            system=name,
            proxy=name in _PROXY_SYSTEMS,
            records=recs_by_system[name],
            skipped_non_note=skipped,
        )
        for name in systems
    ]


def _rr(rank: int | None) -> float:
    return 1.0 / rank if rank else 0.0


@dataclasses.dataclass(frozen=True)
class PairedStat:
    """One system's paired comparison against the base run.

    Every field is a number the table below prints. It exists as a value
    because a promotion rule that asks for significance needs the same
    McNemar p the reader sees, and computing it twice is how a printed
    table and a decision start disagreeing.
    """

    system: str
    proxy: bool
    #: Questions both runs answered (impossible ones excluded).
    n: int
    recall: float
    d_recall: float
    #: Exact McNemar on the discordant pairs. Two-sided, uncorrected: a
    #: round comparing several candidates against one base is making
    #: several comparisons, and the correction belongs to the round that
    #: knows how many, not to the statistic.
    mcnemar_p: float
    mrr: float
    d_mrr: float
    ci_lo: float
    ci_hi: float
    #: Per-category recall delta and support. A round can pre-register the
    #: category an improvement must show up in, which the pooled number
    #: cannot answer: a model can gain on one category and lose on another
    #: and still come out ahead overall.
    d_recall_by_category: Mapping[str, float] = dataclasses.field(default_factory=dict)
    n_by_category: Mapping[str, int] = dataclasses.field(default_factory=dict)


def paired_stats(
    runs: Sequence[SystemRun],
    *,
    base_system: str = SYSTEM_MYCELIUM,
    seed: int = 42,
    n_resamples: int = 2000,
) -> list[PairedStat]:
    """Base-vs-each-system paired comparison: delta recall@k with exact
    McNemar p on the discordant pairs, delta MRR with a cluster-bootstrap CI
    (clusters = fact_id). Scored queries only (impossible excluded).

    ``base_system`` names the run every other run is compared against. It
    defaults to the T6 baseline matrix's own base, and is a parameter so a
    different paired round (the embedder round compares candidates against
    the incumbent embedder, not against ``mycelium``) reuses this statistic
    instead of growing a second copy of it.

    The base run is included in the result, comparing against itself, so a
    caller rendering a table gets every row from one list."""
    by_system: dict[str, dict[str, dict[str, Any]]] = {
        run.system: {rec["qid"]: rec for rec in run.records if not rec["impossible"]}
        for run in runs
    }
    base = by_system.get(base_system)
    if not base:
        raise ValueError(f"paired_stats: base run {base_system!r} missing")
    out: list[PairedStat] = []
    for run in runs:
        recs = by_system[run.system]
        qids = sorted(set(recs) & set(base))
        if not qids:
            continue
        n = len(qids)
        hits = sum(1 for q in qids if recs[q]["rank"] is not None)
        recall = hits / n
        base_recall = sum(1 for q in qids if base[q]["rank"] is not None) / n
        # discordants: b = base hit & system miss, c = miss & hit
        b = sum(1 for q in qids if base[q]["rank"] is not None and recs[q]["rank"] is None)
        c = sum(1 for q in qids if base[q]["rank"] is None and recs[q]["rank"] is not None)
        p = mcnemar_exact(b, c)
        mrr = sum(_rr(recs[q]["rank"]) for q in qids) / n
        clustered = [
            (str(recs[q]["fact_id"]), _rr(recs[q]["rank"]) - _rr(base[q]["rank"])) for q in qids
        ]
        rng = Random(seed)  # noqa: S311 (resampling determinism, not crypto)
        lo, hi = cluster_bootstrap(clustered, rng=rng, n_resamples=n_resamples)
        d_mrr = sum(v for _, v in clustered) / n
        # The category comes from the records rather than from an argument:
        # it is the dataset's own label for the question, and the two runs
        # answer the SAME questions, so the base's label is the same label.
        per_cat_n: dict[str, int] = {}
        per_cat_hits: dict[str, float] = {}
        for q in qids:
            cat = str(recs[q].get("category") or "?")
            per_cat_n[cat] = per_cat_n.get(cat, 0) + 1
            delta = (1.0 if recs[q]["rank"] is not None else 0.0) - (
                1.0 if base[q]["rank"] is not None else 0.0
            )
            per_cat_hits[cat] = per_cat_hits.get(cat, 0.0) + delta
        out.append(
            PairedStat(
                system=run.system,
                proxy=run.proxy,
                n=n,
                recall=recall,
                d_recall=recall - base_recall,
                mcnemar_p=p,
                mrr=mrr,
                d_mrr=d_mrr,
                ci_lo=lo,
                ci_hi=hi,
                d_recall_by_category={k: v / per_cat_n[k] for k, v in per_cat_hits.items()},
                n_by_category=per_cat_n,
            )
        )
    return out


def paired_table(
    runs: Sequence[SystemRun],
    *,
    base_system: str = SYSTEM_MYCELIUM,
    seed: int = 42,
    n_resamples: int = 2000,
) -> str:
    """:func:`paired_stats`, rendered as a fixed-width table."""
    stats = paired_stats(runs, base_system=base_system, seed=seed, n_resamples=n_resamples)
    # Column width from the data: the T6 systems are short names, but a
    # paired round over embedders labels its runs with HuggingFace ids,
    # which are long enough to shift every column of a fixed-width table.
    w = max(20, *(len(r.system) + (8 if r.proxy else 0) for r in runs)) + 2
    lines = [
        f"{'system':<{w}}{'n':>5}  {'recall':>7} {'Δrecall':>8} {'McNemar p':>10}  "
        f"{'MRR':>6} {'ΔMRR':>7} {'Δ95%CI':>17}",
    ]
    for s in stats:
        label = s.system + (" (proxy)" if s.proxy else "")
        lines.append(
            f"{label:<{w}}{s.n:>5}  {s.recall:>7.3f} {s.d_recall:>+8.3f} "
            f"{s.mcnemar_p:>10.4f}  {s.mrr:>6.3f} {s.d_mrr:>+7.3f} "
            f"[{s.ci_lo:>+.3f},{s.ci_hi:>+.3f}]"
        )
    return "\n".join(lines)


def write_runs(runs: Sequence[SystemRun], out_dir: Path) -> dict[str, Any]:
    """One JSONL per system + a manifest with SHA256 and the proxy labels
    (the reproducibility artifact contract of §1.6)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"systems": {}}
    for run in runs:
        path = out_dir / f"records_{run.system}.jsonl"
        payload = "\n".join(json.dumps(rec, sort_keys=True) for rec in run.records) + "\n"
        path.write_text(payload, encoding="utf-8")
        manifest["systems"][run.system] = {
            "file": path.name,
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "n_records": len(run.records),
            "proxy": run.proxy,
            "skipped_non_note": run.skipped_non_note,
        }
    (out_dir / "manifest_baselines.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest
