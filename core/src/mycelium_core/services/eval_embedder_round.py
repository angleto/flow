"""Embedder round: the same public benchmark, run once per candidate
embedder, compared against the incumbent with a paired statistic.

Why this exists as its own module rather than a flag on the bench: an
embedder comparison has two requirements the plain bench does not have,
and both of them are the reasons the 2026-07-03 round (nota 3276b266 §4)
could not settle the question it asked.

1. **Query and document must be embedded differently.** Instruction-tuned
   retrieval models (Qwen3-Embedding, E5-instruct) are trained with an
   instruction prefix on the QUERY side only; run without it they are
   being measured outside the configuration they were trained for, which
   is how the July round scored them. ``Embedder`` is symmetric
   (``embed(text)``, no notion of side), so the prefix cannot be expressed
   through it. The round instead installs a DIFFERENT embedder for each
   phase of the bench (ingest, then score) via ``set_embedder_override``,
   which is exact because the bench is already phase-separated. Production
   cannot reproduce this today: adopting an instruction-prefixed model
   means giving the ``Embedder`` seam a query/document asymmetry, and that
   cost belongs in the evidence for adopting one.

2. **A three-point spread over a few hundred questions needs an interval.**
   July compared 0.726 / 0.713 / 0.696 recall with no paired test, which
   cannot distinguish "in the same band" from "worse". Every candidate here
   answers the SAME questions, so the comparison is naturally paired:
   ``eval_baselines.paired_table`` (exact McNemar on discordant hits,
   cluster-bootstrap CI on ΔMRR, clusters = bench instance) applies
   directly and is reused rather than re-implemented.

The promotion rule is deliberately NOT strengthened here. July pre-registered
"promote only on a net improvement in recall AND MRR" and :func:`verdict`
implements exactly that; the paired statistics are reported ALONGSIDE it so
a delta inside the noise is visible. Changing the rule is a decision to take
before a run, never after seeing one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mycelium_core import embedder as embedder_mod
from mycelium_core.embedder import Embedder, EmbedResult, LocalEmbedder
from mycelium_core.services.eval_baselines import SystemRun, paired_table
from mycelium_core.services.eval_public_bench import BenchReport, InstanceScore


@dataclass(frozen=True)
class EmbedderCandidate:
    """One embedder under test, as declared in the round's spec file.

    ``query_prefix`` / ``document_prefix`` are prepended verbatim (no
    separator is added: the model cards specify their own, usually ending
    in a newline). Empty prefixes make this a plain model swap, which is
    the right shape for a symmetric model like bge-m3.
    """

    model: str
    label: str = ""
    query_prefix: str = ""
    document_prefix: str = ""

    @property
    def name(self) -> str:
        return self.label or self.model

    @property
    def asymmetric(self) -> bool:
        """True when the candidate embeds queries and documents differently,
        i.e. when adopting it would require the seam change described in this
        module's docstring."""
        return self.query_prefix != self.document_prefix


@dataclass
class PrefixedEmbedder:
    """Prepends a fixed instruction prefix to every text it embeds.

    Bench-only, and one instance is one SIDE of the comparison: the round
    installs the document-prefixed instance for ingest and the
    query-prefixed one for scoring. ``model_id`` passes through unchanged,
    so the blobs a run writes and the queries that search them stay in the
    same vector space (``SemanticDenseStage`` filters on it).
    """

    inner: Embedder
    prefix: str

    async def embed(self, text: str) -> EmbedResult:
        return await self.inner.embed(f"{self.prefix}{text}" if self.prefix else text)

    async def embed_batch(self, texts: list[str]) -> list[EmbedResult]:
        """Forwarded so the ingest phase keeps the batched path (the
        module-level ``embedder.embed_batch`` helper detects this method
        structurally; without it a round degrades to one encode per chunk).
        That helper is also what does the forwarding, so the "batched if the
        inner supports it" rule lives in one place."""
        prefixed = [f"{self.prefix}{t}" if self.prefix else t for t in texts]
        return await embedder_mod.embed_batch(self.inner, prefixed)


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


@dataclass(frozen=True)
class Verdict:
    """The pre-registered rule's mechanical answer for one candidate."""

    label: str
    d_recall: float
    d_mrr: float
    promote: bool
    reason: str


@dataclass(frozen=True)
class RoundSpec:
    """A round's declared candidates and which of them is the incumbent.

    Kept as a file rather than CLI arguments for two reasons: prefixes carry
    newlines that do not survive a shell round-trip intact, and the spec is
    the artifact that records what was pre-registered before the run. The
    optional ``notes`` carries WHY these candidates and not others, which is
    the half of a pre-registration that a bare list of model ids loses.
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
    """Parse a round spec file. Fails loudly on an unknown key: a typo in
    ``query_prefix`` would otherwise run the candidate WITHOUT its prefix and
    produce a number that looks valid and answers a different question."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object with 'baseline' and 'candidates'")
    unknown_top = set(raw) - {"baseline", "candidates", "notes"}
    if unknown_top:
        raise ValueError(f"{path}: unknown top-level key(s) {sorted(unknown_top)}")
    entries = raw.get("candidates")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 'candidates' must be a non-empty list")
    allowed = {"model", "label", "query_prefix", "document_prefix"}
    candidates: list[EmbedderCandidate] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise ValueError(f"{path}: candidate {i} is not an object")
        unknown = set(e) - allowed
        if unknown:
            raise ValueError(f"{path}: candidate {i} has unknown key(s) {sorted(unknown)}")
        if not e.get("model"):
            raise ValueError(f"{path}: candidate {i} has no 'model'")
        candidates.append(EmbedderCandidate(**e))
    baseline = raw.get("baseline") or candidates[0].name
    spec = RoundSpec(
        baseline=str(baseline),
        candidates=tuple(candidates),
        notes=str(raw.get("notes") or ""),
    )
    spec.baseline_candidate()  # fail here, not after hours of encoding
    return spec


def build_embedders(candidate: EmbedderCandidate) -> tuple[Embedder, Embedder, LocalEmbedder]:
    """``(document_side, query_side, shared_model)`` for one candidate.

    Both sides wrap the SAME :class:`LocalEmbedder`, so the checkpoint is
    loaded and held in memory once per candidate rather than once per phase.
    The third element is that shared instance, returned so the caller can
    read :attr:`LocalEmbedder.native_dim` after the first encode.
    """
    inner = LocalEmbedder(candidate.model)
    return (
        PrefixedEmbedder(inner=inner, prefix=candidate.document_prefix),
        PrefixedEmbedder(inner=inner, prefix=candidate.query_prefix),
        inner,
    )


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


def verdict(baseline: BenchReport, candidate: BenchReport, *, label: str) -> Verdict:
    """The rule pre-registered on 2026-07-03: promote only on a net
    improvement in BOTH recall@k and MRR. Implemented literally, including
    its weakness (it does not ask whether the improvement is distinguishable
    from noise) -- the paired table beside it is what shows that, and
    strengthening a rule after seeing the numbers it judges is how a
    pre-registration stops being one."""
    d_recall = candidate.recall_at_k - baseline.recall_at_k
    d_mrr = candidate.mrr - baseline.mrr
    if d_recall > 0 and d_mrr > 0:
        return Verdict(label, d_recall, d_mrr, True, "recall and MRR both up")
    if d_recall <= 0 and d_mrr <= 0:
        return Verdict(label, d_recall, d_mrr, False, "neither improved")
    worse = "recall" if d_recall <= 0 else "MRR"
    return Verdict(label, d_recall, d_mrr, False, f"{worse} did not improve")


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

    lines = [
        f"EMBEDDER ROUND  dataset={base.report.dataset}  k={k}  "
        f"instances={base.report.n_instances}  scored={base.report.n_scored}  "
        f"baseline={baseline}",
        "",
        f"{'candidate':<{w}}{'dim':>6} {'prefix':>7}  {'recall@' + str(k):>9} "
        f"{'MRR':>7} {'tok/query':>10}  {'verdict':<10} reason",
    ]
    for o in outcomes:
        v = verdict(base.report, o.report, label=o.candidate.name)
        dim = str(o.native_dim) if o.native_dim is not None else "?"
        pref = "query" if o.candidate.asymmetric else "-"
        mark = "BASELINE" if o.candidate.name == baseline else ("PROMOTE" if v.promote else "no")
        reason = "" if o.candidate.name == baseline else v.reason
        lines.append(
            f"{o.candidate.name:<{w}}{dim:>6} {pref:>7}  {o.report.recall_at_k:>9.3f} "
            f"{o.report.mrr:>7.3f} {o.report.tokens_per_query:>10.0f}  {mark:<10} {reason}"
        )

    lines += [
        "",
        f"Paired against {baseline} (exact McNemar on discordant hits; "
        "cluster-bootstrap CI on ΔMRR, clusters = bench instance):",
        paired_table([system_run(o) for o in outcomes], base_system=baseline),
        "",
        "A dim above the fleet dim ran TRUNCATED (Matryoshka): meaningful only for",
        "an MRL-trained checkpoint. A candidate marked prefix=query embedded queries",
        "and documents differently, which production cannot do today: adopting it",
        "includes giving the Embedder seam a query/document asymmetry.",
    ]
    return "\n".join(lines)
