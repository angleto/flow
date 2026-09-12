"""Embedder round: the same public benchmark, run once per candidate
embedder, compared against the incumbent with a paired statistic.

Why this exists as its own module rather than a flag on the bench: a
comparison needs something the plain bench does not, and it is the reason
the 2026-07-03 round (nota 3276b266 §4) could not settle the question it
asked. **A three-point spread over a few hundred questions needs an
interval.** July compared 0.726 / 0.713 / 0.696 recall with no paired test,
which cannot distinguish "in the same band" from "worse". Every candidate
here answers the SAME questions, so the comparison is naturally paired:
``eval_baselines.paired_stats`` (exact McNemar on discordant hits,
cluster-bootstrap CI on ΔMRR, clusters = bench instance) applies directly
and is reused rather than re-implemented.

The OTHER reason July could not answer is no longer this module's problem,
and the history is worth keeping because it explains a shape that is now
absent. Instruction-tuned retrieval models (Qwen3-Embedding, E5-instruct)
are trained with an instruction prefix on the QUERY side only; run without
it they are measured outside the configuration they were trained for, which
is how July scored them. That was first corrected here, in the bench: a
different embedder installed around each phase, wrapping the model in a
prefix. It measured a shape production could not reproduce, so a winning
candidate would have had to be measured again after adoption, and it rested
on an undocumented discipline (the phases run in sequence, the override is
process-global) that no test held. The asymmetry now belongs to the
``Embedder`` seam itself (``EmbedSide``, ADR-0061), where production uses it
too, and the prefix is read from the checkpoint's own configuration rather
than written beside the model id. This module installs ONE embedder per
candidate and lets the seam carry the side.

The promotion rule was strengthened on 2026-09-12, BEFORE this round ran and
with July's numbers as the only ones in view. July pre-registered "promote
only on a net improvement in recall AND MRR", which has no notion of noise:
it promotes on +0.005 as readily as on +0.05, and the cost of a wrong
promotion is not symmetric with the cost of a wrong hold: adopting a local
embedder re-embeds the entire corpus (ADR-0030) and does not undo cleanly.
:func:`verdict` therefore also requires the delta to be distinguishable from
noise (exact McNemar under a Bonferroni-corrected alpha, because a round
comparing several candidates against one incumbent makes several
comparisons) and to appear in the category the round is actually about.

That category is SINGLE-HOP, and naming it is the other half of the
pre-registration. The recall gap is not uniform (July: single-hop 0.763,
multi-hop 0.712, open-domain 0.472) and July already concluded that the
multi-hop gap is a graph problem rather than an embedding one. A candidate
that moved only multi-hop would be evidence about something this round
cannot change.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mycelium_core.config import get_settings
from mycelium_core.embed_dims import EMBED_DIM, EMBED_DIM_HOSTED
from mycelium_core.embedder import EmbedResult, EmbedSide, HostedEmbedder, LocalEmbedder
from mycelium_core.services.eval_baselines import PairedStat, SystemRun, paired_stats, paired_table
from mycelium_core.services.eval_public_bench import BenchReport, InstanceScore

#: Hosted endpoints a candidate may name. A closed set rather than a free
#: string because an unrecognised provider must stop the round: the
#: alternative is a run that quietly measures the local default while its
#: table names something else.
PROVIDER_SCALEWAY = "scaleway"
KNOWN_PROVIDERS = frozenset({PROVIDER_SCALEWAY})


@runtime_checkable
class RoundEmbedder(Protocol):
    """What the round needs from a candidate, beyond embedding.

    Both implementations answer these, with different strengths, and the
    round prints both so a hosted row is never read as if it carried a local
    row's evidence. ``native_dim`` is what a checkpoint emitted before the
    fleet-dim coercion, and ``None`` from a hosted endpoint means "not
    knowable", never "not truncated".
    """

    async def embed(self, text: str, *, side: EmbedSide) -> EmbedResult: ...

    @property
    def native_dim(self) -> int | None: ...

    def declared_prompt(self, side: EmbedSide) -> str | None: ...


@dataclass(frozen=True)
class EmbedderCandidate:
    """One embedder under test, as declared in the round's spec file.

    A LOCAL candidate is a model id, a pin, and one claim. It carries no
    instruction prefix: the checkpoint declares its own in
    ``config_sentence_transformers.json`` and ``LocalEmbedder`` reads it
    there, so a spec file cannot state a prefix that disagrees with the
    model it names.

    ``mrl`` is the candidate's claim to Matryoshka training, and it is
    declared per candidate rather than read from the deployment because
    in a round the claim is evidence: a checkpoint wider than the fleet
    dim is truncated to reach it, and that is only principled for an MRL
    model. A wider candidate that does not declare it refuses to load,
    which is the intended outcome -- a number measured from a silently
    degraded model is worse than a missing row.

    ``provider`` names a HOSTED endpoint instead ("scaleway"), which is how
    a model too large to hold in the backend pod can still be measured
    against the incumbent: the vectors come over HTTP at the fleet width and
    every other part of the run -- the column, the model_id filter, the
    scoring path -- is the one production uses. The prefixes are declarable
    ONLY here, and only because there is no checkpoint to read: an
    ``/v1/embeddings`` endpoint serves a model without exposing its
    sentence-transformers configuration, so the instruction is knowable only
    from the model card. On a local candidate they are refused, because
    there the model already states it and a second copy would be the thing
    that drifts.

    ``revision`` does not apply to a hosted candidate and is refused there
    too: the endpoint serves whatever weights the provider has deployed, and
    a pin the run cannot enforce would be a reproducibility claim that is
    not true.
    """

    model: str
    label: str = ""
    mrl: bool = False
    revision: str = ""
    provider: str = ""
    query_prefix: str = ""
    document_prefix: str = ""

    @property
    def name(self) -> str:
        return self.label or self.model

    @property
    def prefixes(self) -> dict[EmbedSide, str]:
        return {EmbedSide.query: self.query_prefix, EmbedSide.document: self.document_prefix}


@dataclass(frozen=True)
class CandidateOutcome:
    """What one candidate's full pass produced."""

    candidate: EmbedderCandidate
    report: BenchReport
    scores: tuple[InstanceScore, ...]
    # Dimension the checkpoint emits before the fleet-dim coercion. None when
    # it could not be read (no model loaded); equal to the fleet dim when the
    # candidate ran untruncated.
    native_dim: int | None = None
    # The instruction prefix the checkpoint declared for the QUERY side, as
    # read from its own config. None for a symmetric model. Recorded rather
    # than assumed: "this model is instruction-tuned" is a claim about what
    # actually ran, and it is the difference between this round and July's.
    query_prompt: str | None = None


#: Family-wise error rate the round accepts across all of its
#: candidate-vs-incumbent comparisons, split by Bonferroni over however many
#: comparisons it actually makes. Bonferroni rather than a sharper correction
#: because the number of candidates is small and the reader has to be able to
#: recompute the threshold in their head.
PROMOTION_ALPHA = 0.05

#: The category an improvement must appear in for this round to mean what it
#: claims. LOCOMO's own label (``eval_public_bench.LOCOMO_CATEGORY_LABELS``).
PROMOTION_CATEGORY = "single-hop"


@dataclass(frozen=True)
class Verdict:
    """The pre-registered rule's mechanical answer for one candidate.

    Every number here comes from the PAIRED statistic rather than from the
    two bench reports, so that the delta being judged and the test guarding
    it are the same quantity over the same questions. The reports' own
    recall/MRR are printed beside it for continuity with July, and the two
    agree whenever both runs answered the whole question set.
    """

    label: str
    d_recall: float
    d_mrr: float
    promote: bool
    reason: str
    #: Exact McNemar p for this candidate against the incumbent, and the
    #: corrected threshold it was judged against.
    mcnemar_p: float = 1.0
    alpha: float = PROMOTION_ALPHA
    #: Recall delta restricted to :data:`PROMOTION_CATEGORY`, and how many
    #: questions carried that label. ``None`` when the round's dataset does
    #: not label categories at all.
    d_recall_category: float | None = None
    n_category: int = 0


@dataclass(frozen=True)
class RoundSpec:
    """A round's declared candidates and which of them is the incumbent.

    Kept as a file rather than CLI arguments because it is the artifact
    that records what was pre-registered before the run, and a shell history
    is not one. The optional ``notes`` carries WHY these candidates and not
    others, which is the half of a pre-registration that a bare list of
    model ids loses.
    """

    baseline: str
    candidates: tuple[EmbedderCandidate, ...] = field(default_factory=tuple)
    notes: str = ""

    def baseline_candidate(self) -> EmbedderCandidate:
        for c in self.candidates:
            if c.name == self.baseline:
                return c
        raise ValueError(
            f"round spec: baseline {self.baseline!r} is not among the candidates "
            f"({[c.name for c in self.candidates]})"
        )


def load_round_spec(path: str | Path) -> RoundSpec:
    """Parse a round spec file. Fails loudly on an unknown key and on a key
    that does not belong to the kind of candidate it appears on, because a
    spec key that is silently dropped is how a round measures something other
    than what its author wrote down: a ``query_prefix`` on a local candidate
    would run under whatever the checkpoint declares while its author
    believed they had chosen one, and a ``revision`` on a hosted candidate
    would be a pin the run cannot enforce."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object with 'baseline' and 'candidates'")
    unknown_top = set(raw) - {"baseline", "candidates", "notes"}
    if unknown_top:
        raise ValueError(f"{path}: unknown top-level key(s) {sorted(unknown_top)}")
    entries = raw.get("candidates")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 'candidates' must be a non-empty list")
    allowed = {"model", "label", "mrl", "revision", "provider", "query_prefix", "document_prefix"}
    #: Keys that only mean something on a hosted candidate, and the key that
    #: only means something on a local one.
    hosted_only = {"query_prefix", "document_prefix"}
    local_only = {"revision"}
    candidates: list[EmbedderCandidate] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise ValueError(f"{path}: candidate {i} is not an object")
        unknown = set(e) - allowed
        if unknown:
            raise ValueError(f"{path}: candidate {i} has unknown key(s) {sorted(unknown)}")
        if not e.get("model"):
            raise ValueError(f"{path}: candidate {i} has no 'model'")
        if "mrl" in e and not isinstance(e["mrl"], bool):
            # A string here would be truthy whatever it said, including
            # "false", and the candidate would run truncated on a claim
            # nobody made.
            raise ValueError(f"{path}: candidate {i} has a non-boolean 'mrl' ({e['mrl']!r})")
        provider = str(e.get("provider") or "")
        if provider and provider not in KNOWN_PROVIDERS:
            raise ValueError(
                f"{path}: candidate {i} names provider {provider!r}; "
                f"known providers are {sorted(KNOWN_PROVIDERS)}"
            )
        misplaced = (hosted_only if not provider else local_only) & set(e)
        if misplaced:
            where = "a local candidate" if not provider else f"a {provider} candidate"
            raise ValueError(
                f"{path}: candidate {i} is {where} and cannot carry {sorted(misplaced)}"
            )
        candidates.append(EmbedderCandidate(**e))
    baseline = raw.get("baseline") or candidates[0].name
    spec = RoundSpec(
        baseline=str(baseline),
        candidates=tuple(candidates),
        notes=str(raw.get("notes") or ""),
    )
    spec.baseline_candidate()  # fail here, not after hours of encoding
    return spec


def build_embedder(candidate: EmbedderCandidate) -> RoundEmbedder:
    """The one embedder that runs a candidate's whole pass.

    One rather than two: the query/document asymmetry is the seam's job now,
    so a checkpoint is loaded once and both phases of the bench share it.

    A hosted candidate is built at the LOCAL fleet width, not at the hosted
    one. That is the whole point of measuring it here: the question is
    whether this model beats the incumbent in the column the incumbent
    occupies, so the vectors must land in that column and go through the
    same scoring path. Using the hosted width instead would compare a model
    AND a column at once and answer neither question. The endpoint is asked
    for the width via ``dimensions`` (Matryoshka), which is why a hosted
    candidate carries the same ``mrl`` claim as a local one, and the claim
    is checked by a human against the model card rather than by the code:
    there is no checkpoint here to interrogate.
    """
    if not candidate.provider:
        return LocalEmbedder(
            candidate.model, mrl=candidate.mrl, revision=candidate.revision or None
        )
    if candidate.provider == PROVIDER_SCALEWAY:
        settings = get_settings()
        if not settings.scaleway_api_key:
            raise RuntimeError(
                f"candidate {candidate.name!r} runs on {PROVIDER_SCALEWAY} but "
                f"MYCELIUM_SCALEWAY_API_KEY is empty. A round that silently fell back to a "
                f"local model here would label the wrong model in its own table."
            )
        if not candidate.mrl and EMBED_DIM_HOSTED != EMBED_DIM:
            # The endpoint is being asked for fewer dims than the model's
            # natural output, which is a truncation like any other.
            raise RuntimeError(
                f"candidate {candidate.name!r} asks {PROVIDER_SCALEWAY} for {EMBED_DIM} dims "
                f"without declaring MRL. Requesting a narrower vector from a non-MRL model "
                f"degrades it exactly as truncating a local one does, and just as silently."
            )
        return HostedEmbedder(
            api_key=settings.scaleway_api_key,
            model=candidate.model,
            base_url=settings.scaleway_base_url,
            target_dim=EMBED_DIM,
            prefixes=candidate.prefixes,
        )
    raise ValueError(f"candidate {candidate.name!r}: unknown provider {candidate.provider!r}")


def system_run(outcome: CandidateOutcome) -> SystemRun:
    """One candidate's per-question results in the shape ``paired_table``
    consumes. ``fact_id`` is the bench instance: questions inside one
    instance share a haystack, so they are not independent and the
    bootstrap must resample instances, not questions."""
    records: list[dict[str, Any]] = []
    for score in outcome.scores:
        for r in score.results:
            records.append(
                {
                    # Question ids are unique within an instance but not
                    # necessarily across them (LOCOMO reuses short ids), and
                    # paired_table keys on qid alone.
                    "qid": f"{score.instance_id}:{r.qid}",
                    "category": r.category,
                    "fact_id": score.instance_id,
                    "rank": r.rank,
                    "impossible": r.abstention,
                    "abstained": bool(r.abstain_correct),
                    "served_tokens": r.served_tokens,
                    "system": outcome.candidate.name,
                    "proxy": False,
                }
            )
    return SystemRun(
        system=outcome.candidate.name, proxy=False, records=records, skipped_non_note=0
    )


def verdict(stat: PairedStat, *, label: str, n_comparisons: int) -> Verdict:
    """The pre-registered rule, in the order its clauses are checked.

    July's clause (2026-07-03), unchanged: recall@k AND MRR both up. Two
    clauses added 2026-09-12, before the first run of this round:

    * the improvement shows up in :data:`PROMOTION_CATEGORY`, the category
      an embedder can actually move;
    * the pooled delta is distinguishable from noise -- exact McNemar under
      ``PROMOTION_ALPHA / n_comparisons``.

    ``n_comparisons`` is how many candidates the round weighs against the
    incumbent, and it is an argument rather than a constant because the
    correction has to match the round that was actually run: adding a fourth
    candidate makes the evidence each one needs stronger, and a threshold
    frozen at three would quietly stop being the one that was registered.

    A verdict is mechanical and it is NOT the decision. Promotion means a
    re-embedding of the corpus, and that is Angelo's call on evidence this
    function only summarises."""
    alpha = PROMOTION_ALPHA / max(1, n_comparisons)
    d_cat = stat.d_recall_by_category.get(PROMOTION_CATEGORY)
    n_cat = stat.n_by_category.get(PROMOTION_CATEGORY, 0)

    def answer(*, promote: bool, reason: str) -> Verdict:
        return Verdict(
            label=label,
            d_recall=stat.d_recall,
            d_mrr=stat.d_mrr,
            promote=promote,
            reason=reason,
            mcnemar_p=stat.mcnemar_p,
            alpha=alpha,
            d_recall_category=d_cat,
            n_category=n_cat,
        )

    if stat.d_recall <= 0 and stat.d_mrr <= 0:
        return answer(promote=False, reason="neither improved")
    if stat.d_recall <= 0 or stat.d_mrr <= 0:
        worse = "recall" if stat.d_recall <= 0 else "MRR"
        return answer(promote=False, reason=f"{worse} did not improve")
    if d_cat is None:
        # Not a failure of the candidate: the round was run on a dataset
        # that does not carry the label the rule names, so the rule cannot
        # be evaluated and must not be reported as satisfied.
        return answer(promote=False, reason=f"no {PROMOTION_CATEGORY} questions: not evaluable")
    if d_cat <= 0:
        return answer(
            promote=False,
            reason=f"{PROMOTION_CATEGORY} did not improve ({d_cat:+.3f} over {n_cat} q)",
        )
    if stat.mcnemar_p >= alpha:
        return answer(
            promote=False,
            reason=f"inside the noise (McNemar p={stat.mcnemar_p:.4f} >= {alpha:.4f})",
        )
    return answer(
        promote=True,
        reason=f"recall, MRR and {PROMOTION_CATEGORY} up; p={stat.mcnemar_p:.4f} < {alpha:.4f}",
    )


def render_round(outcomes: Sequence[CandidateOutcome], *, baseline: str) -> str:
    """The round's whole answer: one row per candidate, the paired statistic
    against the incumbent, and the mechanical verdict."""
    if not outcomes:
        return "embedder round: no candidate ran"
    by_name = {o.candidate.name: o for o in outcomes}
    if baseline not in by_name:
        raise ValueError(f"render_round: baseline {baseline!r} did not produce an outcome")
    base = by_name[baseline]
    k = base.report.k
    w = max(20, *(len(o.candidate.name) for o in outcomes)) + 2

    runs = [system_run(o) for o in outcomes]
    stats = {s.system: s for s in paired_stats(runs, base_system=baseline)}
    # The incumbent's own row is a comparison with itself, not a comparison
    # the correction has to pay for.
    n_comparisons = sum(1 for o in outcomes if o.candidate.name != baseline)

    lines = [
        f"EMBEDDER ROUND  dataset={base.report.dataset}  k={k}  "
        f"instances={base.report.n_instances}  scored={base.report.n_scored}  "
        f"baseline={baseline}",
        "",
        f"{'candidate':<{w}}{'dim':>6} {'trunc':>6} {'prefix':>7}  {'recall@' + str(k):>9} "
        f"{'MRR':>7} {'tok/query':>10}  {'verdict':<10} reason",
    ]
    for o in outcomes:
        stat = stats.get(o.candidate.name)
        v = (
            verdict(stat, label=o.candidate.name, n_comparisons=n_comparisons)
            if stat is not None
            else Verdict(o.candidate.name, 0.0, 0.0, False, "no scored questions in common")
        )
        dim = str(o.native_dim) if o.native_dim is not None else "?"
        # Whether this row paid a truncation, not whether it was allowed
        # to: the claim is what let the model load, the width is what says
        # it was used.
        truncated = o.native_dim is not None and o.native_dim > EMBED_DIM
        trunc = f"->{EMBED_DIM}" if truncated else "-"
        # What the CHECKPOINT declared, not what the spec asked for: this
        # column is the one that says whether the instruction-tuned model
        # actually ran instruction-tuned, which is the question July's round
        # answered wrongly without knowing it.
        pref = "query" if o.query_prompt else "-"
        mark = "BASELINE" if o.candidate.name == baseline else ("PROMOTE" if v.promote else "no")
        reason = "" if o.candidate.name == baseline else v.reason
        lines.append(
            f"{o.candidate.name:<{w}}{dim:>6} {trunc:>6} {pref:>7}  {o.report.recall_at_k:>9.3f} "
            f"{o.report.mrr:>7.3f} {o.report.tokens_per_query:>10.0f}  {mark:<10} {reason}"
        )

    lines += [
        "",
        f"Paired against {baseline} (exact McNemar on discordant hits; "
        "cluster-bootstrap CI on ΔMRR, clusters = bench instance):",
        paired_table(runs, base_system=baseline),
        "",
        f"Promotion rule: recall@{k} and MRR both up, AND {PROMOTION_CATEGORY} recall up,",
        f"AND McNemar p < {PROMOTION_ALPHA} / {n_comparisons} comparisons = "
        f"{PROMOTION_ALPHA / max(1, n_comparisons):.4f} (Bonferroni).",
        f"Per-{PROMOTION_CATEGORY} delta, which the pooled row above does not show:",
    ]
    for o in outcomes:
        if o.candidate.name == baseline:
            continue
        stat = stats.get(o.candidate.name)
        d_cat = stat.d_recall_by_category.get(PROMOTION_CATEGORY) if stat else None
        n_cat = stat.n_by_category.get(PROMOTION_CATEGORY, 0) if stat else 0
        shown = f"{d_cat:+.3f}" if d_cat is not None else "n/a"
        lines.append(f"  {o.candidate.name:<{w}}{shown:>8}  over {n_cat} questions")

    lines += [
        "",
        f"A row marked ->{EMBED_DIM} ran TRUNCATED (Matryoshka), which its spec entry",
        "declared the checkpoint trained for; an undeclared wider model does not load.",
        "A row marked prefix=query embedded queries and documents differently, using the",
        "prefix the checkpoint's own config declares. Production embeds the same way",
        "(EmbedSide), so this is the shape a promoted candidate would actually ship in.",
    ]
    hosted = [o.candidate for o in outcomes if o.candidate.provider]
    if hosted:
        lines.append("")
        lines.append(
            "Ran on a HOSTED endpoint, vectors fetched over HTTP at the fleet width: "
            + ", ".join(f"{c.name} ({c.provider})" for c in hosted)
        )
        lines.append(
            "Their dim column reads '?' because an endpoint does not report what the model"
        )
        lines.append(
            "emits before the width it was asked for, and their prefix is configured from the"
        )
        lines.append("model card rather than read from a checkpoint.")
    prompts = {o.candidate.name: o.query_prompt for o in outcomes if o.query_prompt}
    if prompts:
        lines.append("")
        lines.append("Query prefixes actually applied:")
        for name, prompt in prompts.items():
            lines.append(f"  {name:<{w}}{prompt!r}")
    return "\n".join(lines)
