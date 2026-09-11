"""The embedder round: spec parsing, per-side prefixes, the pre-registered
promotion rule, and the paired shape.

The failure this guards is quiet: a round that runs an instruction-tuned
candidate without its prefix, or that keys two instances' questions on the
same id, produces a table that looks valid and answers a different question.
That is how the 2026-07-03 round reached "nessuna promozione" on three
models it had measured outside the configuration they were trained for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_core.embedder import EmbedResult
from mycelium_core.services import eval_public_bench as bench
from mycelium_core.services.eval_embedder_round import (
    CandidateOutcome,
    EmbedderCandidate,
    PrefixedEmbedder,
    load_round_spec,
    render_round,
    system_run,
    verdict,
)

QUERY_PREFIX = "Instruct: Given a web search query, retrieve relevant passages\nQuery:"


class RecordingEmbedder:
    """Records what it was asked to embed, which is the only thing the
    prefix wrapper is responsible for."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def embed(self, text: str) -> EmbedResult:
        self.seen.append(text)
        return EmbedResult(vector=[1.0, 0.0], model_id="recording/model", tokens=1)


def _score(instance_id: str, *, ranks: dict[str, int | None]) -> bench.InstanceScore:
    return bench.InstanceScore(
        instance_id=instance_id,
        results=tuple(
            bench.QuestionResult(
                qid=qid,
                category="single-hop",
                abstention=False,
                rank=rank,
                abstain_correct=None,
                served_tokens=100,
            )
            for qid, rank in ranks.items()
        ),
        skipped_no_evidence=(),
    )


def _outcome(
    label: str,
    scores: list[bench.InstanceScore],
    *,
    native_dim: int | None = 1024,
    prefix: str = "",
) -> CandidateOutcome:
    return CandidateOutcome(
        candidate=EmbedderCandidate(model=f"vendor/{label}", label=label, query_prefix=prefix),
        report=bench.aggregate("locomo", 10, scores, ["vendor/" + label]),
        scores=tuple(scores),
        native_dim=native_dim,
    )


# --- spec parsing -----------------------------------------------------------


def _write(tmp_path: Path, payload: object) -> Path:
    p = tmp_path / "round.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_a_spec_round_trips(tmp_path: Path) -> None:
    spec = load_round_spec(
        _write(
            tmp_path,
            {
                "baseline": "bge-m3",
                "notes": "perche' questi candidati",
                "candidates": [
                    {"label": "bge-m3", "model": "BAAI/bge-m3"},
                    {"label": "q", "model": "Qwen/x", "query_prefix": QUERY_PREFIX},
                ],
            },
        )
    )
    assert spec.baseline == "bge-m3"
    assert spec.notes.startswith("perche'")
    assert [c.name for c in spec.candidates] == ["bge-m3", "q"]
    assert spec.baseline_candidate().model == "BAAI/bge-m3"


def test_a_misspelled_prefix_key_is_refused_not_ignored(tmp_path: Path) -> None:
    """The whole point of the round: a candidate silently running WITHOUT
    its instruction prefix is the bug being corrected, so a typo in the key
    must stop the run rather than produce a plausible number."""
    path = _write(
        tmp_path,
        {"candidates": [{"model": "Qwen/x", "querry_prefix": QUERY_PREFIX}]},
    )
    with pytest.raises(ValueError, match="querry_prefix"):
        load_round_spec(path)


def test_an_unknown_top_level_key_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, {"candidates": [{"model": "m"}], "baselines": "m"})
    with pytest.raises(ValueError, match="baselines"):
        load_round_spec(path)


def test_a_baseline_outside_the_candidates_fails_before_the_run(tmp_path: Path) -> None:
    """Hours of encoding must not precede this error."""
    path = _write(tmp_path, {"baseline": "absent", "candidates": [{"model": "m"}]})
    with pytest.raises(ValueError, match="absent"):
        load_round_spec(path)


def test_the_first_candidate_is_the_default_baseline(tmp_path: Path) -> None:
    spec = load_round_spec(_write(tmp_path, {"candidates": [{"model": "a"}, {"model": "b"}]}))
    assert spec.baseline == "a"


# --- the prefix wrapper -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_prefix_is_prepended_verbatim() -> None:
    inner = RecordingEmbedder()
    await PrefixedEmbedder(inner=inner, prefix=QUERY_PREFIX).embed("dove sta il reranker")
    assert inner.seen == [f"{QUERY_PREFIX}dove sta il reranker"]


@pytest.mark.asyncio
async def test_an_empty_prefix_leaves_the_text_untouched() -> None:
    """The document side of a symmetric model must not be perturbed."""
    inner = RecordingEmbedder()
    await PrefixedEmbedder(inner=inner, prefix="").embed("un documento")
    assert inner.seen == ["un documento"]


@pytest.mark.asyncio
async def test_batching_prefixes_every_text() -> None:
    inner = RecordingEmbedder()
    out = await PrefixedEmbedder(inner=inner, prefix="p:").embed_batch(["a", "b"])
    assert inner.seen == ["p:a", "p:b"]
    assert len(out) == 2


@pytest.mark.asyncio
async def test_model_id_passes_through_so_the_vector_space_matches() -> None:
    """``SemanticDenseStage`` filters on ``model_id``: if the wrapper
    reported its own, a run's queries would never match its own corpus."""
    res = await PrefixedEmbedder(inner=RecordingEmbedder(), prefix="p:").embed("x")
    assert res.model_id == "recording/model"


def test_only_a_one_sided_prefix_counts_as_asymmetric() -> None:
    assert EmbedderCandidate(model="m", query_prefix="q:").asymmetric
    assert not EmbedderCandidate(model="m").asymmetric
    assert not EmbedderCandidate(model="m", query_prefix="p", document_prefix="p").asymmetric


# --- the pre-registered rule ------------------------------------------------


def test_promotion_needs_both_recall_and_mrr() -> None:
    base = bench.aggregate("locomo", 10, [_score("i1", ranks={"q1": 2, "q2": None})], ["base"])
    better = bench.aggregate("locomo", 10, [_score("i1", ranks={"q1": 1, "q2": 3})], ["cand"])
    v = verdict(base, better, label="cand")
    assert v.promote and v.d_recall > 0 and v.d_mrr > 0


def test_a_candidate_that_only_reorders_is_not_promoted() -> None:
    """Recall flat, MRR up: the reranker's signature, and by the July rule
    not a reason to swap the embedder."""
    base = bench.aggregate("locomo", 10, [_score("i1", ranks={"q1": 3, "q2": 4})], ["base"])
    reordered = bench.aggregate("locomo", 10, [_score("i1", ranks={"q1": 1, "q2": 2})], ["cand"])
    v = verdict(base, reordered, label="cand")
    assert not v.promote
    assert v.d_recall == 0 and v.d_mrr > 0
    assert "recall" in v.reason


def test_a_strictly_worse_candidate_names_both() -> None:
    base = bench.aggregate("locomo", 10, [_score("i1", ranks={"q1": 1, "q2": 2})], ["base"])
    worse = bench.aggregate("locomo", 10, [_score("i1", ranks={"q1": None, "q2": None})], ["cand"])
    assert verdict(base, worse, label="cand").reason == "neither improved"


# --- the paired shape -------------------------------------------------------


def test_question_ids_are_namespaced_by_instance() -> None:
    """LOCOMO reuses short question ids across conversations and
    ``paired_table`` keys on qid alone: without the namespace, instance 2
    would overwrite instance 1 and the comparison would silently run on a
    fraction of the questions."""
    run = system_run(
        _outcome("c", [_score("conv1", ranks={"q1": 1}), _score("conv2", ranks={"q1": 2})])
    )
    assert sorted(r["qid"] for r in run.records) == ["conv1:q1", "conv2:q1"]


def test_the_bootstrap_clusters_on_the_instance() -> None:
    """Questions inside one instance share a haystack, so they are not
    independent draws."""
    run = system_run(_outcome("c", [_score("conv1", ranks={"q1": 1, "q2": 2})]))
    assert {r["fact_id"] for r in run.records} == {"conv1"}


def test_the_round_renders_every_candidate_with_its_verdict() -> None:
    scores_base = [_score("i1", ranks={"q1": 3, "q2": None})]
    scores_cand = [_score("i1", ranks={"q1": 1, "q2": 2})]
    out = render_round(
        [
            _outcome("bge-m3", scores_base),
            _outcome("qwen3-emb-8B", scores_cand, native_dim=4096, prefix=QUERY_PREFIX),
        ],
        baseline="bge-m3",
    )
    assert "baseline=bge-m3" in out
    assert "BASELINE" in out and "PROMOTE" in out
    # The two labels a reader needs to interpret the numbers at all.
    assert "4096" in out  # ran truncated
    assert "query" in out  # embedded asymmetrically
    assert "McNemar" in out


def test_rendering_refuses_a_baseline_that_did_not_run() -> None:
    with pytest.raises(ValueError, match="bge-m3"):
        render_round([_outcome("other", [_score("i1", ranks={"q1": 1})])], baseline="bge-m3")
