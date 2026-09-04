"""ADR-0025 P5: closed-loop dispatch + approval gates.

The closed cycle that ties P1-P4 together:

    tick -> recompute (P1/P2 scheduler) -> admit the llm_agent set ->
    governance gate (human-in-the-loop default / auto opt-in) ->
    dispatch via the P3 metered ``start_run`` -> [runs execute
    out-of-band, the P4 set_state hook fires handoffs on completion] ->
    next tick recompute picks up the new ready set.

Covers the P5 acceptance bullets:
(a) policy ``off`` -> tick is a no-op (no requests, no runs);
(b) ``approval_required`` (the governance default) -> tick creates a
    PENDING request, NO run started, no credit spent;
(c) approve -> the run starts via P3 (request ``dispatched``,
    ``agent_run_id`` set, metered through the existing agentrun meter);
(d) deny -> no run; a denied/decided request cannot be approved;
(e) ``auto`` -> tick dispatches without a manual approval, still
    bounded by the per-agent WIP / budget;
(f) per-tick churn cap respected;
(g) NON-FATAL isolation: one task whose run start fails (live WIP
    re-check) is marked ``failed`` while another in the SAME tick
    dispatches;
(h) idempotent: a second tick with no state change -> no duplicate
    requests / no duplicate runs;
(i) owner-gating: a non-owner (effective role clamps to member) is
    denied approve / deny / tick / policy-set (403 / ForbiddenError),
    same assertion style as the P2/P4 privileged-op tests.

Style mirrors api/tests/test_coordination_p4.py and
test_agent_runtime_p3.py (the ScriptedLLM + FakeEmbedder
fixture-override + ``_dispatched_llm_task`` pattern; signup -> owner of
a fresh workspace; privileged calls send ``X-Workspace-Role: owner``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator, Sequence
from decimal import Decimal

import pytest
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests_helpers import seed_ai_assistant_identity

from mycelium_api.main import app
from mycelium_core.ai_providers import LLMResult, set_llm_override
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.errors import DomainError, ForbiddenError
from mycelium_core.models.agent_run import AgentRun, AgentRunStatus
from mycelium_core.models.ai_assistant import AssistantRuntime
from mycelium_core.models.dispatch_request import (
    AutonomousDispatch,
    DispatchRequest,
    DispatchStatus,
)
from mycelium_core.models.executor import Executor, ExecutorKind
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.organization import Organization
from mycelium_core.security import decode_token
from mycelium_core.services import billing
from mycelium_core.services import dispatch_loop as loop
from mycelium_core.services import executors as exec_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup

_AS_OF = dt.datetime(2026, 1, 12, 8, 0, tzinfo=dt.UTC)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


class ScriptedLLM:
    model_id = "fake-agent-llm"

    def __init__(self, script: Sequence[str]) -> None:
        self._script = list(script)
        self._i = 0

    async def complete(
        self, *, system: str | None, messages: Sequence[tuple[str, str]]
    ) -> LLMResult:
        text = (
            self._script[self._i]
            if self._i < len(self._script)
            else '{"tool": "finish", "args": {"output": "done"}}'
        )
        self._i += 1
        return LLMResult(text=text, tokens_in=10, tokens_out=5, model_id=self.model_id)


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _use_llm(script: Sequence[str]) -> None:
    set_llm_override(lambda: ScriptedLLM(script))


def _clear_llm() -> None:
    set_llm_override(None)


_FINISH = ['{"tool": "finish", "args": {"output": "ok"}}']


async def _set_policy(s: AsyncSession, *, org: uuid.UUID, policy: AutonomousDispatch) -> None:
    """Write the autonomous policy into the workspace settings bag
    (same JSON mechanism the settings endpoint uses)."""
    org_row = (await s.execute(select(Organization).where(Organization.id == org))).scalar_one()
    org_row.settings = {**(org_row.settings or {}), loop.SETTINGS_KEY: policy.value}
    org_row.version += 1
    await s.flush()


async def _capable_agent(
    s: AsyncSession,
    *,
    org: uuid.UUID,
    user: uuid.UUID,
    name: str = "Worker",
    tag: str = "x",
    rate: Decimal = Decimal("0.5"),
    budget: Decimal | None = None,
    max_parallel: int = 4,
    disable_default: bool = True,
) -> Executor:
    """A single enabled llm_agent (the seeded default disabled by
    default so the capable one is the only eligible executor) -- the
    P3/P4 helper pattern."""
    await exec_svc.ensure_default_agent(s, org_id=org)
    if disable_default:
        default_agent = (
            await s.execute(
                select(Executor).where(
                    Executor.kind == ExecutorKind.llm_agent,
                    Executor.name == exec_svc.DEFAULT_AGENT_NAME,
                )
            )
        ).scalar_one()
        if default_agent.enabled:
            await exec_svc.update_executor(
                s,
                org_id=org,
                actor_id=user,
                executor_id=default_agent.id,
                expected_version=default_agent.version,
                values={"enabled": False},
            )
    return await exec_svc.create_executor(
        s,
        org_id=org,
        actor_id=user,
        kind=ExecutorKind.llm_agent,
        name=name,
        max_parallel=max_parallel,
        credit_rate_per_hour=rate,
        credit_budget=budget,
        capability_tags=[tag],
    )


async def _llm_task(
    s: AsyncSession,
    *,
    org: uuid.UUID,
    user: uuid.UUID,
    title: str,
    tag: str = "x",
    effort: Decimal = Decimal(2),
    assignee_id: uuid.UUID | None = None,
) -> object:
    # Without an explicit ai_assistant assignee, ``create_task`` would
    # default to the calling user (forcing ExecKind.human). Seed one
    # in-line if the caller did not supply it so the task lands on the
    # llm dispatch path.
    if assignee_id is None:
        ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
        assignee_id = ident.id
    return await tasks_svc.create_task(
        s,
        org_id=org,
        actor_id=user,
        title=title,
        estimate_effort_h=effort,
        assignee_id=assignee_id,
        required_capabilities=[tag],
    )


async def _grant_and_rate(s: AsyncSession, *, org: uuid.UUID, user: uuid.UUID) -> None:
    """Credits + a rate card on the agent model so a started run is
    really metered (executor.model_id is None -> the meter model is
    "agent")."""
    await billing.grant_credits(s, org_id=org, actor_id=user, amount=Decimal(100))
    await billing.upsert_rate_card(
        s,
        org_id=org,
        actor_id=user,
        model_id="agent",
        provider="local",
        values={"credits_per_input": "0.01", "credits_per_output": "0.02"},
    )


# --- (a) policy off -> no-op ------------------------------------------


async def test_policy_off_tick_is_noop(_fake_embedder: None) -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="OFF")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        await _capable_agent(s, org=org, user=user)
        await _llm_task(s, org=org, user=user, title="A")
        await _set_policy(s, org=org, policy=AutonomousDispatch.off)
        res = await loop.tick(s, org_id=org, actor_id=user, as_of=_AS_OF)
        assert res.enabled is False
        assert res.policy is AutonomousDispatch.off
        assert (res.created, res.dispatched, res.skipped, res.failed) == (0, 0, 0, 0)
        n_req = (await s.execute(select(func.count()).select_from(DispatchRequest))).scalar_one()
        n_run = (await s.execute(select(func.count()).select_from(AgentRun))).scalar_one()
        assert n_req == 0 and n_run == 0


# --- (b) approval_required -> creates a pending request, no run -------


async def test_approval_required_creates_pending_no_run(_fake_embedder: None) -> None:
    """The governance default: a tick creates ONE pending request per
    admitted agent task and starts NO run / spends NO credit."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="AR")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        agent = await _capable_agent(s, org=org, user=user)
        task = await _llm_task(s, org=org, user=user, title="Needs approval")
        # Default policy (unset) must resolve to approval_required.
        org_row = (await s.execute(select(Organization).where(Organization.id == org))).scalar_one()
        assert loop.resolve_policy(org_row) is AutonomousDispatch.approval_required

        res = await loop.tick(s, org_id=org, actor_id=user, as_of=_AS_OF)
        assert res.enabled is True
        assert res.policy is AutonomousDispatch.approval_required
        assert res.created == 1 and res.dispatched == 0 and res.approved == 0

        req = (
            await s.execute(select(DispatchRequest).where(DispatchRequest.task_id == task.id))
        ).scalar_one()
        assert req.status is DispatchStatus.pending
        assert req.executor_id == agent.id
        assert req.agent_run_id is None
        # Projected cost = effort_h * rate = 2 * 0.5 = 1.0 (the
        # scheduler's projection for the row).
        assert req.projected_credit_cost == Decimal("1.0000")
        n_run = (await s.execute(select(func.count()).select_from(AgentRun))).scalar_one()
        assert n_run == 0

        # Idempotent: a second tick with no state change creates no
        # duplicate request and starts no run.
        res2 = await loop.tick(s, org_id=org, actor_id=user, as_of=_AS_OF)
        assert res2.created == 0
        n_req = (await s.execute(select(func.count()).select_from(DispatchRequest))).scalar_one()
        assert n_req == 1


# --- (c) approve -> run starts (metered) ------------------------------


async def test_approve_starts_metered_run_via_api(_fake_embedder: None) -> None:
    """approve (owner) flips pending -> approved and IMMEDIATELY
    dispatches inline: the request becomes ``dispatched`` with an
    ``agent_run_id``, and the run is metered through the existing
    agentrun meter."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={
                    "email": _email(),
                    "password": "pw-strong-123",
                    "workspace_name": "AP",
                },
            )
        ).json()
    org = uuid.UUID(a["workspace_id"])
    user = uuid.UUID(decode_token(a["token"])["sub"])
    owner_h = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }
    member_h = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
    }

    async with tenant_session(str(org), str(user)) as s:
        await _capable_agent(s, org=org, user=user)
        task = await _llm_task(s, org=org, user=user, title="Approve me")
        await _grant_and_rate(s, org=org, user=user)

    _use_llm(_FINISH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # Tick (owner) -> a pending request appears in the queue.
        r = await c.post("/dispatch/tick", headers=owner_h, json={})
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 1 and r.json()["dispatched"] == 0

        q = await c.get("/dispatch/requests", headers=member_h)
        assert q.status_code == 200
        rows = q.json()
        assert len(rows) == 1
        req = rows[0]
        assert req["status"] == "pending"
        assert req["task_id"] == str(task.id)
        assert req["task_title"] == "Approve me"
        assert req["executor_name"] == "Worker"

        # A member cannot approve (owner-gated).
        rm = await c.post(
            f"/dispatch/requests/{req['id']}/approve",
            headers=member_h,
            json={"expected_version": req["version"]},
        )
        assert rm.status_code == 403
        assert rm.json()["code"] == "rbac.role_insufficient"

        # Owner approves -> inline dispatch: dispatched + agent_run_id.
        ra = await c.post(
            f"/dispatch/requests/{req['id']}/approve",
            headers=owner_h,
            json={"expected_version": req["version"]},
        )
        assert ra.status_code == 200, ra.text
        body = ra.json()
        assert body["status"] == "dispatched"
        assert body["agent_run_id"] is not None
    _clear_llm()

    async with tenant_session(str(org), str(user)) as s:
        run = (await s.execute(select(AgentRun).where(AgentRun.task_id == task.id))).scalar_one()
        assert run.status is AgentRunStatus.succeeded
        # Metered through the existing agentrun meter (>= one model
        # call * (10*0.01 + 5*0.02) = 0.20 each).
        assert run.credits_spent >= Decimal("0.2000")
        req_row = (
            await s.execute(select(DispatchRequest).where(DispatchRequest.task_id == task.id))
        ).scalar_one()
        assert req_row.status is DispatchStatus.dispatched
        assert req_row.agent_run_id == run.id
        assert req_row.decided_by == user

        # A second tick does not re-create / re-dispatch (idempotent:
        # the request is terminal, the task has a completed run).
        res = await loop.tick(s, org_id=org, actor_id=user, as_of=_AS_OF)
        assert res.created == 0 and res.dispatched == 0
        assert (await s.execute(select(func.count()).select_from(AgentRun))).scalar_one() == 1


# --- (d) deny -> no run; decided cannot be approved -------------------


async def test_deny_blocks_run_and_decided_cannot_approve(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={
                    "email": _email(),
                    "password": "pw-strong-123",
                    "workspace_name": "DN",
                },
            )
        ).json()
    org = uuid.UUID(a["workspace_id"])
    user = uuid.UUID(decode_token(a["token"])["sub"])
    owner_h = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }

    async with tenant_session(str(org), str(user)) as s:
        await _capable_agent(s, org=org, user=user)
        task = await _llm_task(s, org=org, user=user, title="Deny me")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        await c.post("/dispatch/tick", headers=owner_h, json={})
        rows = (await c.get("/dispatch/requests", headers=owner_h)).json()
        req = rows[0]

        rd = await c.post(
            f"/dispatch/requests/{req['id']}/deny",
            headers=owner_h,
            json={"expected_version": req["version"], "reason": "not now"},
        )
        assert rd.status_code == 200, rd.text
        assert rd.json()["status"] == "denied"
        assert rd.json()["reason"] == "not now"

        # A denied request cannot be approved (already decided).
        ra = await c.post(
            f"/dispatch/requests/{req['id']}/approve",
            headers=owner_h,
            json={"expected_version": rd.json()["version"]},
        )
        assert ra.status_code == 400
        assert ra.json()["code"] == "dispatch.not_pending"

    async with tenant_session(str(org), str(user)) as s:
        n_run = (
            await s.execute(
                select(func.count()).select_from(AgentRun).where(AgentRun.task_id == task.id)
            )
        ).scalar_one()
        assert n_run == 0


# --- (e) auto -> dispatches without manual approval -------------------


async def test_auto_policy_dispatches_without_approval(_fake_embedder: None) -> None:
    """``auto`` reduces the human click: a single tick admits, auto-
    approves and dispatches via the P3 metered path -- still bounded by
    the per-agent WIP / budget (the guardrails stay)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="AU")
    org, user = a.org_id, a.user_id
    _use_llm(_FINISH)
    async with tenant_session(str(org), str(user)) as s:
        agent = await _capable_agent(s, org=org, user=user)
        task = await _llm_task(s, org=org, user=user, title="Auto")
        await _grant_and_rate(s, org=org, user=user)
        await _set_policy(s, org=org, policy=AutonomousDispatch.auto)

        res = await loop.tick(s, org_id=org, actor_id=user, as_of=_AS_OF)
        assert res.policy is AutonomousDispatch.auto
        assert res.created == 1 and res.approved == 1 and res.dispatched == 1
        assert res.failed == 0

        req = (
            await s.execute(select(DispatchRequest).where(DispatchRequest.task_id == task.id))
        ).scalar_one()
        assert req.status is DispatchStatus.dispatched
        run = (await s.execute(select(AgentRun).where(AgentRun.task_id == task.id))).scalar_one()
        assert run.status is AgentRunStatus.succeeded
        assert run.executor_id == agent.id

        # Idempotent: a second auto tick neither re-creates nor
        # re-dispatches (the run completed; the request is terminal).
        res2 = await loop.tick(s, org_id=org, actor_id=user, as_of=_AS_OF)
        assert (res2.created, res2.dispatched, res2.failed) == (0, 0, 0)
        assert (await s.execute(select(func.count()).select_from(AgentRun))).scalar_one() == 1
    _clear_llm()


# --- (f) per-tick churn cap respected ---------------------------------


async def test_per_tick_dispatch_cap_respected(_fake_embedder: None) -> None:
    """With more admitted+approved tasks than the cap, a single tick
    dispatches at most ``max_dispatches`` and leaves the rest
    ``approved`` for the next tick (a throttle, not a drop)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="CAP")
    org, user = a.org_id, a.user_id
    _use_llm(_FINISH)
    async with tenant_session(str(org), str(user)) as s:
        # One roomy agent (max_parallel high so WIP is not the binding
        # constraint -- the per-tick cap is what we are testing).
        await _capable_agent(s, org=org, user=user, max_parallel=50, rate=Decimal(0))
        for i in range(5):
            await _llm_task(s, org=org, user=user, title=f"T{i}")
        await _set_policy(s, org=org, policy=AutonomousDispatch.auto)

        # Cap the tick at 2: exactly 2 dispatch, 3 stay approved.
        res = await loop.tick(s, org_id=org, actor_id=user, as_of=_AS_OF, max_dispatches=2)
        assert res.created == 5 and res.approved == 5
        assert res.dispatched == 2 and res.failed == 0
        approved_left = (
            await s.execute(
                select(func.count())
                .select_from(DispatchRequest)
                .where(DispatchRequest.status == DispatchStatus.approved)
            )
        ).scalar_one()
        dispatched = (
            await s.execute(
                select(func.count())
                .select_from(DispatchRequest)
                .where(DispatchRequest.status == DispatchStatus.dispatched)
            )
        ).scalar_one()
        assert dispatched == 2 and approved_left == 3

        # The next tick drains 2 more (still capped), none failed.
        res2 = await loop.tick(s, org_id=org, actor_id=user, as_of=_AS_OF, max_dispatches=2)
        assert res2.dispatched == 2 and res2.failed == 0
        assert res2.created == 0
    _clear_llm()


# --- (g) non-fatal isolation: one fails, another dispatches ----------


async def test_non_fatal_one_failed_other_dispatched_same_tick(
    _fake_embedder: None,
) -> None:
    """Two admitted agent tasks, two agents. Agent-B is at its
    ``max_parallel=1`` WIP (an unrelated in-flight run occupies it), so
    when the loop dispatches task-B's request the live WIP re-check
    marks THAT request ``failed`` (reason ``wip_exhausted``), while
    task-A dispatches successfully IN THE SAME TICK -- one bad task
    never aborts the tick or the others."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="NF")
    org, user = a.org_id, a.user_id
    _use_llm(_FINISH)
    async with tenant_session(str(org), str(user)) as s:
        # Agent A: roomy, capability "a". Agent B: max_parallel=1,
        # capability "b". The seeded default is disabled by the first
        # _capable_agent call.
        agent_a = await _capable_agent(s, org=org, user=user, name="A", tag="a", rate=Decimal(0))
        agent_b = await _capable_agent(
            s,
            org=org,
            user=user,
            name="B",
            tag="b",
            rate=Decimal(0),
            max_parallel=1,
            disable_default=False,
        )
        task_a = await _llm_task(s, org=org, user=user, title="TA", tag="a")
        task_b = await _llm_task(s, org=org, user=user, title="TB", tag="b")
        # An unrelated human task carrying an in-flight run that occupies
        # agent B's single slot (so B's live WIP == max_parallel).
        filler = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="filler", estimate_effort_h=Decimal(1)
        )
        s.add(
            AgentRun(
                org_id=org,
                task_id=filler.id,
                executor_id=agent_b.id,
                status=AgentRunStatus.running,
                steps=0,
                credits_spent=Decimal(0),
                started_at=dt.datetime.now(tz=dt.UTC),
            )
        )
        await s.flush()
        await _set_policy(s, org=org, policy=AutonomousDispatch.auto)

        res = await loop.tick(s, org_id=org, actor_id=user, as_of=_AS_OF)
        # Both admitted+approved; A dispatched, B failed -- the tick did
        # NOT abort on B.
        assert res.created == 2 and res.approved == 2
        assert res.dispatched == 1 and res.failed == 1

        req_a = (
            await s.execute(select(DispatchRequest).where(DispatchRequest.task_id == task_a.id))
        ).scalar_one()
        req_b = (
            await s.execute(select(DispatchRequest).where(DispatchRequest.task_id == task_b.id))
        ).scalar_one()
        assert req_a.status is DispatchStatus.dispatched
        assert req_a.agent_run_id is not None
        assert req_b.status is DispatchStatus.failed
        assert req_b.reason == "wip_exhausted"
        # Task A actually ran on agent A; task B never started a run.
        run_a = (
            await s.execute(select(AgentRun).where(AgentRun.task_id == task_a.id))
        ).scalar_one()
        assert run_a.executor_id == agent_a.id
        assert (
            await s.execute(
                select(func.count()).select_from(AgentRun).where(AgentRun.task_id == task_b.id)
            )
        ).scalar_one() == 0
    _clear_llm()


# --- (i) owner-gating: tick / policy-set ------------------------------


async def test_tick_and_policy_set_owner_gated_via_api(_fake_embedder: None) -> None:
    """A non-owner (no X-Workspace-Role -> effective role clamps to
    member) is denied the manual tick and the autonomous-policy set;
    the owner succeeds. Same assertion style as the P2/P4 privileged-op
    tests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={
                    "email": _email(),
                    "password": "pw-strong-123",
                    "workspace_name": "OG",
                },
            )
        ).json()
    owner_h = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }
    member_h = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # Manual tick is owner-gated.
        rm = await c.post("/dispatch/tick", headers=member_h, json={})
        assert rm.status_code == 403
        assert rm.json()["code"] == "rbac.role_insufficient"
        ro = await c.post("/dispatch/tick", headers=owner_h, json={})
        assert ro.status_code == 200

        # The autonomous policy is surfaced in the workspace settings
        # GET and is owner-gated to change.
        me = (await c.get("/workspaces/me", headers=member_h)).json()
        assert me["settings"]["autonomous_dispatch"] == "approval_required"
        ver = me["version"]

        bad = await c.patch(
            "/workspaces/me/settings",
            headers=member_h,
            json={
                "expected_version": ver,
                "estimate_presets": ["1", "2"],
                "autonomous_dispatch": "auto",
            },
        )
        assert bad.status_code == 403
        assert bad.json()["code"] == "rbac.role_insufficient"

        ok = await c.patch(
            "/workspaces/me/settings",
            headers=owner_h,
            json={
                "expected_version": ver,
                "estimate_presets": ["1", "2"],
                "autonomous_dispatch": "auto",
            },
        )
        assert ok.status_code == 200
        me2 = (await c.get("/workspaces/me", headers=member_h)).json()
        assert me2["settings"]["autonomous_dispatch"] == "auto"

        # An invalid policy value is rejected (validated enum).
        me3 = (await c.get("/workspaces/me", headers=owner_h)).json()
        bad_pol = await c.patch(
            "/workspaces/me/settings",
            headers=owner_h,
            json={
                "expected_version": me3["version"],
                "estimate_presets": ["1"],
                "autonomous_dispatch": "yolo",
            },
        )
        assert bad_pol.status_code == 422  # pydantic enum rejection


# --- service-level RBAC parity (approve/deny/tick owner-gated) --------


async def test_service_level_owner_gating(_fake_embedder: None) -> None:
    """The approve/deny/tick service entrypoints are owner-gated at the
    choke point: a plain member cannot call them (ForbiddenError),
    falling back to stored membership (no published GUC role in a direct
    service call -- the worker/test path)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="SG")
        b = await signup(s, email=_email(), password="pw-strong-123", org_name="SG2")
    org, owner, member = a.org_id, a.user_id, b.user_id

    async with tenant_session(str(org), str(owner)) as s:
        s.add(Membership(org_id=org, user_id=member, role=Role.member))
        await s.flush()
        await _capable_agent(s, org=org, user=owner)
        await _llm_task(s, org=org, user=owner, title="Gated")
        res = await loop.tick(s, org_id=org, actor_id=owner, as_of=_AS_OF)
        assert res.created == 1
        req = (await s.execute(select(DispatchRequest))).scalar_one()

    # A plain member cannot tick / approve / deny (owner-gated; the
    # member's stored membership is the fallback role).
    async with tenant_session(str(org), str(member)) as s:
        with pytest.raises(ForbiddenError):
            await loop.tick(s, org_id=org, actor_id=member, as_of=_AS_OF)
        with pytest.raises(ForbiddenError):
            await loop.approve_request(
                s,
                org_id=org,
                actor_id=member,
                request_id=req.id,
                expected_version=req.version,
            )
        with pytest.raises(ForbiddenError):
            await loop.deny_request(
                s,
                org_id=org,
                actor_id=member,
                request_id=req.id,
                expected_version=req.version,
            )

    # The owner still can (and a stale version is a concurrency error,
    # not a silent overwrite).
    async with tenant_session(str(org), str(owner)) as s:
        with pytest.raises(DomainError) as ei:
            await loop.deny_request(
                s,
                org_id=org,
                actor_id=owner,
                request_id=req.id,
                expected_version=req.version + 99,
            )
        assert ei.value.code.value == "concurrency.stale_version"
        out = await loop.deny_request(
            s,
            org_id=org,
            actor_id=owner,
            request_id=req.id,
            expected_version=req.version,
        )
        assert out.status is DispatchStatus.denied


async def test_tick_does_not_queue_a_task_addressed_to_an_external_assistant(
    _fake_embedder: None,
) -> None:
    """The trap's entrance, closed.

    ``_admitted_agent_rows`` read ``identities.kind == ai_assistant``, so
    every task an MCP client wrote for a person queued for a run that
    could never start: 210 requests, zero runs, all of them against the
    one ``llm_agent`` executor, which is eligible for everything because
    it advertises no capability tags. The requests sat harmless only
    because the governance default is ``approval_required``; one
    settings value flipped to ``auto`` would have approved all of them.

    An ``internal`` assistant in the same tick still queues, which is
    what separates this fix from switching the loop off.
    """
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="EXTQ")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        await _capable_agent(s, org=org, user=user)
        ext = await seed_ai_assistant_identity(
            s, org_id=org, user_id=user, label="ext", runtime=AssistantRuntime.external
        )
        internal = await seed_ai_assistant_identity(
            s, org_id=org, user_id=user, label="int", runtime=AssistantRuntime.internal
        )
        ext_task = await _llm_task(s, org=org, user=user, title="For a person", assignee_id=ext.id)
        int_task = await _llm_task(
            s, org=org, user=user, title="For the loop", assignee_id=internal.id
        )

        res = await loop.tick(s, org_id=org, actor_id=user, as_of=_AS_OF)
        assert res.created == 1

        n_ext = (
            await s.execute(
                select(func.count())
                .select_from(DispatchRequest)
                .where(DispatchRequest.task_id == ext_task.id)
            )
        ).scalar_one()
        assert n_ext == 0
        n_int = (
            await s.execute(
                select(func.count())
                .select_from(DispatchRequest)
                .where(DispatchRequest.task_id == int_task.id)
            )
        ).scalar_one()
        assert n_int == 1
