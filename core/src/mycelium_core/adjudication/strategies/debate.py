"""Debate: multi-round multi-agent deliberation with cross-visibility,
convergence detection and an LLM-as-judge synthesis (ADR-0027 §P3).

For each round, every debater receives the question and the prior
round's positions of the other debaters (with attribution) and emits a
JSON turn ``{position, rationale, changed_mind}``. The convergence
detector computes the coherence energy of the round's embeddings
(``1 - cosine`` against the centroid, averaged). The loop stops on the
first of:

1. ``coherence_energy <= coherence_threshold`` (semantic agreement),
2. no debater reports ``changed_mind`` for ``stability_window``
   consecutive rounds (positions stable),
3. ``max_rounds`` reached.

A judge agent (configurable, on by default) then reads the full thread
and produces a synthesis with **explicit** residual dissent (a synthesis
that suppresses disagreement is a bug per ADR-0027 §D7).

Personas are configurable; defaults give one proponent, one skeptic and
one synthesizer. ``devils_advocate=True`` adds an extra debater forced
to argue against the current majority position regardless of merit.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from mycelium_core.adjudication.base import (
    AdjudicationContext,
    AdjudicationOutcome,
    CostModel,
    DissentNote,
    StepStore,
    StrategyRequirements,
)
from mycelium_core.ai_providers import get_llm
from mycelium_core.embedder import EmbedSide, get_embedder
from mycelium_core.models.adjudication import AdjudicationStepKind

_DEFAULT_PERSONAS: tuple[str, ...] = (
    "You are a thorough proponent. Argue for the action under discussion. "
    "Surface its benefits and the forward path; do not exaggerate risks.",
    "You are a thorough skeptic. Argue against the action under discussion. "
    "Surface its risks and missed considerations; do not exaggerate benefits.",
    "You are a careful synthesizer. State the strongest version of each "
    "side, then propose a balanced position. Do not water down disagreement "
    "to please.",
)

_DEVIL_PERSONA = (
    "You are a devil's advocate. Regardless of merit, argue the position "
    "opposite to the current round's majority. Your goal is to stress-test "
    "the consensus, not to be liked."
)

_JUDGE_SYSTEM = (
    "You are reviewing a multi-agent debate. Produce a final decision "
    "explicitly surfacing any residual disagreement. Do not suppress "
    "dissent; if debaters disagreed, the dissent block must list them with "
    "their position and rationale. Output strictly as JSON with keys "
    '"answer", "rationale", "dissent" (list of {"agent_id", "position", '
    '"rationale"}).'
)

_DEBATER_INSTR = (
    'Reply strictly as JSON: {"position": string, "rationale": string, '
    '"changed_mind": boolean}. "changed_mind" is true when this turn '
    "differs from your prior turn (or always true on round 0)."
)


@dataclass(frozen=True)
class DebateConfig:
    """All knobs for ``DebateStrategy``. Built from
    ``ctx.config`` with sane defaults; callers can also instantiate
    one and pass it through ``strategy_config_json``."""

    max_rounds: int = 3
    # Coherence energy (mean 1-cosine against the centroid). Lower
    # means the round agrees; values < ``coherence_threshold`` stop the
    # loop with ``stop_reason='converged'``.
    coherence_threshold: float = 0.20
    # The loop also stops when no debater reported a ``changed_mind``
    # for this many consecutive rounds (positions stable).
    stability_window: int = 2
    # Adds an extra debater that argues against the majority.
    devils_advocate: bool = False
    use_judge: bool = True
    personas: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _Turn:
    agent_id: str
    position: str
    rationale: str
    changed_mind: bool
    embedding: tuple[float, ...]


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_turn_text(text: str, *, default_changed_mind: bool) -> tuple[str, str, bool]:
    """Parse ``{position, rationale, changed_mind}`` from a debater
    reply. Tolerant: extracts the first JSON object substring, falls
    back to the raw text as ``position`` when nothing parses."""
    payload: dict[str, Any] | None = None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        m = _JSON_BLOCK_RE.search(text)
        if m is not None:
            try:
                payload = json.loads(m.group(0))
            except (ValueError, TypeError):
                payload = None
    if not isinstance(payload, dict):
        return (text.strip()[:500], "", default_changed_mind)
    position = str(payload.get("position", "")).strip() or text.strip()[:500]
    rationale = str(payload.get("rationale", "")).strip()
    changed_raw = payload.get("changed_mind", default_changed_mind)
    if isinstance(changed_raw, str):
        changed_mind = changed_raw.strip().lower() in ("1", "true", "yes")
    else:
        changed_mind = bool(changed_raw)
    return (position, rationale, changed_mind)


def _coherence_energy(embeddings: Sequence[Sequence[float]]) -> float:
    """Mean cosine distance to the centroid. 0 means perfect alignment
    (one direction), values near 1 mean uncorrelated. Returns 0 for
    fewer than two embeddings."""
    if len(embeddings) < 2:
        return 0.0
    dim = len(embeddings[0])
    centroid = [0.0] * dim
    for emb in embeddings:
        for i, v in enumerate(emb):
            centroid[i] += v
    for i in range(dim):
        centroid[i] /= len(embeddings)
    cnorm = math.sqrt(sum(c * c for c in centroid))
    if cnorm == 0:
        # All-zero centroid: degenerate; treat as full disagreement.
        return 1.0
    total = 0.0
    for emb in embeddings:
        enorm = math.sqrt(sum(v * v for v in emb))
        if enorm == 0:
            total += 1.0
            continue
        cos = sum(emb[i] * centroid[i] for i in range(dim)) / (enorm * cnorm)
        # Cosine can drift slightly outside [-1, 1] due to float error.
        cos = max(-1.0, min(1.0, cos))
        total += 1.0 - cos
    return total / len(embeddings)


def _confidence_for(stop_reason: str, coherence_last: float | None) -> float:
    """Map ``(stop_reason, last_coherence)`` to confidence in [0, 1].

    Converged: 0.7 + (1 - coherence_last) * 0.25, capped at 0.95.
    Stable:    0.65 fixed (positions stable but possibly diverse).
    Exhausted: 0.40 fixed (we ran out of rounds without converging).
    """
    if stop_reason == "converged":
        base = 0.7
        if coherence_last is not None:
            base += max(0.0, (1.0 - coherence_last)) * 0.25
        return min(0.95, base)
    if stop_reason == "stable":
        return 0.65
    return 0.40


_JUDGE_USER_TEMPLATE = (
    "Question:\n{question}\n\n"
    "Debate transcript (rounds in order; each round lists every "
    "debater's turn):\n{transcript}\n\n"
    "Produce the JSON decision now."
)


def _format_transcript(rounds: list[list[_Turn]]) -> str:
    lines: list[str] = []
    for r_idx, turns in enumerate(rounds):
        lines.append(f"Round {r_idx + 1}:")
        for t in turns:
            cm = "changed_mind=true" if t.changed_mind else "changed_mind=false"
            lines.append(f"  [{t.agent_id}] position={t.position!r} rationale={t.rationale!r} {cm}")
    return "\n".join(lines)


def _parse_judge_reply(text: str) -> tuple[dict[str, Any], tuple[DissentNote, ...]]:
    """Parse the judge's JSON reply. Lenient like the debater parser.

    Returns ``(decision_dict, residual_dissent)``. ``decision_dict`` is
    ``{"answer": ..., "rationale": ...}`` plus any extra top-level
    keys the judge included.
    """
    payload: dict[str, Any] | None = None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        m = _JSON_BLOCK_RE.search(text)
        if m is not None:
            try:
                payload = json.loads(m.group(0))
            except (ValueError, TypeError):
                payload = None
    if not isinstance(payload, dict):
        return ({"answer": text.strip()[:500], "rationale": ""}, ())
    dissent_list = payload.get("dissent", []) or []
    dissent: list[DissentNote] = []
    if isinstance(dissent_list, list):
        for d in dissent_list:
            if not isinstance(d, dict):
                continue
            dissent.append(
                DissentNote(
                    agent_id=str(d.get("agent_id", "?")),
                    position=str(d.get("position", "")),
                    rationale=str(d.get("rationale", "")),
                )
            )
    decision: dict[str, Any] = {k: v for k, v in payload.items() if k != "dissent"}
    return (decision, tuple(dissent))


def _summarize_without_judge(
    last_round: list[_Turn],
) -> tuple[dict[str, Any], tuple[DissentNote, ...]]:
    """No-judge synthesis: the most frequent ``position`` string wins;
    the others become residual dissent."""
    if not last_round:
        return ({}, ())
    counts: dict[str, int] = {}
    for t in last_round:
        counts[t.position] = counts.get(t.position, 0) + 1
    winner = max(counts.items(), key=lambda kv: kv[1])[0]
    decision = {"answer": winner}
    dissent = tuple(
        DissentNote(agent_id=t.agent_id, position=t.position, rationale=t.rationale)
        for t in last_round
        if t.position != winner
    )
    return (decision, dissent)


def _build_round_prompt(
    *,
    question: str,
    self_prior: _Turn | None,
    prior_round: list[_Turn] | None,
    devils_advocate: bool,
) -> str:
    parts: list[str] = [f"Question:\n{question}\n"]
    if prior_round:
        parts.append("Prior round positions from other debaters:")
        for t in prior_round:
            parts.append(f"  [{t.agent_id}] {t.position}  ({t.rationale})")
        parts.append("")
    if self_prior is not None:
        parts.append("Your previous turn:")
        parts.append(f"  position={self_prior.position!r}")
        parts.append(f"  rationale={self_prior.rationale!r}")
        parts.append("")
    if devils_advocate:
        parts.append(
            "Reminder: you are the devil's advocate. Take the position "
            "opposite to whatever the prior round's majority advanced."
        )
        parts.append("")
    parts.append(_DEBATER_INSTR)
    return "\n".join(parts)


@dataclass
class _LoopState:
    rounds: list[list[_Turn]] = field(default_factory=list)
    no_change_streak: int = 0
    stop_reason: str = "exhausted"


class DebateStrategy:
    id = "debate"
    requires = StrategyRequirements(min_agents=2, needs_embedder=True)
    # Composable inside the standard meta-strategies; explicitly *not*
    # composable with ``memory_quorum`` (voting and deliberation are
    # different epistemic stances; see ADR-0027 §D4).
    composable_with: frozenset[str] = frozenset({"fallback_chain", "cap", "filter", "race"})
    mutually_exclusive_with: frozenset[str] = frozenset({"memory_quorum"})
    cost_model = CostModel(
        fixed_tokens=500,
        tokens_per_agent=350,
        tokens_per_round=1000,
    )

    def applicable(self, ctx: AdjudicationContext) -> float:
        # Higher than baseline strategies but conditional: the policy
        # router should only pick debate when there are at least two
        # agents to talk and an embedder is available.
        if ctx.n_agents_available is not None and ctx.n_agents_available < 2:
            return 0.0
        return 0.7

    @staticmethod
    def _cfg_from_ctx(ctx: AdjudicationContext) -> DebateConfig:
        c = ctx.config or {}
        personas = c.get("personas")
        if personas is not None:
            personas_t: tuple[str, ...] | None = tuple(str(p) for p in personas)
        else:
            personas_t = None
        return DebateConfig(
            max_rounds=int(c.get("max_rounds", 3)),
            coherence_threshold=float(c.get("coherence_threshold", 0.20)),
            stability_window=int(c.get("stability_window", 2)),
            devils_advocate=bool(c.get("devils_advocate", False)),
            use_judge=bool(c.get("use_judge", True)),
            personas=personas_t,
        )

    @staticmethod
    def _effective_personas(cfg: DebateConfig) -> list[str]:
        base = list(cfg.personas) if cfg.personas else list(_DEFAULT_PERSONAS)
        if cfg.devils_advocate:
            base.append(_DEVIL_PERSONA)
        return base

    async def run(self, ctx: AdjudicationContext, store: StepStore) -> AdjudicationOutcome:
        cfg = self._cfg_from_ctx(ctx)
        personas = self._effective_personas(cfg)
        if len(personas) < 2:
            raise ValueError("DebateStrategy needs at least two personas")

        llm = get_llm()
        embedder = get_embedder()
        state = _LoopState()
        prior_self: dict[str, _Turn] = {}

        for round_no in range(cfg.max_rounds):
            round_turns: list[_Turn] = []
            for agent_idx, persona_system in enumerate(personas):
                agent_id = f"debater_{agent_idx}"
                is_devil = cfg.devils_advocate and agent_idx == len(personas) - 1
                user_prompt = _build_round_prompt(
                    question=ctx.question_text,
                    self_prior=prior_self.get(agent_id),
                    prior_round=state.rounds[-1] if state.rounds else None,
                    devils_advocate=is_devil,
                )
                result = await llm.complete(
                    system=persona_system,
                    messages=[("user", user_prompt)],
                )
                position, rationale, changed_mind = _parse_turn_text(
                    result.text, default_changed_mind=(round_no == 0)
                )
                embed_text = position + ("\n" + rationale if rationale else "")
                emb = await embedder.embed(embed_text, side=EmbedSide.document)
                turn = _Turn(
                    agent_id=agent_id,
                    position=position,
                    rationale=rationale,
                    changed_mind=changed_mind,
                    embedding=tuple(emb.vector),
                )
                await store.append(
                    kind=AdjudicationStepKind.turn,
                    payload={
                        "round": round_no,
                        "position": position,
                        "rationale": rationale,
                        "changed_mind": changed_mind,
                        "model_id": result.model_id,
                        "tokens_in": result.tokens_in,
                        "tokens_out": result.tokens_out,
                        "devils_advocate": is_devil,
                    },
                    agent_id=agent_id,
                    embedding=list(emb.vector),
                )
                round_turns.append(turn)
                prior_self[agent_id] = turn

            state.rounds.append(round_turns)
            coherence = _coherence_energy([t.embedding for t in round_turns])
            n_changed = sum(1 for t in round_turns if t.changed_mind)
            await store.append(
                kind=AdjudicationStepKind.score,
                payload={
                    "round": round_no,
                    "coherence_energy": coherence,
                    "n_changed_mind": n_changed,
                    "n_debaters": len(round_turns),
                },
            )

            if coherence <= cfg.coherence_threshold:
                state.stop_reason = "converged"
                break
            if n_changed == 0:
                state.no_change_streak += 1
                if state.no_change_streak >= cfg.stability_window:
                    state.stop_reason = "stable"
                    break
            else:
                state.no_change_streak = 0
        else:
            state.stop_reason = "exhausted"

        last_round = state.rounds[-1] if state.rounds else []
        coherence_last = (
            _coherence_energy([t.embedding for t in last_round]) if last_round else None
        )

        if cfg.use_judge and last_round:
            judge_user = _JUDGE_USER_TEMPLATE.format(
                question=ctx.question_text,
                transcript=_format_transcript(state.rounds),
            )
            judge_result = await llm.complete(
                system=_JUDGE_SYSTEM,
                messages=[("user", judge_user)],
            )
            decision, dissent = _parse_judge_reply(judge_result.text)
            await store.append(
                kind=AdjudicationStepKind.synthesis,
                payload={
                    "decision": decision,
                    "dissent": [
                        {
                            "agent_id": d.agent_id,
                            "position": d.position,
                            "rationale": d.rationale,
                        }
                        for d in dissent
                    ],
                    "model_id": judge_result.model_id,
                    "tokens_in": judge_result.tokens_in,
                    "tokens_out": judge_result.tokens_out,
                },
                agent_id="judge",
            )
        else:
            decision, dissent = _summarize_without_judge(last_round)
            if last_round:
                await store.append(
                    kind=AdjudicationStepKind.synthesis,
                    payload={
                        "decision": decision,
                        "dissent": [
                            {
                                "agent_id": d.agent_id,
                                "position": d.position,
                                "rationale": d.rationale,
                            }
                            for d in dissent
                        ],
                        "judge": False,
                    },
                    agent_id="majority_vote",
                )

        confidence = _confidence_for(state.stop_reason, coherence_last)

        return AdjudicationOutcome(
            decision=decision,
            confidence=confidence,
            residual_dissent=dissent,
            meta={
                "stop_reason": state.stop_reason,
                "rounds_run": len(state.rounds),
                "coherence_last": coherence_last,
                "judge_used": cfg.use_judge and bool(last_round),
                "n_debaters": len(personas),
            },
        )
