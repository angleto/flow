"""ADR-0025 P3: agent execution runtime.

One LLM task end-to-end: spawn -> work -> artifact -> complete;
metered, bounded, killable, with governance by construction.

Covers the P3 acceptance bullets:
(a) happy path: an assigned llm task + a scripted FakeLLM that runs one
    allowed tool (append a work note) then finishes -> the run
    succeeds, ``artifact_note_id`` is set and that note is linked to
    the task, ``credits_spent`` is metered (with a rate card) and free
    (no rate card, via ``meter_if_billable``), ``steps`` > 0, and the
    whole run is DETERMINISTIC across two executions.
(b) not dispatchable: a task with no Schedule row / an unassignable
    Schedule / a human executor -> 400 AGENT_RUN_NOT_DISPATCHABLE.
(c) budget: a tiny executor ``credit_budget`` + a FakeLLM that would
    exceed it -> status=blocked, blocked_reason="budget_exhausted".
(d) tool allowlist: a FakeLLM requesting a non-allowlisted op ->
    status=blocked, blocked_reason="tool_not_allowed", NO side effect.
(e) cancel: start with a long script, request cancel -> status=
    cancelled, the loop stopped (no further steps / no artifact).
(f) double-start rejected (AGENT_RUN_ALREADY_ACTIVE).
(g) RBAC: a member (no owner header) cannot start/cancel (403).
(h) cross-org isolation (a foreign task / run -> not found).

Style mirrors api/tests/test_scheduler_p2.py + the FakeEmbedder/STT
fixture-override pattern (set_llm_override, like set_embedder_override).
Privileged start/cancel send X-Workspace-Role: owner (the start/cancel
service calls are owner-gated -- running an agent spends credits).
The real LocalLLM path stays ``# pragma: no cover``; here a scripted
deterministic provider is injected.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator, Sequence
from decimal import Decimal

import pytest
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests_helpers import seed_ai_assistant_identity

from mycelium_api.main import app
from mycelium_core.ai_providers import LLMResult, set_llm_override
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import DomainError, ForbiddenError, NotFoundError
from mycelium_core.models.agent_run import AgentRun, AgentRunStatus
from mycelium_core.models.ai_assistant import AiAssistant, AssistantRuntime
from mycelium_core.models.executor import Executor, ExecutorKind
from mycelium_core.models.identity import Identity
from mycelium_core.models.note import Note
from mycelium_core.models.schedule import Schedule
from mycelium_core.services import agent_runtime as runtime
from mycelium_core.services import billing
from mycelium_core.services import executors as exec_svc
from mycelium_core.services import scheduler as sch
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup

_AS_OF = dt.datetime(2026, 1, 12, 8, 0, tzinfo=dt.UTC)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


# --- A scriptable, deterministic LLM provider (the P3 test seam) ------
#
# Returns a fixed list of canned ``LLMResult.text`` payloads, one per
# ``complete`` call (the agent-runtime contract is a JSON object
# {"tool": ..., "args": {...}}). Token counts are derived
# deterministically from the script so metering is reproducible. This
# is the P3 analogue of FakeEmbedder/FakeSTT and is injected via
# ``set_llm_override`` exactly like ``set_embedder_override``.


class ScriptedLLM:
    model_id = "fake-agent-llm"

    def __init__(self, script: Sequence[str]) -> None:
        self._script = list(script)
        self._i = 0

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        # Past the script end keep emitting a terminal finish so a run
        # that out-steps its script still terminates deterministically.
        if self._i < len(self._script):
            text = self._script[self._i]
        else:
            text = '{"tool": "finish", "args": {"output": "done"}}'
        self._i += 1
        return LLMResult(text=text, tokens_in=10, tokens_out=5, model_id=self.model_id)


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    # write_memory / recall_memory go through the embedder; keep it the
    # deterministic fake so the run stays reproducible (no real model).
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _use_llm(script: Sequence[str]) -> None:
    """Install a fresh ScriptedLLM (one instance per run so the cursor
    is not shared across runs in determinism tests)."""
    set_llm_override(lambda: ScriptedLLM(script))


def _clear_llm() -> None:
    set_llm_override(None)


async def _dispatched_llm_task(
    s: AsyncSession,
    *,
    org: uuid.UUID,
    user: uuid.UUID,
    title: str = "Agent task",
    rate: Decimal = Decimal("0.5"),
    budget: Decimal | None = None,
) -> tuple[object, Executor]:
    """Create a capable llm_agent + an llm task that the P2 scheduler
    assigns to it; returns (task, executor) with a dispatchable
    Schedule row (assigned, not unassignable)."""
    # Disable the seeded default agent (no tags) so the capable one is
    # the only eligible executor -> deterministic assignment.
    await exec_svc.ensure_default_agent(s, org_id=org)
    default_agent = (
        await s.execute(select(Executor).where(Executor.kind == ExecutorKind.llm_agent))
    ).scalar_one()
    await exec_svc.update_executor(
        s,
        org_id=org,
        actor_id=user,
        executor_id=default_agent.id,
        expected_version=default_agent.version,
        values={"enabled": False},
    )
    executor = await exec_svc.create_executor(
        s,
        org_id=org,
        actor_id=user,
        kind=ExecutorKind.llm_agent,
        name="Worker",
        max_parallel=4,
        credit_rate_per_hour=rate,
        credit_budget=budget,
        capability_tags=["x"],
    )
    ai_ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
    task = await tasks_svc.create_task(
        s,
        org_id=org,
        actor_id=user,
        title=title,
        estimate_effort_h=Decimal(2),
        assignee_id=ai_ident.id,
        required_capabilities=["x"],
    )
    await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
    row = (await s.execute(select(Schedule).where(Schedule.task_id == task.id))).scalar_one()
    assert row.assigned_executor_id == executor.id and row.unassignable is False
    return task, executor


# --- (a) happy path: metered + deterministic, artifact linked --------


async def test_happy_path_metered_artifact_and_determinism(
    _fake_embedder: None,
) -> None:
    """An assigned llm task; the agent appends a work note then
    finishes. Run succeeds, the artifact note exists & is linked, with
    a rate card credits are metered, and two identical runs are
    byte-identical (status/steps/credits)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="HP")
    org, user = a.org_id, a.user_id

    script = [
        '{"tool": "append_work_note", "args": {"title": "Findings", '
        '"text": "Investigated and resolved."}}',
        '{"tool": "finish", "args": {"output": "Completed the task."}}',
    ]

    async with tenant_session(str(org), str(user)) as s:
        task, executor = await _dispatched_llm_task(s, org=org, user=user)
        # Rate card for the agent model + credits so metering is real.
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(100))
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id="agent",  # executor.model_id is None -> "agent"
            provider="local",
            values={"credits_per_input": "0.01", "credits_per_output": "0.02"},
        )
        _use_llm(script)
        run = await runtime.start_run(s, org_id=org, actor_id=user, task_id=task.id)
        _clear_llm()

        assert run.status is AgentRunStatus.succeeded
        assert run.steps == 2  # one tool call + the finish step
        assert run.executor_id == executor.id
        assert run.ended_at is not None and run.started_at is not None
        # Metered: 2 model calls * (10*0.01 + 5*0.02) = 2 * 0.20 = 0.40.
        assert run.credits_spent == Decimal("0.4000")
        assert run.blocked_reason is None and run.error is None

        # The artifact is a Proposal-A work note LINKED to the task.
        # ADR-0029 P3: the link is a NoteTaskLink (kind=artifact),
        # not ``note.task_id`` which was dropped.
        assert run.artifact_note_id is not None
        note = (await s.execute(select(Note).where(Note.id == run.artifact_note_id))).scalar_one()
        from mycelium_core.models.note_link import NoteTaskLink

        art_link = (
            await s.execute(
                select(NoteTaskLink).where(
                    NoteTaskLink.note_id == run.artifact_note_id,
                    NoteTaskLink.task_id == task.id,
                    NoteTaskLink.kind == "artifact",
                )
            )
        ).scalar_one_or_none()
        assert art_link is not None
        # Phase 6 final: body lives in note_part(ord=0).
        from mycelium_core.services.notes import get_body as _get_body

        assert "Investigated and resolved." in (await _get_body(s, note_id=note.id))

    # Determinism: two fresh workspaces + the SAME script -> identical
    # terminal run shape (status, steps, credits).
    def _snapshot(r: AgentRun) -> tuple[str, int, Decimal]:
        return (r.status.value, r.steps, r.credits_spent)

    snaps: list[tuple[str, int, Decimal]] = []
    for _ in range(2):
        async with admin_session() as s:
            c = await signup(s, email=_email(), password="pw-strong-123", org_name="DET")
        o, u = c.org_id, c.user_id
        async with tenant_session(str(o), str(u)) as s:
            t, _ex = await _dispatched_llm_task(s, org=o, user=u)
            await billing.grant_credits(s, org_id=o, actor_id=u, amount=Decimal(100))
            await billing.upsert_rate_card(
                s,
                org_id=o,
                actor_id=u,
                model_id="agent",
                provider="local",
                values={"credits_per_input": "0.01", "credits_per_output": "0.02"},
            )
            _use_llm(script)
            r = await runtime.start_run(s, org_id=o, actor_id=u, task_id=t.id)
            _clear_llm()
            snaps.append(_snapshot(r))
    assert snaps[0] == snaps[1] == ("succeeded", 2, Decimal("0.4000"))


async def test_happy_path_free_without_rate_card(_fake_embedder: None) -> None:
    """With NO rate card the run is FREE (meter_if_billable: missing
    rate card => no charge, no error) yet still succeeds and produces
    the artifact -- the agent works out of the box, unmetered."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="FREE")
    org, user = a.org_id, a.user_id
    script = ['{"tool": "finish", "args": {"output": "ok"}}']

    async with tenant_session(str(org), str(user)) as s:
        task, _ex = await _dispatched_llm_task(s, org=org, user=user)
        _use_llm(script)
        run = await runtime.start_run(s, org_id=org, actor_id=user, task_id=task.id)
        _clear_llm()

    assert run.status is AgentRunStatus.succeeded
    assert run.steps == 1
    assert run.credits_spent == Decimal(0)  # free: no rate card
    assert run.artifact_note_id is not None


# --- (b) not dispatchable ---------------------------------------------


async def test_not_dispatchable_human_task(_fake_embedder: None) -> None:
    """A human-executor task is never agent-dispatchable -> 400."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "ND"},
            )
        ).json()
        oh = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        t = (
            await c.post(
                "/tasks",
                headers=oh,
                json={"title": "Human work", "estimate_effort_h": "2"},
            )
        ).json()
        r = await c.post(f"/tasks/{t['id']}/run", headers=oh)
        assert r.status_code == 400
        assert r.json()["code"] == "agent_run.not_dispatchable"


async def test_not_dispatchable_no_schedule_or_unassignable(
    _fake_embedder: None,
) -> None:
    """An llm task with NO Schedule row -> not dispatchable; and an
    llm task whose Schedule is unassignable -> not dispatchable."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="NS")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        ai_ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
        # (i) llm task, never scheduled -> no Schedule row at all.
        t1 = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Unscheduled",
            estimate_effort_h=Decimal(2),
            assignee_id=ai_ident.id,
        )
        with pytest.raises(DomainError) as ei:
            await runtime.start_run(s, org_id=org, actor_id=user, task_id=t1.id)
        assert ei.value.code.value == "agent_run.not_dispatchable"

        # (ii) llm task needing a capability no enabled agent has ->
        # the scheduler flags the Schedule row unassignable.
        await exec_svc.ensure_default_agent(s, org_id=org)
        t2 = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="NoCapableAgent",
            estimate_effort_h=Decimal(2),
            assignee_id=ai_ident.id,
            required_capabilities=["rare"],
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        row = (await s.execute(select(Schedule).where(Schedule.task_id == t2.id))).scalar_one()
        assert row.unassignable is True
        with pytest.raises(DomainError) as ei2:
            await runtime.start_run(s, org_id=org, actor_id=user, task_id=t2.id)
        assert ei2.value.code.value == "agent_run.not_dispatchable"


# --- (c) budget exhaustion -> blocked ---------------------------------


async def test_budget_exhaustion_blocks_run(_fake_embedder: None) -> None:
    """The assigned executor's credit_budget caps the RUNTIME loop: a
    script that would keep going is stopped with status=blocked,
    blocked_reason budget_exhausted, before exceeding the cap.

    The budget must be >= the P2 admission projection (effort_h *
    credit_rate_per_hour = 2 * 0.5 = 1.0) or the scheduler would refuse
    to dispatch the task in the first place (P2 admission control). So
    budget=1.0 (admitted) and a rate card making each runtime step cost
    1.0 (units_in 10 * 0.10) -> after step 1 the loop top sees
    credits_spent (1.0) >= budget (1.0) and blocks."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="BUD")
    org, user = a.org_id, a.user_id
    # A long script of allowed memory writes (never finishes on its own)
    # so ONLY the budget guard can stop it.
    script = ['{"tool": "write_memory", "args": {"text": "step"}}'] * 12

    async with tenant_session(str(org), str(user)) as s:
        task, _ex = await _dispatched_llm_task(
            s, org=org, user=user, rate=Decimal("0.5"), budget=Decimal("1.0")
        )
        await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(100))
        await billing.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id="agent",
            provider="local",
            values={"credits_per_input": "0.10", "credits_per_output": "0"},
        )
        _use_llm(script)
        run = await runtime.start_run(s, org_id=org, actor_id=user, task_id=task.id)
        _clear_llm()

        assert run.status is AgentRunStatus.blocked
        assert run.blocked_reason == "budget_exhausted"
        assert run.ended_at is not None
        # Stopped right at the cap, well before MAX_STEPS (1 paid step
        # of 1.0 reaches the 1.0 budget; step 2 is refused).
        assert run.steps == 1
        assert run.credits_spent >= Decimal("1.0")
        assert run.artifact_note_id is None  # blocked: no artifact


# --- (d) tool allowlist -> blocked, NO side effect --------------------


async def test_search_tool_runs_inside_agent_loop(_fake_embedder: None) -> None:
    """Task a83a5c0b: ``search`` is in the agent allowlist so the
    runtime accepts it and dispatches to ``task_search.search_unified``.
    Smoke: a script that calls search then finishes succeeds (status
    succeeded, observation surfaced) -- the search-result count is not
    asserted (deterministic vectors but a single dispatched task only
    has its own pointer in flight)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="SRCH")
    org, user = a.org_id, a.user_id
    script = [
        '{"tool": "search", "args": {"query": "task", "limit": 3}}',
        '{"tool": "finish", "args": {"output": "found"}}',
    ]
    async with tenant_session(str(org), str(user)) as s:
        task, _ex = await _dispatched_llm_task(s, org=org, user=user)
        _use_llm(script)
        run = await runtime.start_run(s, org_id=org, actor_id=user, task_id=task.id)
        _clear_llm()
        assert run.status is AgentRunStatus.succeeded
        assert run.blocked_reason is None
        # Step 1 = search dispatch, step 2 = finish.
        assert run.steps == 2


async def test_tool_not_allowed_blocks_with_no_side_effect(
    _fake_embedder: None,
) -> None:
    """A FakeLLM requesting a tool OUTSIDE the hard allowlist stops the
    run (status=blocked, tool_not_allowed) and performs NO side effect
    (no note, no artifact)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="TA")
    org, user = a.org_id, a.user_id
    # 'delete_task' is deliberately destructive and NOT in the allowlist.
    script = [
        '{"tool": "delete_task", "args": {"task_id": "00000000-0000-0000-0000-000000000000"}}',
        '{"tool": "finish", "args": {"output": "should not get here"}}',
    ]

    async with tenant_session(str(org), str(user)) as s:
        task, _ex = await _dispatched_llm_task(s, org=org, user=user)
        # ADR-0029 P3: notes are linked to tasks via NoteTaskLink
        # (any kind), not the dropped ``Note.task_id`` column.
        from mycelium_core.models.note_link import NoteTaskLink

        notes_before = (
            await s.execute(
                select(func.count())
                .select_from(NoteTaskLink)
                .where(NoteTaskLink.task_id == task.id)
            )
        ).scalar_one()
        _use_llm(script)
        run = await runtime.start_run(s, org_id=org, actor_id=user, task_id=task.id)
        _clear_llm()

        assert run.status is AgentRunStatus.blocked
        assert run.blocked_reason == "tool_not_allowed"
        assert run.artifact_note_id is None  # NO artifact produced
        notes_after = (
            await s.execute(
                select(func.count())
                .select_from(NoteTaskLink)
                .where(NoteTaskLink.task_id == task.id)
            )
        ).scalar_one()
        assert notes_after == notes_before  # NO side effect
        # The disallowed tool was step 1; the loop stopped there.
        assert run.steps == 1


# --- (e) cancel stops the loop ----------------------------------------


async def test_cancel_observed_by_loop_stops_run(_fake_embedder: None) -> None:
    """The cooperative kill switch. A provider whose FIRST step requests
    cancel_run (an allowlisted... no -- cancel is NOT a tool; instead we
    drive the documented loop: a run with cancel_requested already set
    is observed at the top of the loop and stops with status=cancelled,
    no steps, no artifact). In-process the loop is synchronous, so a
    cancel observed at a loop boundary halts it -- exactly the contract
    cancel_run relies on."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="CAN")
    org, user = a.org_id, a.user_id
    long_script = ['{"tool": "write_memory", "args": {"text": "x"}}'] * 12

    async with tenant_session(str(org), str(user)) as s:
        task, ex = await _dispatched_llm_task(s, org=org, user=user)
        # Pre-create a RUNNING AgentRun with cancel already requested
        # (as cancel_run would), then drive the loop: it must observe
        # the flag on the first iteration and stop with no side effect.
        run = AgentRun(
            org_id=org,
            task_id=task.id,
            executor_id=ex.id,
            status=AgentRunStatus.running,
            steps=0,
            credits_spent=Decimal(0),
            started_at=_AS_OF,
            cancel_requested=True,
        )
        s.add(run)
        await s.flush()
        _use_llm(long_script)
        await runtime._drive(
            s,
            org_id=org,
            actor_id=user,
            run=run,
            task=task,
            executor=ex,
            provider=runtime.get_llm(),
        )
        _clear_llm()

        assert run.status is AgentRunStatus.cancelled
        assert run.steps == 0  # loop stopped before any model call
        assert run.artifact_note_id is None
        assert run.ended_at is not None

        # And cancel_run itself: on an ACTIVE run it sets the flag and
        # is idempotent (a second call does not error while still
        # non-terminal -- here the run is already cancelled/terminal, so
        # assert the owner-gated path on a fresh active run instead).
        run2 = AgentRun(
            org_id=org,
            task_id=task.id,
            executor_id=ex.id,
            status=AgentRunStatus.running,
            steps=0,
            credits_spent=Decimal(0),
            started_at=_AS_OF,
        )
        s.add(run2)
        await s.flush()
        c1 = await runtime.cancel_run(s, org_id=org, actor_id=user, run_id=run2.id)
        assert c1.cancel_requested is True
        # Idempotent: a second cancel on the still-active run is fine.
        c2 = await runtime.cancel_run(s, org_id=org, actor_id=user, run_id=run2.id)
        assert c2.cancel_requested is True


async def test_cancel_run_owner_gated_and_terminal_guard(
    _fake_embedder: None,
) -> None:
    """cancel_run is owner-gated and idempotent; cancelling a terminal
    (already succeeded) run is AGENT_RUN_TERMINAL."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="CG")
    org, user = a.org_id, a.user_id
    script = ['{"tool": "finish", "args": {"output": "ok"}}']

    async with tenant_session(str(org), str(user)) as s:
        task, _ex = await _dispatched_llm_task(s, org=org, user=user)
        _use_llm(script)
        run = await runtime.start_run(s, org_id=org, actor_id=user, task_id=task.id)
        _clear_llm()
        assert run.status is AgentRunStatus.succeeded
        # Cancelling a terminal run is rejected.
        with pytest.raises(DomainError) as ei:
            await runtime.cancel_run(s, org_id=org, actor_id=user, run_id=run.id)
        assert ei.value.code.value == "agent_run.terminal"


# --- (f) double start rejected ----------------------------------------


async def test_double_start_rejected_via_api(_fake_embedder: None) -> None:
    """A second start while a run is active -> AGENT_RUN_ALREADY_ACTIVE.
    (Drive a run to a terminal state first to prove the guard is on
    ACTIVE runs, then assert a service-level active guard directly.)"""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="DS")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        task, ex = await _dispatched_llm_task(s, org=org, user=user)
        # Insert an ACTIVE (running) run row directly, then a start must
        # be rejected because one is already active for this task.
        active = AgentRun(
            org_id=org,
            task_id=task.id,
            executor_id=ex.id,
            status=AgentRunStatus.running,
            steps=1,
            credits_spent=Decimal(0),
            started_at=_AS_OF,
        )
        s.add(active)
        await s.flush()
        _use_llm(['{"tool": "finish", "args": {"output": "x"}}'])
        with pytest.raises(DomainError) as ei:
            await runtime.start_run(s, org_id=org, actor_id=user, task_id=task.id)
        _clear_llm()
        assert ei.value.code.value == "agent_run.already_active"

        # A terminal run does NOT block a new start (guard is on
        # queued/running only).
        active.status = AgentRunStatus.cancelled
        await s.flush()
        _use_llm(['{"tool": "finish", "args": {"output": "ok"}}'])
        run2 = await runtime.start_run(s, org_id=org, actor_id=user, task_id=task.id)
        _clear_llm()
        assert run2.status is AgentRunStatus.succeeded


# --- (g) RBAC: member cannot start/cancel -----------------------------


def _uid(token: str) -> uuid.UUID:
    from mycelium_core.security import decode_token

    return uuid.UUID(decode_token(token)["sub"])


async def test_rbac_member_cannot_start_or_cancel(_fake_embedder: None) -> None:
    """Without the owner header the effective role clamps to member
    (least privilege); POST /tasks/{id}/run and the cancel endpoint are
    403 (the service require_role(owner) choke point + effective-role
    sudo). A dispatchable task is built in-session (the signup user is
    the owner); the API call is made WITHOUT the owner header."""
    a = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "RB"},
            )
        ).json()
    assert a is not None
    org = uuid.UUID(a["workspace_id"])
    user = _uid(a["token"])
    member_h = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
    }

    async with tenant_session(str(org), str(user)) as s:
        task, _ex = await _dispatched_llm_task(s, org=org, user=user)
        task_id = task.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # No X-Workspace-Role => effective role clamps to member.
        r = await c.post(f"/tasks/{task_id}/run", headers=member_h)
        assert r.status_code == 403
        assert r.json()["code"] == "rbac.role_insufficient"
        # Cancel is also owner-gated (require_role runs before the row
        # lookup, so a random id still yields 403, not 404).
        rc = await c.post(f"/agent-runs/{uuid.uuid4()}/cancel", headers=member_h)
        assert rc.status_code == 403


async def test_rbac_member_cannot_start_service_level(
    _fake_embedder: None,
) -> None:
    """Service choke point: a plain member of the workspace (no
    published effective role -> require_role falls back to stored
    membership) cannot start OR cancel a run. Mirrors the P2
    ``test_service_mutations_owner_gated_in_session`` precedent (add a
    second user as a plain member of the owner's org from inside the
    owner's tenant session, then act as them)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="RBS")
    owner_org, owner_user = a.org_id, a.user_id

    async with tenant_session(str(owner_org), str(owner_user)) as s:
        task, _ex = await _dispatched_llm_task(s, org=owner_org, user=owner_user)
        task_id = task.id
        # A successful run so there is a terminal run to attempt to
        # cancel as a member (proves cancel is owner-gated too).
        _use_llm(['{"tool": "finish", "args": {"output": "ok"}}'])
        done = await runtime.start_run(s, org_id=owner_org, actor_id=owner_user, task_id=task_id)
        _clear_llm()
        run_id = done.id
        # Add `other` as a plain member of owner_org (RLS allows the
        # insert: app.current_org == owner_org in this session).
        from mycelium_core.models.membership import Membership, Role

        other = await signup(s, email=_email(), password="pw-strong-123", org_name="OTH")
        s.add(Membership(org_id=owner_org, user_id=other.user_id, role=Role.member))
        await s.flush()

    async with tenant_session(str(owner_org), str(other.user_id)) as s:
        with pytest.raises(ForbiddenError):
            await runtime.start_run(s, org_id=owner_org, actor_id=other.user_id, task_id=task_id)
        with pytest.raises(ForbiddenError):
            await runtime.cancel_run(s, org_id=owner_org, actor_id=other.user_id, run_id=run_id)


# --- (h) cross-org isolation ------------------------------------------


async def test_cross_org_isolation(_fake_embedder: None) -> None:
    """A run created in org A is invisible to org B (RLS): get_run /
    cancel_run / start_run for a foreign task all resolve to NOT FOUND
    (never another tenant's data)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ORGA")
        b = await signup(s, email=_email(), password="pw-strong-123", org_name="ORGB")
    org_a, user_a = a.org_id, a.user_id
    org_b, user_b = b.org_id, b.user_id

    script = ['{"tool": "finish", "args": {"output": "ok"}}']
    async with tenant_session(str(org_a), str(user_a)) as s:
        task_a, _ex = await _dispatched_llm_task(s, org=org_a, user=user_a)
        _use_llm(script)
        run_a = await runtime.start_run(s, org_id=org_a, actor_id=user_a, task_id=task_a.id)
        _clear_llm()
        run_a_id, task_a_id = run_a.id, task_a.id

    async with tenant_session(str(org_b), str(user_b)) as s:
        # B cannot read A's run.
        with pytest.raises(NotFoundError):
            await runtime.get_run(s, org_id=org_b, run_id=run_a_id)
        # B cannot cancel A's run.
        with pytest.raises(NotFoundError):
            await runtime.cancel_run(s, org_id=org_b, actor_id=user_b, run_id=run_a_id)
        # B cannot start a run on A's task (foreign task -> not found).
        with pytest.raises(NotFoundError):
            await runtime.start_run(s, org_id=org_b, actor_id=user_b, task_id=task_a_id)
        # B's run list does not include A's run.
        rows = await runtime.list_runs(s, org_id=org_b)
        assert all(r.id != run_a_id for r in rows)


async def test_start_run_refuses_an_external_assistant_on_its_own(_fake_embedder: None) -> None:
    """The door ``start_run`` has to close by itself.

    Two of its three callers -- MCP ``agent_run_start`` and
    POST /tasks/{id}/run -- never pass through the dispatch loop, so the
    loop's admission is not this function's guarantee. Neither is the
    executor guard below it: ``_assigned_executor`` reads back whatever
    the last recompute assigned, so it finds a row and the run proceeds.

    Assigned while the assistant was ``internal``, flipped afterwards.
    Same shape as the ``assignee_inactive`` live re-check: the schedule
    row is only as fresh as the last recompute, and the refusal has to
    come from the identity, not from the snapshot. That is also why the
    schedule is deliberately NOT recomputed after the flip -- a recompute
    would drop the executor and the test would then pass on the wrong
    guard.
    """
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EXTR")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        task, executor = await _dispatched_llm_task(s, org=org, user=user)
        ident = (
            await s.execute(select(Identity).where(Identity.id == task.assignee_id))
        ).scalar_one()
        await s.execute(
            update(AiAssistant)
            .where(AiAssistant.id == ident.ai_assistant_id)
            .values(runtime=AssistantRuntime.external)
        )

        # The schedule still says dispatchable; the identity says no.
        row = (await s.execute(select(Schedule).where(Schedule.task_id == task.id))).scalar_one()
        assert row.assigned_executor_id == executor.id and row.unassignable is False

        with pytest.raises(DomainError) as ei:
            await runtime.start_run(s, org_id=org, actor_id=user, task_id=task.id)
        assert ei.value.code.value == "agent_run.not_dispatchable"

        # And it refuses BEFORE the row is written: the run row is
        # flushed ahead of the provider call, and a phantom run excludes
        # the task from every later tick (``_tasks_with_any_run`` has no
        # status predicate at all).
        n_runs = (
            await s.execute(
                select(func.count()).select_from(AgentRun).where(AgentRun.task_id == task.id)
            )
        ).scalar_one()
        assert n_runs == 0

        # Symmetric: flipped back, the same task runs. Without this the
        # test passes against a ``start_run`` that refuses everything.
        await s.execute(
            update(AiAssistant)
            .where(AiAssistant.id == ident.ai_assistant_id)
            .values(runtime=AssistantRuntime.internal)
        )
        _use_llm(['{"tool": "finish", "args": {"output": "ok"}}'])
        run = await runtime.start_run(s, org_id=org, actor_id=user, task_id=task.id)
        _clear_llm()
        assert run.status is AgentRunStatus.succeeded
