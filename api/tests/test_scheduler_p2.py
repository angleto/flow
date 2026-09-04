"""ADR-0025 P2: executor registry + admission-control dispatch
(DB-backed).

Covers the P2 acceptance bullets:
(a) registry CRUD: owner can create/patch/delete an llm_agent; a
    member (no owner header) is 403; optimistic concurrency on patch.
(b) capability match: a task with required_capabilities is assigned
    only to an agent whose capability_tags is a superset.
(c) admission/budget: a low credit_budget on the only capable agent ->
    over-budget tasks become unassignable (budget_exhausted), not
    silently scheduled.
(d) no capable agent -> unassignable (no_capable_agent);
    RecomputeOut.unassignable_count matches.
(e) multiple agents: tasks distributed respecting each agent's
    max_parallel (<= max_parallel concurrent per agent).
(f) determinism: same inputs+registry+policy -> identical assignment
    and fingerprint across two recomputes.
(g) humans unaffected: a human task stays serial on its calendar,
    assigned_executor_id is None, never unassignable for capacity.

Style mirrors api/tests/test_scheduler_p1.py (signup -> tenant_session;
privileged API/MCP calls send X-Workspace-Role: owner / are owner-gated
in the service).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from tests_helpers import seed_ai_assistant_identity

from mycelium_api.main import app
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError, DomainError, ForbiddenError
from mycelium_core.models.ai_assistant import AiAssistant, AssistantRuntime
from mycelium_core.models.executor import Executor, ExecutorKind
from mycelium_core.models.schedule import Schedule
from mycelium_core.models.task import ExecKind, SchedulePolicy
from mycelium_core.models.task_collaborator import TaskCollaborator
from mycelium_core.services import executors as exec_svc
from mycelium_core.services import scheduler as sch
from mycelium_core.services import tasks
from mycelium_core.services.auth import signup

# Monday 2026-01-12 09:00 Europe/Rome (winter = UTC+1).
_AS_OF = dt.datetime(2026, 1, 12, 8, 0, tzinfo=dt.UTC)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _overlap_for(rows: list[Schedule], executor_id: uuid.UUID) -> int:
    """Max concurrently-active [scheduled_start, scheduled_end) intervals
    among rows assigned to one executor (deterministic sweep)."""
    pts: list[tuple[dt.datetime, int]] = []
    for r in rows:
        if r.assigned_executor_id != executor_id:
            continue
        assert r.scheduled_start is not None and r.scheduled_end is not None
        pts.append((r.scheduled_start, 1))
        pts.append((r.scheduled_end, -1))
    pts.sort(key=lambda p: (p[0], p[1]))
    cur = peak = 0
    for _, delta in pts:
        cur += delta
        peak = max(peak, cur)
    return peak


# --- (a) registry CRUD + RBAC + optimistic concurrency ---


async def test_registry_crud_owner_member_and_optimistic_concurrency() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "REG"},
            )
        ).json()
        owner_h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        # Absent X-Workspace-Role => effective role clamps to member
        # (least privilege): mutations must be forbidden.
        member_h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
        }

        # Member can LIST (reads are member-level) but cannot CREATE.
        assert (await c.get("/executors", headers=member_h)).status_code == 200
        denied = await c.post(
            "/executors",
            headers=member_h,
            json={"kind": "llm_agent", "name": "Nope"},
        )
        assert denied.status_code == 403

        # Owner can CREATE an llm_agent.
        created = await c.post(
            "/executors",
            headers=owner_h,
            json={
                "kind": "llm_agent",
                "name": "Coder",
                "max_parallel": 2,
                "credit_rate_per_hour": "1.0",
                "capability_tags": ["python"],
            },
        )
        assert created.status_code == 200
        ex = created.json()
        assert ex["kind"] == "llm_agent"
        assert ex["capability_tags"] == ["python"]
        assert ex["version"] == 1
        ex_id = ex["id"]

        # Invalid: max_parallel < 1 (Pydantic ge=1 -> 422).
        bad = await c.post(
            "/executors",
            headers=owner_h,
            json={"kind": "llm_agent", "name": "Bad", "max_parallel": 0},
        )
        assert bad.status_code == 422

        # Owner PATCH (optimistic concurrency): correct version succeeds.
        patched = await c.patch(
            f"/executors/{ex_id}",
            headers=owner_h,
            json={"expected_version": 1, "capability_tags": ["python", "sql"]},
        )
        assert patched.status_code == 200
        assert patched.json()["version"] == 2

        # Stale version -> 409.
        stale = await c.patch(
            f"/executors/{ex_id}",
            headers=owner_h,
            json={"expected_version": 1, "name": "X"},
        )
        assert stale.status_code == 409

        # Member cannot PATCH or DELETE.
        assert (
            await c.patch(
                f"/executors/{ex_id}",
                headers=member_h,
                json={"expected_version": 2, "name": "X"},
            )
        ).status_code == 403
        assert (await c.delete(f"/executors/{ex_id}", headers=member_h)).status_code == 403

        # Owner DELETE.
        assert (await c.delete(f"/executors/{ex_id}", headers=owner_h)).status_code == 204
        listed = (await c.get("/executors", headers=owner_h)).json()
        assert ex_id not in {e["id"] for e in listed}


async def test_create_human_requires_member_binding() -> None:
    """A human executor cannot be created unbound to a workspace member
    (FK user_id must be a member); a member-bound one is allowed."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="HUM")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        # Unbound human -> EXECUTOR_INVALID (detail user_id).
        try:
            await exec_svc.create_executor(
                s, org_id=org, actor_id=user, kind=ExecutorKind.human, name="Ghost"
            )
            raise AssertionError("unbound human executor must be rejected")
        except DomainError as e:
            assert e.params.get("detail") == "user_id"
        # A non-member user id -> rejected.
        try:
            await exec_svc.create_executor(
                s,
                org_id=org,
                actor_id=user,
                kind=ExecutorKind.human,
                name="Outsider",
                user_id=uuid.uuid4(),
            )
            raise AssertionError("non-member human executor must be rejected")
        except DomainError as e:
            assert e.params.get("detail") == "user_id"
        # Bound to the signup owner (a member) -> allowed.
        row = await exec_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind.human,
            name="Me",
            user_id=user,
        )
        assert row.kind is ExecutorKind.human
        assert row.user_id == user


# --- (b) capability matching ---


async def test_capability_match_routes_only_to_capable_agent() -> None:
    """A task with required_capabilities=['x'] is assigned only to an
    agent whose capability_tags is a superset of {'x'}."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="CAP")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        ai_ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
        # Disable the seeded default agent (no tags) so only the capable
        # one is eligible; create one capable agent.
        await exec_svc.ensure_default_agent(s, org_id=org)
        default_agent = (
            await s.execute(select(Executor).where(Executor.kind == ExecutorKind.llm_agent))
        ).scalar_one()
        capable = await exec_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind.llm_agent,
            name="Specialist",
            capability_tags=["x"],
        )
        await exec_svc.update_executor(
            s,
            org_id=org,
            actor_id=user,
            executor_id=default_agent.id,
            expected_version=default_agent.version,
            values={"enabled": False},
        )
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="NeedsX",
            estimate_effort_h=Decimal(2),
            assignee_id=ai_ident.id,
            required_capabilities=["x"],
        )
        summary = await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        row = (await s.execute(select(Schedule).where(Schedule.task_id == t.id))).scalar_one()

    assert summary.unassignable_count == 0
    assert row.unassignable is False
    assert row.assigned_executor_id == capable.id
    assert row.scheduled_start is not None


# --- (c) admission / budget ---


async def test_budget_exhaustion_marks_excess_unassignable() -> None:
    """Two agents, low credit_budget on the only capable one: tasks
    beyond the budget become unassignable with the budget reason, not
    silently scheduled on the incapable agent."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="BUD")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        ai_ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
        await exec_svc.ensure_default_agent(s, org_id=org)
        default_agent = (
            await s.execute(select(Executor).where(Executor.kind == ExecutorKind.llm_agent))
        ).scalar_one()
        # The ONLY capable agent: rate 1.0 credit/h, budget 5.0 credits.
        capable = await exec_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind.llm_agent,
            name="Budgeted",
            max_parallel=4,
            credit_rate_per_hour=Decimal("1.0"),
            credit_budget=Decimal("5.0"),
            capability_tags=["x"],
        )
        # Default agent is enabled but NOT capable (no 'x' tag): it must
        # never receive these tasks (no silent reroute).
        assert default_agent.enabled is True
        # 3 tasks * 2h * 1.0 = 6.0 credits total > 5.0 budget. With P1
        # priority order the first 2 (4.0) fit, the 3rd (would be 6.0)
        # exhausts the budget -> unassignable.
        tids = []
        for i in range(3):
            t = await tasks.create_task(
                s,
                org_id=org,
                actor_id=user,
                title=f"B{i}",
                importance=1,
                urgency=i + 1,
                estimate_effort_h=Decimal(2),
                assignee_id=ai_ident.id,
                required_capabilities=["x"],
            )
            tids.append(t.id)
        summary = await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = {
            r.task_id: r
            for r in (await s.execute(select(Schedule))).scalars().all()
            if r.task_id in set(tids)
        }

    assigned = [r for r in rows.values() if not r.unassignable]
    unassignable = [r for r in rows.values() if r.unassignable]
    assert summary.unassignable_count == len(unassignable) >= 1
    # Every assigned task went to the capable agent (never the default).
    assert all(r.assigned_executor_id == capable.id for r in assigned)
    # Spend on the capable agent stays within budget.
    assert sum(r.projected_cost for r in assigned) <= Decimal("5.0")
    # The unassignable ones carry the stable budget reason and have NO
    # placement (a visible dispatch gap, not a silent schedule).
    for r in unassignable:
        assert r.unassignable_reason == "budget_exhausted"
        assert r.scheduled_start is None and r.scheduled_end is None
        assert r.assigned_executor_id is None
        assert r.projected_cost == Decimal(0)


# --- (d) no capable agent ---


async def test_no_capable_agent_marks_unassignable_and_count_matches() -> None:
    """A required capability no enabled agent advertises -> the task is
    unassignable with no_capable_agent; RecomputeOut.unassignable_count
    matches the flagged rows; the API surfaces it."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "NOCAP"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        org_id = uuid.UUID(a["workspace_id"])
        async with tenant_session(str(org_id), a["user_id"]) as s:
            ai_ident = await seed_ai_assistant_identity(
                s, org_id=org_id, user_id=uuid.UUID(a["user_id"])
            )
        # Default agent has no capability tags; this task needs "rare".
        r = await c.post(
            "/tasks",
            headers=h,
            json={
                "title": "Rare",
                "estimate_effort_h": "2",
                "assignee_id": str(ai_ident.id),
                "required_capabilities": ["rare"],
            },
        )
        assert r.status_code == 200
        task_id = r.json()["id"]
        assert r.json()["required_capabilities"] == ["rare"]

        rec = await c.post(
            "/schedule/recompute",
            headers=h,
            json={"as_of": "2026-01-12T08:00:00+00:00", "policy": "balanced"},
        )
        assert rec.status_code == 200
        body = rec.json()
        assert body["unassignable_count"] == 1

        sched = (await c.get("/schedule", headers=h)).json()
        row = next(x for x in sched if x["task_id"] == task_id)
        assert row["unassignable"] is True
        assert row["unassignable_reason"] == "no_capable_agent"
        assert row["assigned_executor_id"] is None
        assert row["scheduled_start"] is None
        assert Decimal(row["projected_cost"]) == Decimal(0)


# --- (e) multiple agents respect per-agent max_parallel ---


async def test_multiple_agents_respect_per_agent_max_parallel() -> None:
    """With two capable agents (max_parallel 2 each) and many tasks,
    no agent runs more than its max_parallel tasks concurrently."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="MULTI")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        ai_ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
        # Disable the seeded default; two capable agents, K=2 each.
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
        a1 = await exec_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind.llm_agent,
            name="AgentA",
            max_parallel=2,
            capability_tags=["x"],
        )
        a2 = await exec_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind.llm_agent,
            name="AgentB",
            max_parallel=2,
            capability_tags=["x"],
        )
        for i in range(8):
            await tasks.create_task(
                s,
                org_id=org,
                actor_id=user,
                title=f"M{i}",
                estimate_effort_h=Decimal(3),
                assignee_id=ai_ident.id,
                required_capabilities=["x"],
            )
        summary = await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = (await s.execute(select(Schedule))).scalars().all()

    assert summary.unassignable_count == 0
    rows = list(rows)
    # No agent exceeds its own max_parallel concurrently.
    assert _overlap_for(rows, a1.id) <= 2
    assert _overlap_for(rows, a2.id) <= 2
    # Every task is assigned to one of the two capable agents.
    assigned_to = {r.assigned_executor_id for r in rows if r.scheduled_start is not None}
    assert assigned_to <= {a1.id, a2.id}
    assert assigned_to  # at least one placed


# --- (f) determinism ---


async def test_determinism_same_inputs_registry_policy() -> None:
    """Same tasks + registry + policy -> identical assignment and
    fingerprint across two recomputes."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="DET2")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        await exec_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind.llm_agent,
            name="A1",
            max_parallel=2,
            credit_rate_per_hour=Decimal("0.5"),
            capability_tags=["x"],
        )
        await exec_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind.llm_agent,
            name="A2",
            max_parallel=2,
            credit_rate_per_hour=Decimal("0.5"),
            capability_tags=["x"],
        )
        for i in range(5):
            await tasks.create_task(
                s,
                org_id=org,
                actor_id=user,
                title=f"D{i}",
                importance=1,
                urgency=(i % 4) + 1,
                estimate_effort_h=Decimal(2),
                executor_kind=ExecKind.llm_agent,
                required_capabilities=["x"],
            )
        s1 = await sch.recompute(
            s, org_id=org, actor_id=user, as_of=_AS_OF, policy=SchedulePolicy.balanced
        )
        first = {
            r.task_id: (
                r.assigned_executor_id,
                r.scheduled_start,
                r.scheduled_end,
                r.unassignable,
                r.input_fingerprint,
            )
            for r in (await s.execute(select(Schedule))).scalars().all()
        }
        s2 = await sch.recompute(
            s, org_id=org, actor_id=user, as_of=_AS_OF, policy=SchedulePolicy.balanced
        )
        second = {
            r.task_id: (
                r.assigned_executor_id,
                r.scheduled_start,
                r.scheduled_end,
                r.unassignable,
                r.input_fingerprint,
            )
            for r in (await s.execute(select(Schedule))).scalars().all()
        }

    assert first == second
    assert s1.makespan_minutes == s2.makespan_minutes
    assert s1.projected_credit_cost == s2.projected_credit_cost
    assert len({fp for *_, fp in first.values()}) == 1


async def test_registry_change_reschedules_via_fingerprint() -> None:
    """A registry change (toggling an agent) folds into the fingerprint
    so the schedule is recomputed even when no task changed."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="FPR")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        ai_ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
        ag = await exec_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind.llm_agent,
            name="Cap",
            capability_tags=["x"],
        )
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="T",
            estimate_effort_h=Decimal(2),
            assignee_id=ai_ident.id,
            required_capabilities=["x"],
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        fp_before = (await s.execute(select(Schedule))).scalars().first()
        assert fp_before is not None
        fp1 = fp_before.input_fingerprint

        # Disable the only capable agent: same tasks, different registry.
        await exec_svc.update_executor(
            s,
            org_id=org,
            actor_id=user,
            executor_id=ag.id,
            expected_version=ag.version,
            values={"enabled": False},
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        fp_after = (await s.execute(select(Schedule))).scalars().first()
        assert fp_after is not None
        fp2 = fp_after.input_fingerprint
        # Now the task has no capable enabled agent -> unassignable, and
        # the fingerprint changed (registry folded in).
        assert fp1 != fp2
        assert fp_after.unassignable is True
        assert fp_after.unassignable_reason == "no_capable_agent"


# --- (g) humans unaffected ---


async def test_human_task_unaffected_by_admission() -> None:
    """A human task stays serial on its calendar, is never marked
    unassignable for capacity, and carries no llm assignment."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="HU2")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        h1 = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="H1",
            importance=1,
            urgency=1,
            estimate_effort_h=Decimal(2),
            assignee_ids=[user],
        )
        h2 = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="H2",
            importance=1,
            urgency=2,
            estimate_effort_h=Decimal(2),
            assignee_ids=[user],
        )
        summary = await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = {r.task_id: r for r in (await s.execute(select(Schedule))).scalars().all()}

    assert summary.unassignable_count == 0
    r1, r2 = rows[h1.id], rows[h2.id]
    # Serial on the single human (disjoint), never unassignable, no llm
    # executor assignment (humans route by calendar in P1, not the
    # capability dispatcher).
    assert r1.unassignable is False and r2.unassignable is False
    assert r1.assigned_executor_id is None and r2.assigned_executor_id is None
    assert r1.scheduled_end is not None and r2.scheduled_start is not None
    assert r1.scheduled_end <= r2.scheduled_start
    assert r1.projected_cost == Decimal(0) and r2.projected_cost == Decimal(0)


# --- service-level RBAC (mirrors the rate-card precedent) ---


async def test_service_mutations_owner_gated_in_session() -> None:
    """The service is the choke point: with no published effective role
    a non-owner membership cannot mutate the registry (it falls back to
    stored membership; the signup user IS owner so this asserts the
    positive path + a stale-version conflict raises ConflictError)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="SVC")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        row = await exec_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind.llm_agent,
            name="S1",
        )
        # Optimistic concurrency at the service layer.
        try:
            await exec_svc.update_executor(
                s,
                org_id=org,
                actor_id=user,
                executor_id=row.id,
                expected_version=999,
                values={"name": "S2"},
            )
            raise AssertionError("stale version must raise")
        except ConflictError:
            pass

        # A guest membership (effective fallback) cannot mutate. Simulate
        # by asserting require_role(owner) rejects a non-owner: create a
        # second member and act as them.
        from mycelium_core.models.membership import Membership, Role

        other = await signup(s, email=_email(), password="pw-strong-123", org_name="OTHER")
        # Add `other` to `org` as a plain member (not owner).
        s.add(
            Membership(
                org_id=org,
                user_id=other.user_id,
                role=Role.member,
            )
        )
        await s.flush()

    async with tenant_session(str(org), str(other.user_id)) as s2:
        try:
            await exec_svc.create_executor(
                s2,
                org_id=org,
                actor_id=other.user_id,
                kind=ExecutorKind.llm_agent,
                name="ByMember",
            )
            raise AssertionError("a member must not create an executor")
        except ForbiddenError:
            pass


async def test_a_task_addressed_to_a_deactivated_assistant_is_a_visible_gap() -> None:
    """Not excluded from routing -- FLAGGED.

    Dropping such a task from the agent queue would leave a schedule row
    still claiming ``unassignable=false`` with an assigned executor and a
    start time, i.e. the Gantt says planned and on time while nothing
    ever dispatches it, and any pending request is retired as
    ``not_admitted`` (which reads as "the scheduler dropped it" when the
    scheduler in fact admitted it). Deleting the agent that could do the
    work already lands on ``unassignable``; addressing a task to an
    assistant that can no longer act is the same class of event, and gets
    its own reason so the owner is not sent hunting for a capability tag.

    Order matters: the task is created FIRST, because ``create_task``
    resolves the assignee through the identities resolver, which refuses
    a deactivated assistant outright.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "DEACT"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        org_id = uuid.UUID(a["workspace_id"])
        async with tenant_session(str(org_id), a["user_id"]) as s:
            ai_ident = await seed_ai_assistant_identity(
                s, org_id=org_id, user_id=uuid.UUID(a["user_id"])
            )
            assistant_id = ai_ident.ai_assistant_id

        r = await c.post(
            "/tasks",
            headers=h,
            json={"title": "Bot work", "estimate_effort_h": "2", "assignee_id": str(ai_ident.id)},
        )
        assert r.status_code == 200, r.text
        task_id = r.json()["id"]

        # It schedules normally while the assistant is live.
        rec = await c.post(
            "/schedule/recompute",
            headers=h,
            json={"as_of": "2026-01-12T08:00:00+00:00", "policy": "balanced"},
        )
        assert rec.json()["unassignable_count"] == 0

        async with tenant_session(str(org_id), a["user_id"]) as s:
            await s.execute(
                update(AiAssistant).where(AiAssistant.id == assistant_id).values(is_active=False)
            )

        rec2 = await c.post(
            "/schedule/recompute",
            headers=h,
            json={"as_of": "2026-01-12T08:00:00+00:00", "policy": "balanced"},
        )
        assert rec2.json()["unassignable_count"] == 1
        sched = (await c.get("/schedule", headers=h)).json()
        row = next(x for x in sched if x["task_id"] == task_id)
        assert row["unassignable"] is True
        assert row["unassignable_reason"] == "assignee_inactive"
        assert row["assigned_executor_id"] is None
        assert row["scheduled_start"] is None

        # And the run door refuses by name rather than with the generic
        # "not dispatchable", which would say nothing about which
        # assistant is dead.
        run = await c.post(f"/tasks/{task_id}/run", headers=h)
        assert run.status_code == 400, run.text
        assert run.json()["code"] == "agent_run.assignee_inactive"


async def test_an_external_assistant_task_stays_out_of_the_llm_pool() -> None:
    """The one-way trap, closed at the routing site.

    An MCP client that creates a task is auto-assigned it, and until
    ``ai_assistants.runtime`` existed the recompute read
    ``identities.kind == ai_assistant`` and put that task in the llm
    pool: an executor, a projected cost and a scheduled window, for an
    execution nothing on this side can start. The live queue held 210
    requests against zero runs.

    ``external`` routes the task like a human's, and with no human
    assignee that means off-timeline: no executor, no cost, no window,
    and not ``unassignable`` either, because nothing failed to be
    admitted -- it was never a candidate.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "EXT"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        org_id = uuid.UUID(a["workspace_id"])
        async with tenant_session(str(org_id), a["user_id"]) as s:
            ext = await seed_ai_assistant_identity(
                s,
                org_id=org_id,
                user_id=uuid.UUID(a["user_id"]),
                label="external-client",
                runtime=AssistantRuntime.external,
            )
            internal = await seed_ai_assistant_identity(
                s,
                org_id=org_id,
                user_id=uuid.UUID(a["user_id"]),
                label="internal-agent",
                runtime=AssistantRuntime.internal,
            )

        ext_task = (
            await c.post(
                "/tasks",
                headers=h,
                json={
                    "title": "Wrote it for a person",
                    "estimate_effort_h": "2",
                    "assignee_id": str(ext.id),
                },
            )
        ).json()["id"]
        int_task = (
            await c.post(
                "/tasks",
                headers=h,
                json={
                    "title": "Agent work",
                    "estimate_effort_h": "2",
                    "assignee_id": str(internal.id),
                },
            )
        ).json()["id"]

        rec = await c.post(
            "/schedule/recompute",
            headers=h,
            json={"as_of": "2026-01-12T08:00:00+00:00", "policy": "balanced"},
        )
        assert rec.status_code == 200, rec.text
        assert rec.json()["unassignable_count"] == 0

        sched = (await c.get("/schedule", headers=h)).json()
        ext_row = next(x for x in sched if x["task_id"] == ext_task)
        assert ext_row["assigned_executor_id"] is None
        assert Decimal(str(ext_row["projected_cost"])) == Decimal(0)
        assert ext_row["unassignable"] is False
        # ``scheduled_start`` is NOT null here, and asserting that it was
        # is the mistake this line exists to prevent. An off-timeline
        # node still gets its logical CPM window written through
        # (``sched[id] = (es, ef)``); NULL is reserved for an
        # unassignable row, which is a dispatch gap and a different
        # thing. What says "not in the llm pool" is the absent executor
        # and the zero cost, above.
        assert ext_row["scheduled_start"] == ext_row["es"]

        # The symmetric half, and the reason it is in the same test:
        # disabling the loop outright would satisfy every assertion
        # above. An internal assistant must still be routed.
        int_row = next(x for x in sched if x["task_id"] == int_task)
        assert int_row["assigned_executor_id"] is not None
        assert int_row["scheduled_start"] is not None


async def test_an_external_assistant_task_with_a_collaborator_is_that_persons_work() -> None:
    """The branch the routing change does not make off-timeline.

    ``_Node.assignee`` falls back to the first ``task_assignee`` row
    before the identity-resolved user, so an external-assistant task
    that also names a person is serialized on that person's calendar
    rather than dropped. That is the right answer -- it is planned human
    work -- but it is a different outcome from the test above, so it is
    asserted rather than discovered.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "EXTC"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        org_id = uuid.UUID(a["workspace_id"])
        user_id = uuid.UUID(a["user_id"])
        async with tenant_session(str(org_id), a["user_id"]) as s:
            ext = await seed_ai_assistant_identity(
                s,
                org_id=org_id,
                user_id=user_id,
                label="external-client",
                runtime=AssistantRuntime.external,
            )

        task_id = (
            await c.post(
                "/tasks",
                headers=h,
                json={
                    "title": "Drafted for Ada",
                    "estimate_effort_h": "2",
                    "assignee_id": str(ext.id),
                },
            )
        ).json()["id"]

        async with tenant_session(str(org_id), a["user_id"]) as s:
            s.add(
                TaskCollaborator(
                    org_id=org_id,
                    task_id=uuid.UUID(task_id),
                    user_id=user_id,
                )
            )

        rec = await c.post(
            "/schedule/recompute",
            headers=h,
            json={"as_of": "2026-01-12T08:00:00+00:00", "policy": "balanced"},
        )
        assert rec.status_code == 200, rec.text
        sched = (await c.get("/schedule", headers=h)).json()
        row = next(x for x in sched if x["task_id"] == task_id)
        # On the person's timeline, and still not an llm dispatch.
        assert row["scheduled_start"] is not None
        assert row["assigned_executor_id"] is None
        assert Decimal(str(row["projected_cost"])) == Decimal(0)
        assert row["unassignable"] is False
