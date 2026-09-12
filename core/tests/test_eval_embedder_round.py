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

from mycelium_core.config import get_settings
from mycelium_core.embed_dims import EMBED_DIM
from mycelium_core.embedder import EmbedSide, HostedEmbedder
from mycelium_core.services import eval_public_bench as bench
from mycelium_core.services.eval_baselines import PairedStat, paired_stats
from mycelium_core.services.eval_embedder_round import (
    CandidateOutcome,
    EmbedderCandidate,
    build_embedder,
    load_round_spec,
    render_round,
    system_run,
    verdict,
)

QUERY_PREFIX = "Instruct: Given a web search query, retrieve relevant passages\nQuery:"


def _score(
    instance_id: str,
    *,
    ranks: dict[str, int | None],
    categories: dict[str, str] | None = None,
) -> bench.InstanceScore:
    """``categories`` overrides the label per question; everything unnamed is
    ``single-hop``, the category the promotion rule is pre-registered on, so a
    test that does not care about categories still exercises the whole rule."""
    cats = categories or {}
    return bench.InstanceScore(
        instance_id=instance_id,
        results=tuple(
            bench.QuestionResult(
                qid=qid,
                category=cats.get(qid, "single-hop"),
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
    query_prompt: str | None = None,
) -> CandidateOutcome:
    return CandidateOutcome(
        candidate=EmbedderCandidate(model=f"vendor/{label}", label=label),
        report=bench.aggregate("locomo", 10, scores, ["vendor/" + label]),
        scores=tuple(scores),
        native_dim=native_dim,
        query_prompt=query_prompt,
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
                    {"label": "q", "model": "Qwen/x", "mrl": True, "revision": "abc123"},
                ],
            },
        )
    )
    assert spec.baseline == "bge-m3"
    assert spec.notes.startswith("perche'")
    assert [c.name for c in spec.candidates] == ["bge-m3", "q"]
    assert spec.baseline_candidate().model == "BAAI/bge-m3"
    assert spec.candidates[1].mrl and spec.candidates[1].revision == "abc123"


def test_a_misspelled_key_is_refused_not_ignored(tmp_path: Path) -> None:
    """A spec key that is silently dropped is how a round measures something
    other than what its author wrote down: ``revsion`` would pin nothing and
    still produce a plausible table."""
    path = _write(tmp_path, {"candidates": [{"model": "Qwen/x", "revsion": "abc123"}]})
    with pytest.raises(ValueError, match="revsion"):
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


# --- what a spec may say ----------------------------------------------------


def test_a_retired_prefix_key_stops_the_round(tmp_path: Path) -> None:
    """``query_prefix`` was where the instruction used to be declared, before
    the checkpoint became the source. A spec still carrying one was written
    against the old shape, and ignoring the key would run that candidate with
    whatever prefix its config happens to declare while its author believed
    they had chosen one. Loud, and early: the loader is the only place that
    still knows the key ever existed."""
    with pytest.raises(ValueError, match="query_prefix"):
        load_round_spec(
            _write(tmp_path, {"candidates": [{"model": "Qwen/x", "query_prefix": "Instruct:"}]})
        )


def test_the_mrl_claim_must_be_a_boolean(tmp_path: Path) -> None:
    """A string would be truthy whatever it said, including "false", and the
    candidate would run truncated on a claim nobody made."""
    with pytest.raises(ValueError, match="mrl"):
        load_round_spec(_write(tmp_path, {"candidates": [{"model": "Qwen/x", "mrl": "yes"}]}))


# --- the pre-registered rule ------------------------------------------------
#
# Three clauses, checked in order: July's (recall AND MRR both up), then the
# category the round is about, then significance. The tests below drive the
# rule through the REAL path -- two runs of per-question records, paired by
# ``paired_stats`` -- rather than through a hand-built statistic, because the
# clause that is easiest to get wrong is the one that depends on how the
# pairs are counted.


def _stat(
    base_ranks: dict[str, int | None],
    cand_ranks: dict[str, int | None],
    *,
    categories: dict[str, str] | None = None,
) -> PairedStat:
    runs = [
        system_run(_outcome(name, [_score("i1", ranks=ranks, categories=categories)]))
        for name, ranks in (("base", base_ranks), ("cand", cand_ranks))
    ]
    return {s.system: s for s in paired_stats(runs, base_system="base")}["cand"]


#: Seven questions the incumbent misses and the candidate finds, with none
#: the other way: exact McNemar puts that at p=0.0156, just under the
#: Bonferroni threshold for three candidates (0.05/3 = 0.0167). Seven rather
#: than a round ten because the boundary is where a rule is worth testing.
_SEVEN_WINS_BASE: dict[str, int | None] = {f"q{i}": None for i in range(7)}
_SEVEN_WINS_CAND: dict[str, int | None] = {f"q{i}": 1 for i in range(7)}


def test_a_significant_improvement_in_the_registered_category_promotes() -> None:
    v = verdict(_stat(_SEVEN_WINS_BASE, _SEVEN_WINS_CAND), label="cand", n_comparisons=3)
    assert v.promote
    assert v.d_recall > 0 and v.d_mrr > 0
    assert v.mcnemar_p < v.alpha
    assert v.d_recall_category is not None and v.d_recall_category > 0


def test_a_candidate_that_only_reorders_is_not_promoted() -> None:
    """Recall flat, MRR up: the reranker's signature, and not a reason to
    swap the embedder. This clause is July's, unchanged."""
    v = verdict(
        _stat({"q1": 3, "q2": 4}, {"q1": 1, "q2": 2}),
        label="cand",
        n_comparisons=3,
    )
    assert not v.promote
    assert v.d_recall == 0 and v.d_mrr > 0
    assert "recall" in v.reason


def test_a_strictly_worse_candidate_names_both() -> None:
    v = verdict(_stat({"q1": 1, "q2": 2}, {"q1": None, "q2": None}), label="cand", n_comparisons=3)
    assert v.reason == "neither improved"


def test_an_improvement_inside_the_noise_is_not_promoted() -> None:
    """The clause July did not have. Five wins and no losses is a clean
    direction and a 0.0625 p: by the old rule this promotes, and promotion
    means re-embedding the corpus."""
    base: dict[str, int | None] = {f"q{i}": None for i in range(5)}
    cand: dict[str, int | None] = {f"q{i}": 1 for i in range(5)}
    v = verdict(_stat(base, cand), label="cand", n_comparisons=3)
    assert not v.promote
    assert v.d_recall > 0 and v.d_mrr > 0  # July's clause is satisfied
    assert "noise" in v.reason and "McNemar" in v.reason


def test_the_threshold_follows_the_number_of_comparisons() -> None:
    """Bonferroni is a property of the ROUND, not of the candidate: the same
    evidence is enough when it is the only comparison and not enough when it
    is one of five. A threshold frozen at three candidates would stop being
    the one that was pre-registered as soon as a fourth was added."""
    base: dict[str, int | None] = {f"q{i}": None for i in range(6)}
    cand: dict[str, int | None] = {f"q{i}": 1 for i in range(6)}  # p = 0.031
    assert verdict(_stat(base, cand), label="cand", n_comparisons=1).promote
    assert not verdict(_stat(base, cand), label="cand", n_comparisons=5).promote


def test_a_gain_outside_the_registered_category_does_not_promote() -> None:
    """The round is about single-hop: July already attributed the multi-hop
    gap to the graph rather than to the embedder. A candidate that wins
    everything EXCEPT single-hop is evidence about something this round
    cannot act on, and the pooled row alone would have called it a win."""
    base: dict[str, int | None] = {f"q{i}": None for i in range(7)}
    cand: dict[str, int | None] = {f"q{i}": 1 for i in range(7)}
    base["s1"], cand["s1"] = 1, None  # the one single-hop question, lost
    cats = {f"q{i}": "multi-hop" for i in range(7)} | {"s1": "single-hop"}
    v = verdict(_stat(base, cand, categories=cats), label="cand", n_comparisons=3)
    assert not v.promote
    assert v.d_recall > 0 and "single-hop" in v.reason
    assert v.d_recall_category is not None and v.d_recall_category < 0


def test_a_dataset_without_the_category_cannot_satisfy_the_rule() -> None:
    """LongMemEval does not carry LOCOMO's labels. The rule is then not
    evaluable, which must read as "not evaluable" and never as a pass."""
    base: dict[str, int | None] = {f"q{i}": None for i in range(7)}
    cand: dict[str, int | None] = {f"q{i}": 1 for i in range(7)}
    cats = {f"q{i}": "temporal" for i in range(7)}
    v = verdict(_stat(base, cand, categories=cats), label="cand", n_comparisons=3)
    assert not v.promote
    assert v.d_recall_category is None
    assert "not evaluable" in v.reason


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
    # Seven clean wins: enough evidence to reach PROMOTE with one candidate
    # under the Bonferroni threshold, so the rendered table exercises both
    # marks rather than only the refusal.
    scores_base = [_score("i1", ranks=_SEVEN_WINS_BASE)]
    scores_cand = [_score("i1", ranks=_SEVEN_WINS_CAND)]
    out = render_round(
        [
            _outcome("bge-m3", scores_base),
            _outcome("qwen3-emb-8B", scores_cand, native_dim=4096, query_prompt=QUERY_PREFIX),
        ],
        baseline="bge-m3",
    )
    assert "baseline=bge-m3" in out
    assert "BASELINE" in out and "PROMOTE" in out
    # The two labels a reader needs to interpret the numbers at all.
    assert "4096" in out  # ran truncated
    assert "query" in out  # embedded asymmetrically
    assert "McNemar" in out
    # The rule the reader is being asked to trust, and the per-category
    # delta it turns on, both stated next to the verdict they produced.
    assert "Bonferroni" in out
    assert "single-hop" in out


def test_rendering_refuses_a_baseline_that_did_not_run() -> None:
    with pytest.raises(ValueError, match="bge-m3"):
        render_round([_outcome("other", [_score("i1", ranks={"q1": 1})])], baseline="bge-m3")


# --- the hosted arm ---------------------------------------------------------
#
# A model too large to sit in the backend pod can still be measured against
# the incumbent, by fetching its vectors over HTTP at the LOCAL fleet width.
# What must not happen is a hosted row that reads like a local one: the
# evidence behind it is weaker (no checkpoint to interrogate, no pin the run
# can enforce), and the spec keys enforce that difference.


def test_a_hosted_candidate_is_built_at_the_LOCAL_fleet_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The question is whether this model beats the incumbent in the column
    the incumbent occupies. Built at the hosted width it would compare a
    model and a column at once and answer neither."""
    monkeypatch.setenv("MYCELIUM_SCALEWAY_API_KEY", "sk-test")
    get_settings.cache_clear()
    try:
        emb = build_embedder(
            EmbedderCandidate(
                model="qwen3-embedding-8b",
                label="hosted-8B",
                provider="scaleway",
                mrl=True,
                query_prefix=QUERY_PREFIX,
            )
        )
        assert isinstance(emb, HostedEmbedder)
        assert emb._target_dim == EMBED_DIM
        assert emb.declared_prompt(EmbedSide.query) == QUERY_PREFIX
        assert emb.declared_prompt(EmbedSide.document) is None
        # Not knowable from an endpoint, and saying so is the point.
        assert emb.native_dim is None
    finally:
        get_settings.cache_clear()


def test_a_hosted_candidate_without_a_key_stops_the_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to the local default here would label the wrong model in
    the round's own table, which is worse than not running."""
    monkeypatch.setenv("MYCELIUM_SCALEWAY_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="SCALEWAY_API_KEY"):
            build_embedder(
                EmbedderCandidate(model="qwen3-embedding-8b", provider="scaleway", mrl=True)
            )
    finally:
        get_settings.cache_clear()


def test_a_hosted_candidate_must_declare_mrl_to_ask_for_a_narrower_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking an endpoint for fewer dims than the model's natural output is a
    truncation like any other, and degrades a non-MRL model just as
    silently. The local gate lives in LocalEmbedder; this is the same rule on
    the side where there is no checkpoint to check it against."""
    monkeypatch.setenv("MYCELIUM_SCALEWAY_API_KEY", "sk-test")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="MRL"):
            build_embedder(
                EmbedderCandidate(model="qwen3-embedding-8b", provider="scaleway", mrl=False)
            )
    finally:
        get_settings.cache_clear()


def test_prefixes_belong_to_hosted_candidates_only(tmp_path: Path) -> None:
    """On a local candidate the checkpoint already states the instruction, so
    a spec-level prefix is the copy that drifts. Refused, not merged."""
    with pytest.raises(ValueError, match="local candidate"):
        load_round_spec(
            _write(tmp_path, {"candidates": [{"model": "Qwen/x", "query_prefix": "Instruct:"}]})
        )


def test_a_pin_belongs_to_local_candidates_only(tmp_path: Path) -> None:
    """An endpoint serves whatever weights the provider deployed. A
    ``revision`` there would be a reproducibility claim the run cannot
    enforce, which is worse than no claim."""
    with pytest.raises(ValueError, match="scaleway candidate"):
        load_round_spec(
            _write(
                tmp_path,
                {"candidates": [{"model": "q", "provider": "scaleway", "revision": "abc123"}]},
            )
        )


def test_an_unknown_provider_stops_the_round(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="openai"):
        load_round_spec(
            _write(tmp_path, {"candidates": [{"model": "text-embedding-3", "provider": "openai"}]})
        )


def test_a_hosted_row_says_so_in_the_table() -> None:
    """A reader must not take a hosted row for a local one: it carries no
    native dim and its prefix was configured, not read."""
    scores_base = [_score("i1", ranks=_SEVEN_WINS_BASE)]
    scores_cand = [_score("i1", ranks=_SEVEN_WINS_CAND)]
    out = render_round(
        [
            _outcome("bge-m3", scores_base),
            CandidateOutcome(
                candidate=EmbedderCandidate(
                    model="qwen3-embedding-8b", label="hosted-8B", provider="scaleway", mrl=True
                ),
                report=bench.aggregate("locomo", 10, scores_cand, ["qwen3-embedding-8b"]),
                scores=tuple(scores_cand),
                native_dim=None,
                query_prompt=QUERY_PREFIX,
            ),
        ],
        baseline="bge-m3",
    )
    assert "HOSTED" in out and "scaleway" in out
    assert "?" in out  # the dim it cannot report
