"""Public memory benchmarks (LongMemEval / LOCOMO) over the REAL pipeline
(task cc4653bd). Ingest a dataset file into THROWAWAY orgs (one per
instance -- each LongMemEval entry carries its own haystack; each LOCOMO
sample is one conversation) and score retrieval with the same
``eval_offline.run_eval`` path as the CI gate.

Run against a disposable database (the script creates orgs and writes
blobs; never point it at prod):

    docker run -d --rm -e POSTGRES_USER=mycelium -e POSTGRES_PASSWORD=mycelium \
        -e POSTGRES_DB=mycelium -p 5436:5432 pgvector/pgvector:pg16
    # bootstrap_roles.sql + alembic upgrade head + db_harden, then:
    MYCELIUM_DATABASE_URL_SYNC=... MYCELIUM_DATABASE_URL=... \
        uv run python scripts/eval_public_bench.py \
        --dataset longmemeval --path ~/data/WORK/mycelium-bench/datasets/longmemeval_oracle.json \
        --limit-instances 20

Datasets are operator-provided (never committed; ~100MB for the full
variants): LongMemEval from huggingface ``xiaowu0162/longmemeval-cleaned``
(``longmemeval_oracle.json`` is the small evidence-only variant), LOCOMO from
github ``snap-research/locomo`` (``data/locomo10.json``).

HONESTY: the report prints the corpus ``model_id`` set. ``['none']`` means no
embedder was importable and every number is KEYWORD-ONLY retrieval; install
the worker's bge-m3 extra (sentence-transformers) for dense numbers. Scores
are retrieval recall@k / MRR + abstention correctness -- not judged QA.

EMBEDDER ROUND (``--embedders spec.json``): run the whole bench once per
candidate embedder and print one paired comparison against the incumbent.
Each candidate gets its own throwaway orgs, so no corpus ever mixes two
vector spaces. See ``eval_embedder_round`` for the spec format and for why
the query side is embedded through a separate override.

    MYCELIUM_DATABASE_URL_SYNC=... MYCELIUM_DATABASE_URL=... \
        uv run python scripts/eval_public_bench.py \
        --dataset locomo --path ~/.../locomo10.json --limit-instances 2 \
        --embedders docs/eval/embedder-round-2026-09.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import Callable
from pathlib import Path

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import Embedder, set_embedder_override
from mycelium_core.services import eval_embedder_round as round_
from mycelium_core.services import eval_public_bench as bench
from mycelium_core.services.auth import signup


def _load_instances(dataset: str, path: Path) -> list[bench.BenchInstance]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a JSON list of {dataset} entries")
    parse = (
        bench.parse_longmemeval_instance if dataset == "longmemeval" else bench.parse_locomo_sample
    )
    return [parse(obj) for obj in data]


def _factory(emb: Embedder | None) -> Callable[[], Embedder] | None:
    """``set_embedder_override`` takes a factory, not an instance."""
    return None if emb is None else lambda: emb


async def _run_pass(
    instances: list[bench.BenchInstance],
    args: argparse.Namespace,
    *,
    tag: str,
    doc_embedder: Embedder | None = None,
    query_embedder: Embedder | None = None,
) -> tuple[bench.BenchReport, tuple[bench.InstanceScore, ...]]:
    """One full pass over the dataset: a throwaway org per instance, ingest,
    then score.

    ``doc_embedder`` / ``query_embedder`` are installed around the two phases
    through ``set_embedder_override``. They are separate because an
    instruction-tuned model prefixes the query side only, and the ``Embedder``
    seam has no notion of side (see ``eval_embedder_round``). Leaving both
    None runs the configured embedder for everything, which is the plain bench.
    """
    scores: list[bench.InstanceScore] = []
    embedder_models: set[str] = set()
    # Built once, outside the loop: both are constant across instances, and a
    # closure created per iteration would capture the loop's binding rather
    # than its value (ruff B023) -- harmless while the override is consumed
    # immediately, a real bug the first time one is deferred.
    doc_factory = _factory(doc_embedder)
    query_factory = _factory(query_embedder)
    for i, instance in enumerate(instances):
        async with admin_session() as s:
            r = await signup(
                s,
                email=f"bench-{uuid.uuid4().hex[:10]}@example.test",
                password=uuid.uuid4().hex,  # throwaway org, never logged into
                org_name=f"BENCH-{args.dataset}-{i}",
            )
        org, user = r.org_id, r.user_id
        if doc_factory is not None:
            set_embedder_override(doc_factory)
        async with tenant_session(str(org), str(user)) as s:
            await bench.ingest_instance(s, org_id=org, actor_id=user, instance=instance)
        if query_factory is not None:
            set_embedder_override(query_factory)
        async with tenant_session(str(org), str(user)) as s:
            score = await bench.score_instance(
                s,
                org_id=org,
                actor_id=user,
                instance=instance,
                k=args.k,
                limit_questions=args.limit_questions,
                grader_min_rrf=args.grader_floor,
                grader_min_rerank_logit=args.grader_rerank_floor,
            )
            embedder_models.update(await bench.corpus_embedder_models(s, org_id=org))
        scores.append(score)
        print(
            f"  {tag}[{i + 1}/{len(instances)}] {instance.instance_id}: "
            f"{len(instance.units)} units, {len(score.results)} questions scored"
        )

    report = bench.aggregate(
        args.dataset,
        args.k,
        scores,
        sorted(embedder_models),
        grader_min_rrf=args.grader_floor,
        grader_min_rerank_logit=args.grader_rerank_floor,
    )
    return report, tuple(scores)


async def _run_round(
    instances: list[bench.BenchInstance],
    args: argparse.Namespace,
    spec: round_.RoundSpec,
) -> None:
    """Every candidate over the same dataset, then one paired comparison."""
    print(f"embedder round: {len(spec.candidates)} candidate(s), baseline={spec.baseline}")
    outcomes: list[round_.CandidateOutcome] = []
    try:
        for candidate in spec.candidates:
            print(f"\n--- {candidate.name} ({candidate.model}) ---")
            doc_emb, query_emb, inner = round_.build_embedders(candidate)
            report, scores = await _run_pass(
                instances,
                args,
                tag=f"{candidate.name} ",
                doc_embedder=doc_emb,
                query_embedder=query_emb,
            )
            outcomes.append(
                round_.CandidateOutcome(
                    candidate=candidate,
                    report=report,
                    scores=scores,
                    native_dim=inner.native_dim,
                )
            )
    finally:
        # Unconditional: a candidate that raises mid-round must not leave a
        # process-global override installed for whatever runs next.
        set_embedder_override(None)
    print()
    print(round_.render_round(outcomes, baseline=spec.baseline))


async def main() -> None:
    ap = argparse.ArgumentParser(description="LongMemEval/LOCOMO retrieval bench.")
    ap.add_argument("--dataset", required=True, choices=["longmemeval", "locomo"])
    ap.add_argument("--path", required=True, help="dataset JSON file (operator-provided)")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit-instances", type=int, default=None)
    ap.add_argument(
        "--limit-questions", type=int, default=None, help="per-instance question cap (LOCOMO)"
    )
    ap.add_argument(
        "--embedders",
        default=None,
        metavar="SPEC.json",
        help="run the embedder round: the whole bench once per candidate in the "
        "spec file, then one paired comparison against the incumbent "
        "(see mycelium_core.services.eval_embedder_round)",
    )
    ap.add_argument(
        "--grader-floor",
        type=float,
        default=None,
        help="per-call retrieval_grader_min_rrf override (floor sweep, task f0d24fdb); "
        "RRF-fused domain, clamp at 0.05",
    )
    ap.add_argument(
        "--grader-rerank-floor",
        type=float,
        default=None,
        help="per-call retrieval_grader_min_rerank_logit override (honest-abstain "
        "sweep, task f0d24fdb): a [0,1] relevance-probability floor on the reranker "
        "logit; only bites with --rerank / reranker enabled",
    )
    args = ap.parse_args()

    instances = _load_instances(args.dataset, Path(args.path))
    if args.limit_instances is not None:
        instances = instances[: args.limit_instances]
    print(f"{args.dataset}: {len(instances)} instance(s) from {args.path}")

    if args.embedders:
        await _run_round(instances, args, round_.load_round_spec(args.embedders))
        return

    report = await _run_pass(instances, args, tag="")
    print()
    print(report[0].render())


if __name__ == "__main__":
    asyncio.run(main())
