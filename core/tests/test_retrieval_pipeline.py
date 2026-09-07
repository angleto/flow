"""Unit tests for the retrieval pipeline executor + canonical stages
that don't need a DB session (fusion, ordering, limit, grader-min).
The DB-bound stages (lexical, semantic, access-counter) are exercised
by the existing ``test_memory_redesign`` + ``test_search_unified``
integration suites.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import pytest

from mycelium_core.services.memory import (
    _HUMUS_RRF_BOOST,
    _LEXICAL_EXACT_WEIGHT,
    _LEXICAL_STEM_WEIGHT,
    _RRF_K,
    _SEMANTIC_RRF_WEIGHT,
)
from mycelium_core.services.retrieval import (
    Candidate,
    RetrievalContext,
    RetrievalPipeline,
    Stage,
    merge_candidates,
)
from mycelium_core.services.retrieval.stages import (
    GraderMinStage,
    LimitStage,
    OrderingStage,
    RRFFusionStage,
)


@dataclass
class _FakeAddStage(Stage):
    """Drops a fixed candidate list into the pipeline, simulating a
    branch (lexical/semantic) without touching the DB."""

    name: str
    candidates: list[Candidate]

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        return merge_candidates(candidates, self.candidates)


def _cand(*, blob_id: uuid.UUID | None = None, stage: str, rank: int) -> Candidate:
    return Candidate(
        blob_id=blob_id or uuid.uuid4(),
        scores_by_stage={stage: float(rank)},
    )


def _ctx_stub() -> RetrievalContext:
    # The non-DB stages don't touch session/predicates; pass throwaway
    # values typed correctly.
    from sqlalchemy import true as sql_true

    return RetrievalContext(
        session=None,  # type: ignore[arg-type]
        org_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        project_id=None,
        operation_id="t",
        embedder=None,  # type: ignore[arg-type]
        project_pred=sql_true(),
        tag_clauses=(),
        query_embedding=None,
    )


async def test_pipeline_runs_stages_in_order() -> None:
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    pipeline = RetrievalPipeline(
        stages=[
            _FakeAddStage(
                name="lex",
                candidates=[
                    Candidate(blob_id=id_a, scores_by_stage={"lex": 1.0}),
                    Candidate(blob_id=id_b, scores_by_stage={"lex": 2.0}),
                ],
            ),
            _FakeAddStage(
                name="sem",
                candidates=[
                    Candidate(blob_id=id_b, scores_by_stage={"sem": 1.0}),
                    Candidate(blob_id=id_a, scores_by_stage={"sem": 2.0}),
                ],
            ),
            RRFFusionStage(k=60),
        ]
    )
    out = await pipeline.run("q", _ctx_stub())
    by_id = {c.blob_id: c for c in out}
    # RRF score = 1/(60+lex_rank) + 1/(60+sem_rank). Both pairs sum to the same
    # value (1/61 + 1/62) so order at this point is undefined; the OrderingStage
    # would later tie-break on created_at/id (see other test).
    expected = 1.0 / 61 + 1.0 / 62
    # Both candidates carry both per-branch ranks after merge; fusion ALSO
    # surfaces the fused score under the "rrf" key (observability, audit A-4).
    assert by_id[id_a].scores_by_stage["lex"] == 1.0
    assert by_id[id_a].scores_by_stage["sem"] == 2.0
    assert by_id[id_a].scores_by_stage["rrf"] == pytest.approx(expected)
    assert by_id[id_b].scores_by_stage["lex"] == 2.0
    assert by_id[id_b].scores_by_stage["sem"] == 1.0
    assert by_id[id_b].scores_by_stage["rrf"] == pytest.approx(expected)
    assert by_id[id_a].score == pytest.approx(expected)
    assert by_id[id_b].score == pytest.approx(expected)


async def test_ordering_tie_breaks_on_created_at_then_id() -> None:
    older = uuid.UUID("00000000-0000-0000-0000-000000000001")
    newer = uuid.UUID("00000000-0000-0000-0000-000000000002")
    c1 = Candidate(
        blob_id=newer,
        score=0.5,
        created_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
    )
    c2 = Candidate(
        blob_id=older,
        score=0.5,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    pipeline = RetrievalPipeline(stages=[OrderingStage()])
    out = await pipeline.run("q", _ctx_stub())  # empty start
    # Re-inject via fake stage path
    out = [c1, c2]
    out = await OrderingStage().run("q", _ctx_stub(), out)
    assert [c.blob_id for c in out] == [older, newer]


async def test_grader_min_drops_weak_top() -> None:
    weak = Candidate(blob_id=uuid.uuid4(), score=0.01)
    pipeline = RetrievalPipeline(stages=[GraderMinStage(min_score=0.05)])
    out = await pipeline.run("q", _ctx_stub())
    out = [weak]
    out = await GraderMinStage(min_score=0.05).run("q", _ctx_stub(), out)
    assert out == []


async def test_grader_min_keeps_strong_top() -> None:
    strong = Candidate(blob_id=uuid.uuid4(), score=0.5)
    other = Candidate(blob_id=uuid.uuid4(), score=0.1)
    out = await GraderMinStage(min_score=0.05).run("q", _ctx_stub(), [strong, other])
    assert len(out) == 2


async def test_grader_min_records_abstain_on_ctx() -> None:
    # WS-B1: a deliberate abstain (empty result because the top hit is below
    # the floor) must leave a marker so RetrievalMeta can tell it apart from a
    # genuinely empty index.
    ctx = _ctx_stub()
    out = await GraderMinStage(min_score=0.05).run(
        "q", ctx, [Candidate(blob_id=uuid.uuid4(), score=0.01)]
    )
    assert out == []
    assert ctx.extras.get("grader_abstained") is True


async def test_grader_min_no_abstain_marker_when_kept() -> None:
    ctx = _ctx_stub()
    out = await GraderMinStage(min_score=0.05).run(
        "q", ctx, [Candidate(blob_id=uuid.uuid4(), score=0.5)]
    )
    assert len(out) == 1
    assert "grader_abstained" not in ctx.extras


async def test_limit_truncates() -> None:
    cands = [Candidate(blob_id=uuid.uuid4(), score=1.0 - i * 0.1) for i in range(10)]
    out = await LimitStage(k=3).run("q", _ctx_stub(), cands)
    assert len(out) == 3
    assert out == cands[:3]


def test_merge_candidates_accumulates_per_stage_scores() -> None:
    bid = uuid.uuid4()
    existing = [Candidate(blob_id=bid, scores_by_stage={"lex": 1.0})]
    incoming = [Candidate(blob_id=bid, scores_by_stage={"sem": 2.0})]
    merged = merge_candidates(existing, incoming)
    assert len(merged) == 1
    assert merged[0].scores_by_stage == {"lex": 1.0, "sem": 2.0}


def test_merge_candidates_appends_new_ids() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    existing = [Candidate(blob_id=a, scores_by_stage={"lex": 1.0})]
    incoming = [Candidate(blob_id=b, scores_by_stage={"sem": 1.0})]
    merged = merge_candidates(existing, incoming)
    assert [c.blob_id for c in merged] == [a, b]


def test_merge_candidates_propagates_humus_provenance() -> None:
    """ADR-0034: a blob surfaced by BOTH a live branch and the humus source
    keeps the humus marker (the note carries the flag), so the leaf icon and
    the cap still see it."""
    bid = uuid.uuid4()
    existing = [Candidate(blob_id=bid, scores_by_stage={"lexical_exact": 1.0})]
    incoming = [Candidate(blob_id=bid, scores_by_stage={"humus": 1.0}, provenance="humus")]
    merged = merge_candidates(existing, incoming)
    assert len(merged) == 1
    assert merged[0].provenance == "humus"
    assert merged[0].scores_by_stage == {"lexical_exact": 1.0, "humus": 1.0}


async def test_humus_branch_is_a_small_boost_not_an_override() -> None:
    """ADR-0034: humus is fused on the low precision tier (weight 0.2),
    so a humus atom nudges above an equivalent live note but never
    outranks an EXACT lexical match (weight 1.0). This keeps the boost
    'small' and the fused scale low enough not to trip the relative floor."""
    exact_live = Candidate(blob_id=uuid.uuid4(), scores_by_stage={"lexical_exact": 1.0})
    humus_semantic = Candidate(
        blob_id=uuid.uuid4(), scores_by_stage={"semantic": 1.0, "humus": 1.0}
    )
    out = await RRFFusionStage(
        k=60, weights={"lexical_exact": 1.0, "semantic": 0.2, "humus": 0.2}
    ).run("q", _ctx_stub(), [exact_live, humus_semantic])
    by_id = {c.blob_id: c.score for c in out}
    assert by_id[exact_live.blob_id] == pytest.approx(1.0 / 61)
    assert by_id[humus_semantic.blob_id] == pytest.approx(0.2 / 61 + 0.2 / 61)
    # The exact live match still wins despite the humus boost.
    assert by_id[exact_live.blob_id] > by_id[humus_semantic.blob_id]


async def test_humus_cap_limits_slots_and_keeps_live() -> None:
    """ADR-0034 hard cap: at most floor(limit*ratio) humus candidates kept
    (the most relevant, since this runs after ordering); every live
    candidate kept; freed slots fall to live ranked just below."""
    from mycelium_core.services.retrieval.stages import HumusCapStage

    humus = [
        Candidate(blob_id=uuid.uuid4(), score=1.0 - i * 0.01, provenance="humus") for i in range(5)
    ]
    live = [Candidate(blob_id=uuid.uuid4(), score=0.5 - i * 0.01) for i in range(5)]
    out = await HumusCapStage(ratio=0.3, limit=10).run("q", _ctx_stub(), humus + live)
    kept_humus = [c for c in out if c.provenance == "humus"]
    assert kept_humus == humus[:3]  # floor(10*0.3) most-relevant humus
    assert [c for c in out if c.provenance != "humus"] == live  # all live kept


async def test_humus_cap_zero_budget_drops_all_humus() -> None:
    """A small limit whose 30% floors to 0 drops humus entirely (hard cap)."""
    from mycelium_core.services.retrieval.stages import HumusCapStage

    humus = Candidate(blob_id=uuid.uuid4(), score=1.0, provenance="humus")
    live = Candidate(blob_id=uuid.uuid4(), score=0.5)
    out = await HumusCapStage(ratio=0.3, limit=2).run("q", _ctx_stub(), [humus, live])
    assert out == [live]


def test_semantic_stage_keep_gate() -> None:
    """``SemanticDenseStage._keep`` gates on cosine = -distance. Floor 0
    is a no-op (keeps everything, even negative cosine); a positive floor
    keeps only neighbours at/above it."""
    from mycelium_core.services.retrieval.stages.semantic import SemanticDenseStage

    off = SemanticDenseStage(min_similarity=0.0)
    # distance = -cosine. Floor off keeps every row, including a
    # slightly-negative cosine (distance +0.1).
    assert off._keep(-0.9) is True
    assert off._keep(0.1) is True

    gated = SemanticDenseStage(min_similarity=0.5)
    assert gated._keep(-0.6) is True  # cosine 0.6 >= 0.5
    assert gated._keep(-0.5) is True  # cosine 0.5 == floor
    assert gated._keep(-0.3) is False  # cosine 0.3 < 0.5
    assert gated._keep(0.0) is False  # cosine 0.0 < 0.5


def test_semantic_stage_warns_when_floor_nukes_all(caplog) -> None:
    """A floor above the model's achievable cosine band rejects every
    kNN row and silently disables the dense branch. ``_kept`` must fail
    LOUD (warning naming the floor + best cosine), not no-op invisibly --
    the regression that shipped 0.8 against bge-m3's ~0.35-0.65 band."""
    import logging

    from mycelium_core.services.retrieval.stages.semantic import SemanticDenseStage

    # rows are (blob_id, distance); distance = -cosine. Best cosine here 0.63.
    rows = [(uuid.uuid4(), -0.63), (uuid.uuid4(), -0.51), (uuid.uuid4(), -0.40)]

    nuked = SemanticDenseStage(min_similarity=0.8)
    with caplog.at_level(logging.WARNING):
        kept = nuked._kept(rows, "semantic")
    assert kept == []
    assert any("rejected all" in r.message and "0.800" in r.message for r in caplog.records)
    assert any("0.630" in r.message for r in caplog.records)  # best cosine surfaced

    # A floor inside the band keeps the strong neighbours and stays silent.
    caplog.clear()
    ok = SemanticDenseStage(min_similarity=0.4)
    with caplog.at_level(logging.WARNING):
        kept = ok._kept(rows, "semantic")
    assert len(kept) == 3  # 0.63, 0.51, 0.40 all >= 0.4
    assert not caplog.records

    # Floor off (0.0): never warns even on an empty fetch.
    caplog.clear()
    off = SemanticDenseStage(min_similarity=0.0)
    with caplog.at_level(logging.WARNING):
        assert off._kept([], "semantic") == []
    assert not caplog.records


async def test_relative_floor_cuts_low_tail() -> None:
    """RelativeFloorStage drops candidates below ``ratio * top``; a flat
    profile (all near the top) is untouched; ratio 0 disables."""
    from mycelium_core.services.retrieval.stages import RelativeFloorStage

    top = Candidate(blob_id=uuid.uuid4(), score=0.020)
    mid = Candidate(blob_id=uuid.uuid4(), score=0.016)
    tail = Candidate(blob_id=uuid.uuid4(), score=0.005)  # 0.25*top
    out = await RelativeFloorStage(ratio=0.4).run("q", _ctx_stub(), [top, mid, tail])
    ids = {c.blob_id for c in out}
    assert top.blob_id in ids
    assert mid.blob_id in ids  # 0.8*top >= 0.4*top
    assert tail.blob_id not in ids  # 0.25*top < 0.4*top

    out = await RelativeFloorStage(ratio=0.0).run("q", _ctx_stub(), [top, mid, tail])
    assert len(out) == 3


async def test_weighted_rrf_lexical_beats_semantic_only() -> None:
    """With lexical weight 1.0 and semantic 0.3, a lexical-only hit
    outscores a semantic-only hit at the same rank, and a both-branch hit
    wins outright."""
    lex_only = Candidate(blob_id=uuid.uuid4(), scores_by_stage={"lexical": 1.0})
    sem_only = Candidate(blob_id=uuid.uuid4(), scores_by_stage={"semantic": 1.0})
    both = Candidate(blob_id=uuid.uuid4(), scores_by_stage={"lexical": 1.0, "semantic": 1.0})
    out = await RRFFusionStage(k=60, weights={"lexical": 1.0, "semantic": 0.3}).run(
        "q", _ctx_stub(), [lex_only, sem_only, both]
    )
    by_id = {c.blob_id: c.score for c in out}
    assert by_id[lex_only.blob_id] == pytest.approx(1.0 / 61)
    assert by_id[sem_only.blob_id] == pytest.approx(0.3 / 61)
    assert by_id[both.blob_id] == pytest.approx(1.0 / 61 + 0.3 / 61)
    assert by_id[lex_only.blob_id] > by_id[sem_only.blob_id]


# --------------------------------------------------------------- production weights
#
# The tests above fuse with illustrative weights (1.0/0.3). Nothing pinned the
# table the system actually ships, and that table decides every score any
# caller sees: a change to it is invisible in review and silent in production.


def _production_fusion() -> RRFFusionStage:
    """The fusion exactly as ``memory.retrieve`` assembles it."""
    return RRFFusionStage(
        k=_RRF_K,
        weights={
            "lexical_exact": _LEXICAL_EXACT_WEIGHT,
            "lexical_stem": _LEXICAL_STEM_WEIGHT,
            "semantic": _SEMANTIC_RRF_WEIGHT,
            "semantic_hosted": _SEMANTIC_RRF_WEIGHT,
            "humus": _HUMUS_RRF_BOOST,
        },
    )


def test_production_weight_table_has_not_moved() -> None:
    """The literals, on purpose. An accidental edit fails here; a deliberate
    one has to come through this line, which is the prompt to run ``make eval``
    and say what moved."""
    assert _RRF_K == 60
    assert _LEXICAL_EXACT_WEIGHT == 1.0
    assert _LEXICAL_STEM_WEIGHT == 0.2
    assert _SEMANTIC_RRF_WEIGHT == 0.2
    assert _HUMUS_RRF_BOOST == 0.2


async def test_fused_score_is_exactly_the_weighted_rank_sum() -> None:
    """Reproduces two values observed live against the deployed server on
    2026-09-07, from the branch ranks alone: 0.02289793759915389 for a hit
    the exact-lexical branch found at rank 1 (plus stem at 2, semantic at 1),
    and 0.003278688524590164 for a hit only the dense branch reached at
    rank 1. An operator reading the second as 'weak match' is reading a
    weight divided by a rank."""
    three_branch = Candidate(
        blob_id=uuid.uuid4(),
        scores_by_stage={"lexical_exact": 1.0, "lexical_stem": 2.0, "semantic": 1.0},
    )
    dense_only = Candidate(blob_id=uuid.uuid4(), scores_by_stage={"semantic": 1.0})
    out = await _production_fusion().run("q", _ctx_stub(), [three_branch, dense_only])
    by_id = {c.blob_id: c for c in out}

    assert by_id[three_branch.blob_id].score == pytest.approx(0.02289793759915389)
    assert by_id[dense_only.blob_id].score == pytest.approx(0.003278688524590164)
    # The fused value is also republished under "rrf", which is what a caller
    # reading scores_by_stage compares its branch ranks against.
    assert by_id[dense_only.blob_id].scores_by_stage["rrf"] == pytest.approx(
        by_id[dense_only.blob_id].score
    )


async def test_dense_only_score_is_a_function_of_rank_and_nothing_else() -> None:
    """The finding this breakdown exists to make visible. RRF fuses by RANK,
    so the cosine is discarded once the ordering is decided: a dense-only hit
    scores 0.2/(60+rank) whether its cosine was 0.63 or 0.36. Two consequences
    a caller must not get wrong: the fused score is not a confidence, and a
    flat page of 0.0033 values is not evidence that the embedder did badly.
    There is deliberately no cosine anywhere in this test, because there is
    none anywhere in the fusion."""
    ranks = [float(r) for r in range(1, 11)]
    cands = [Candidate(blob_id=uuid.uuid4(), scores_by_stage={"semantic": r}) for r in ranks]
    out = await _production_fusion().run("q", _ctx_stub(), cands)
    by_id = {c.blob_id: c.score for c in out}
    for c, r in zip(cands, ranks, strict=True):
        assert by_id[c.blob_id] == pytest.approx(_SEMANTIC_RRF_WEIGHT / (_RRF_K + r))
    # Top to bottom of a ten-row page the spread is 70/61, under 15%, whatever
    # the cosines were: too flat to rank on, which is why an honest abstention
    # needs the cross-encoder logit and not this number.
    values = sorted(by_id.values(), reverse=True)
    assert values[0] / values[-1] == pytest.approx(70 / 61)
    assert values[0] / values[-1] < 1.15
