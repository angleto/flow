"""ADR-0025 P4: coordination / handoff protocol + contract-net.

A handoff is a typed message bound to a DAG edge: on a task reaching a
terminal workflow state, the producer's artifact + a system message is
delivered to each dependent task's RESOLVED executor -- a human via the
notification + note<->task substrate, an llm_agent via the P3 runtime
context. Same primitive for human<->human, human<->LLM, LLM<->LLM,
LLM<->human. Plus the human-side contract-net primitive
(offer/claim/decline); the llm_agent "award" is the P2 admission
dispatch and is NOT re-implemented here.

Covers the P4 acceptance bullets:
(a) human->human: a predecessor with a human-assigned successor;
    completing the predecessor (set_state into terminal) creates a
    delivered TaskHandoff, a ``task_handoff`` notification for the
    successor's user, and links the predecessor artifact note to the
    successor task.
(b) ->LLM: the successor's executor is an llm_agent -> the handoff
    stays pending; ``agent_runtime._build_context`` for the successor
    then includes the predecessor's message + artifact, and starting a
    run marks the handoff consumed.
(c) idempotent: re-entering the same terminal state -> no duplicate
    handoff/notification; a non-terminal transition -> no handoff.
(d) contract-net: offer (owner) -> an eligible member gets a
    ``task_offer`` notification; member claim -> becomes assignee +
    offered cleared; claim of a non-offered task -> 400
    TASK_NOT_OFFERED; a second claim -> 400 TASK_ALREADY_CLAIMED;
    decline notifies the offerer; a member cannot offer (403).
(e) cross-org isolation (a foreign task -> not found / no leak).
(f) set_state still works for a task with no dependents (no
    regression) and a handoff-delivery failure never blocks the
    transition.

Style mirrors api/tests/test_agent_runtime_p3.py (the ScriptedLLM +
FakeEmbedder fixture-override + ``_dispatched_llm_task`` pattern) and
test_f8_notifications.py (signup/deps helpers). Privileged ops send
``X-Workspace-Role: owner`` (offer is owner-gated).
"""

from __future__ import annotations

import asyncio
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
from mycelium_core.errors import DomainError, ForbiddenError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.dependency import DependencyType
from mycelium_core.models.executor import Executor, ExecutorKind
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.notification import Notification
from mycelium_core.models.schedule import Schedule
from mycelium_core.models.task_collaborator import TaskCollaborator
from mycelium_core.models.task_handoff import HandoffStatus, TaskHandoff
from mycelium_core.models.user import User
from mycelium_core.security import decode_token
from mycelium_core.services import agent_runtime as runtime
from mycelium_core.services import coordination as coord
from mycelium_core.services import dependencies as deps
from mycelium_core.services import executors as exec_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import scheduler as sch
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import workflow as wf
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
        if self._i < len(self._script):
            text = self._script[self._i]
        else:
            text = '{"tool": "finish", "args": {"output": "done"}}'
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


async def _states(s: AsyncSession, org: uuid.UUID) -> dict[str, uuid.UUID]:
    d = await wf.get_default_workflow(s, org)
    return {x.name: x.id for x in await wf.get_states(s, d.id)}


async def _complete(
    s: AsyncSession,
    *,
    org: uuid.UUID,
    user: uuid.UUID,
    task_id: uuid.UUID,
    version: int,
    st: dict[str, uuid.UUID],
) -> int:
    """Drive the default workflow todo -> in_progress -> done (done is
    the terminal state). Returns the version after reaching done."""
    v = await tasks_svc.set_state(
        s,
        org_id=org,
        actor_id=user,
        task_id=task_id,
        expected_version=version,
        state_id=st["in_progress"],
    )
    return await tasks_svc.set_state(
        s,
        org_id=org,
        actor_id=user,
        task_id=task_id,
        expected_version=v,
        state_id=st["done"],
    )


async def _capable_agent(s: AsyncSession, *, org: uuid.UUID, user: uuid.UUID) -> Executor:
    """A single enabled llm_agent (the seeded default agent disabled so
    it is the only eligible executor) -- the P3 helper pattern."""
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
    return await exec_svc.create_executor(
        s,
        org_id=org,
        actor_id=user,
        kind=ExecutorKind.llm_agent,
        name="Worker",
        max_parallel=4,
        credit_rate_per_hour=Decimal("0.5"),
        capability_tags=["x"],
    )


# --- (a) human -> human ------------------------------------------------


async def test_human_to_human_handoff_delivered_with_artifact_link(
    _fake_embedder: None,
) -> None:
    """Predecessor (human) with a work-note artifact -> a human-assigned
    successor. Completing the predecessor creates a DELIVERED
    TaskHandoff, a ``task_handoff`` notification for the successor's
    user, and links the predecessor artifact note to the successor."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="HH")
        b = await signup(s, email=_email(), password="pw-strong-123", org_name="HH2")
    org, user = a.org_id, a.user_id
    succ_user = b.user_id

    async with tenant_session(str(org), str(user)) as s:
        # The successor's assignee must be a member of the org.
        s.add(Membership(org_id=org, user_id=succ_user, role=Role.member))
        await s.flush()
        pred = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="Producer", estimate_effort_h=Decimal(1)
        )
        succ = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Consumer",
            estimate_effort_h=Decimal(1),
            assignee_ids=[succ_user],
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=pred.id,
            successor_id=succ.id,
            type=DependencyType.FS,
        )
        # The producer's work-note artifact.
        art = await notes_svc.create_note_for_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=pred.id,
            title="Findings",
            text="Investigated and resolved.",
        )
        st = await _states(s, org)
        await _complete(s, org=org, user=user, task_id=pred.id, version=pred.version, st=st)

        # A delivered handoff for the edge.
        ho = (
            await s.execute(
                select(TaskHandoff).where(
                    TaskHandoff.predecessor_task_id == pred.id,
                    TaskHandoff.successor_task_id == succ.id,
                )
            )
        ).scalar_one()
        assert ho.status is HandoffStatus.delivered
        assert ho.delivered_at is not None
        assert ho.artifact_note_id == art.id
        assert "Producer" in ho.message

        # A task_handoff notification exists for the successor's user.
        notifs = (
            (
                await s.execute(
                    select(Notification).where(
                        Notification.user_id == succ_user,
                        Notification.kind == "task_handoff",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notifs) == 1

        # The artifact note is now linked to the successor task (context
        # for the human) -- ADR-0029 P3 stores this as a polymorphic
        # ``NoteTaskLink`` of kind=artifact. The predecessor keeps its
        # own artifact link to the same note; both coexist.
        from mycelium_core.models.note_link import NoteTaskLink

        link = (
            await s.execute(
                select(NoteTaskLink).where(
                    NoteTaskLink.note_id == art.id,
                    NoteTaskLink.task_id == succ.id,
                    NoteTaskLink.kind == "artifact",
                )
            )
        ).scalar_one_or_none()
        assert link is not None


# --- (b) -> LLM: pending, surfaced in context, consumed on run --------


async def test_handoff_to_llm_pending_then_in_context_then_consumed(
    _fake_embedder: None,
) -> None:
    """The successor's resolved executor is an llm_agent: the handoff
    stays PENDING (no notification). ``_build_context`` for the
    successor includes the predecessor message + artifact body, and
    starting a run marks the handoff consumed."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LL")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        agent = await _capable_agent(s, org=org, user=user)
        ai_ident = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
        pred = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="UpstreamWork", estimate_effort_h=Decimal(1)
        )
        succ = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="AgentDownstream",
            estimate_effort_h=Decimal(2),
            assignee_id=ai_ident.id,
            required_capabilities=["x"],
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=pred.id,
            successor_id=succ.id,
            type=DependencyType.FS,
        )
        art = await notes_svc.create_note_for_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=pred.id,
            title="UpstreamArtifact",
            text="The upstream conclusion is 42.",
        )
        # Dispatch so the successor resolves to the llm_agent executor.
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        row = (await s.execute(select(Schedule).where(Schedule.task_id == succ.id))).scalar_one()
        assert row.assigned_executor_id == agent.id and row.unassignable is False

        st = await _states(s, org)
        await _complete(s, org=org, user=user, task_id=pred.id, version=pred.version, st=st)

        # Stays pending: an llm_agent successor is NOT notified.
        ho = (
            await s.execute(select(TaskHandoff).where(TaskHandoff.successor_task_id == succ.id))
        ).scalar_one()
        assert ho.status is HandoffStatus.pending
        assert ho.delivered_at is None
        assert ho.artifact_note_id == art.id
        n_count = (
            await s.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.kind == "task_handoff")
            )
        ).scalar_one()
        assert n_count == 0

        # _build_context for the successor surfaces the handoff
        # (predecessor title + message + artifact body), deterministic.
        succ_obj = await tasks_svc.get_task(s, org_id=org, task_id=succ.id)
        ctx = await runtime._build_context(s, org_id=org, task=succ_obj)
        blob = ctx[0][1]
        assert "Handoff from [UpstreamWork]:" in blob
        assert "UpstreamWork" in ho.message and ho.message in blob
        assert "The upstream conclusion is 42." in blob

        # Starting a run on the successor marks the handoff consumed.
        _use_llm(['{"tool": "finish", "args": {"output": "ok"}}'])
        run = await runtime.start_run(s, org_id=org, actor_id=user, task_id=succ.id)
        _clear_llm()
        assert run.status.value == "succeeded"
        await s.refresh(ho)
        assert ho.status is HandoffStatus.consumed
        assert ho.consumed_at is not None
        # Idempotent: it is no longer pending, so it is no longer in a
        # fresh context build, and a second consume finds nothing.
        ctx2 = await runtime._build_context(s, org_id=org, task=succ_obj)
        assert "Handoff from [UpstreamWork]:" not in ctx2[0][1]
        again = await coord.mark_incoming_consumed(s, org_id=org, actor_id=user, task_id=succ.id)
        assert again == 0


# --- (c) idempotency + non-terminal -----------------------------------


async def test_idempotent_recompletion_and_non_terminal_no_handoff(
    _fake_embedder: None,
) -> None:
    """Re-entering the SAME terminal state creates no duplicate handoff
    / notification; a NON-terminal transition creates no handoff."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ID")
        b = await signup(s, email=_email(), password="pw-strong-123", org_name="ID2")
    org, user, succ_user = a.org_id, a.user_id, b.user_id

    async with tenant_session(str(org), str(user)) as s:
        s.add(Membership(org_id=org, user_id=succ_user, role=Role.member))
        await s.flush()
        pred = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="P", estimate_effort_h=Decimal(1)
        )
        succ = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="S",
            estimate_effort_h=Decimal(1),
            assignee_ids=[succ_user],
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=pred.id,
            successor_id=succ.id,
            type=DependencyType.FS,
        )
        st = await _states(s, org)

        # Non-terminal transition (todo -> in_progress): NO handoff.
        v1 = await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=pred.id,
            expected_version=pred.version,
            state_id=st["in_progress"],
        )
        assert (
            await s.execute(
                select(func.count())
                .select_from(TaskHandoff)
                .where(TaskHandoff.predecessor_task_id == pred.id)
            )
        ).scalar_one() == 0

        # Cross into terminal (in_progress -> done): one handoff +
        # one notification.
        v2 = await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=pred.id,
            expected_version=v1,
            state_id=st["done"],
        )
        assert (
            await s.execute(
                select(func.count())
                .select_from(TaskHandoff)
                .where(TaskHandoff.predecessor_task_id == pred.id)
            )
        ).scalar_one() == 1
        assert (
            await s.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.kind == "task_handoff")
            )
        ).scalar_one() == 1

        # Re-set the SAME terminal state (done -> done, a no-op
        # transition, was_terminal == now_terminal): the hook does NOT
        # fire -> no duplicate handoff / notification. (set_state still
        # bumps the optimistic version even for a same-state write, so
        # thread the returned version forward.)
        v2b = await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=pred.id,
            expected_version=v2,
            state_id=st["done"],
        )
        assert (
            await s.execute(
                select(func.count())
                .select_from(TaskHandoff)
                .where(TaskHandoff.predecessor_task_id == pred.id)
            )
        ).scalar_one() == 1
        assert (
            await s.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.kind == "task_handoff")
            )
        ).scalar_one() == 1

        # And a full re-completion (done -> in_progress -> done)
        # REFRESHES the active row in place (still exactly one).
        v3 = await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=pred.id,
            expected_version=v2b,
            state_id=st["in_progress"],
        )
        await tasks_svc.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=pred.id,
            expected_version=v3,
            state_id=st["done"],
        )
        rows = (
            (await s.execute(select(TaskHandoff).where(TaskHandoff.predecessor_task_id == pred.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1


# --- (d) contract-net: offer / claim / decline ------------------------


async def test_contract_net_offer_claim_decline_via_api() -> None:
    """offer (owner) announces to eligible members; a member claim
    awards the task (assignee + offered cleared) and notifies the
    offerer; a non-offered claim -> 400 TASK_NOT_OFFERED; a second
    claim -> 400 TASK_ALREADY_CLAIMED; decline notifies the offerer; a
    member cannot offer (owner-gated, 403)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={
                    "email": _email(),
                    "password": "pw-strong-123",
                    "workspace_name": "CN",
                },
            )
        ).json()
    org = uuid.UUID(a["workspace_id"])
    owner = uuid.UUID(decode_token(a["token"])["sub"])
    owner_h = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }
    member_h = {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
    }

    # A second user, a plain member of the owner's org.
    async with admin_session() as s:
        m = await signup(s, email=_email(), password="pw-strong-123", org_name="MX")
    async with tenant_session(str(org), str(owner)) as s:
        s.add(Membership(org_id=org, user_id=m.user_id, role=Role.member))
        await s.flush()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        t = (await c.post("/tasks", headers=owner_h, json={"title": "Open task"})).json()
        tid = t["id"]
        assert t["offered"] is False

        # Claim before offer -> 400 TASK_NOT_OFFERED.
        r = await c.post(f"/tasks/{tid}/claim", headers=member_h)
        assert r.status_code == 400
        assert r.json()["code"] == "task.not_offered"

        # A member cannot offer (owner-gated).
        r = await c.post(f"/tasks/{tid}/offer", headers=member_h)
        assert r.status_code == 403
        assert r.json()["code"] == "rbac.role_insufficient"

        # Owner offers -> task marked offered.
        r = await c.post(f"/tasks/{tid}/offer", headers=owner_h)
        assert r.status_code == 200
        assert r.json()["offered"] is True

    # An eligible member (here every member, the task requires no
    # capability) got a task_offer notification.
    async with tenant_session(str(org), str(owner)) as s:
        offers = (
            (await s.execute(select(Notification).where(Notification.kind == "task_offer")))
            .scalars()
            .all()
        )
        # The owner is also a member -> announced to all members.
        assert {n.user_id for n in offers} >= {owner, m.user_id}
        assert any("offered" in (n.title or "").lower() for n in offers)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # The member claims it -> becomes the assignee, offered cleared.
        r = await c.post(f"/tasks/{tid}/claim", headers=member_h)
        assert r.status_code == 200
        body = r.json()
        assert body["offered"] is False

    async with tenant_session(str(org), str(owner)) as s:
        assignees = (
            (
                await s.execute(
                    select(TaskCollaborator.user_id).where(
                        TaskCollaborator.task_id == uuid.UUID(tid)
                    )
                )
            )
            .scalars()
            .all()
        )
        # The CALLER of /claim is the token's subject (the owner here);
        # the claim awards the task to that caller and clears offered.
        assert owner in assignees
        # The offerer was notified of the claim.
        claimed = (
            (
                await s.execute(
                    select(Notification).where(
                        Notification.kind == "task_claimed",
                        Notification.title.ilike("Task claimed:%"),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(claimed) == 1

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # A second claim on the now-awarded task -> 400 ALREADY_CLAIMED.
        # (Re-offer first so it is offered again but already has an
        # assignee.)
        r = await c.post(f"/tasks/{tid}/offer", headers=owner_h)
        assert r.status_code == 200
        r = await c.post(f"/tasks/{tid}/claim", headers=member_h)
        assert r.status_code == 400
        assert r.json()["code"] == "task.already_claimed"

        # Decline a (still-offered) task notifies the offerer; does not
        # assign and leaves offered set. Use a fresh offered task.
        t2 = (await c.post("/tasks", headers=owner_h, json={"title": "Declinable"})).json()
        await c.post(f"/tasks/{t2['id']}/offer", headers=owner_h)
        r = await c.post(f"/tasks/{t2['id']}/decline", headers=member_h)
        assert r.status_code == 200
        assert r.json()["offered"] is True  # still open for others

    async with tenant_session(str(org), str(owner)) as s:
        declined = (
            (
                await s.execute(
                    select(Notification).where(
                        Notification.kind == "task_declined",
                        Notification.title.ilike("Task declined:%"),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(declined) == 1
        # decline did NOT create an assignee.
        assert (
            await s.execute(
                select(func.count())
                .select_from(TaskCollaborator)
                .where(TaskCollaborator.task_id == uuid.UUID(t2["id"]))
            )
        ).scalar_one() == 0


# --- (e) cross-org isolation ------------------------------------------


async def test_cross_org_isolation(_fake_embedder: None) -> None:
    """Handoffs and contract-net ops in org A are invisible/unreachable
    from org B (RLS): listing handoffs for A's task from B yields none;
    offer/claim/decline of a foreign task -> TASK_NOT_FOUND."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="OA")
        b = await signup(s, email=_email(), password="pw-strong-123", org_name="OB")
    org_a, user_a = a.org_id, a.user_id
    org_b, user_b = b.org_id, b.user_id

    async with tenant_session(str(org_a), str(user_a)) as s:
        pred = await tasks_svc.create_task(
            s, org_id=org_a, actor_id=user_a, title="A-pred", estimate_effort_h=Decimal(1)
        )
        succ = await tasks_svc.create_task(
            s,
            org_id=org_a,
            actor_id=user_a,
            title="A-succ",
            estimate_effort_h=Decimal(1),
            assignee_ids=[user_a],
        )
        await deps.add_dependency(
            s,
            org_id=org_a,
            actor_id=user_a,
            predecessor_id=pred.id,
            successor_id=succ.id,
            type=DependencyType.FS,
        )
        st = await _states(s, org_a)
        await _complete(s, org=org_a, user=user_a, task_id=pred.id, version=pred.version, st=st)
        succ_id = succ.id
        # A has a handoff for the edge.
        assert len(await coord.list_handoffs(s, org_id=org_a, task_id=succ_id)) == 1

    async with tenant_session(str(org_b), str(user_b)) as s:
        # B cannot see A's handoffs (RLS) -- the foreign task simply
        # yields none.
        assert await coord.list_handoffs(s, org_id=org_b, task_id=succ_id) == []
        # B cannot offer/claim/decline A's task (not found in B).
        with pytest.raises(DomainError) as e1:
            await coord.offer_task(s, org_id=org_b, actor_id=user_b, task_id=succ_id)
        assert e1.value.code.value == "task.not_found"
        with pytest.raises(DomainError) as e2:
            await coord.claim_task(s, org_id=org_b, actor_id=user_b, task_id=succ_id)
        assert e2.value.code.value == "task.not_found"


# --- (f) no-dependents regression + non-fatal delivery ----------------


async def test_set_state_no_dependents_no_regression(_fake_embedder: None) -> None:
    """A task with NO dependents reaches a terminal state with no
    handoff and no error (the hook is additive: zero dependents ->
    zero work, the transition is unaffected)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="NR")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="Lonely", estimate_effort_h=Decimal(1)
        )
        v0 = t.version  # capture BEFORE the hook refreshes the row
        st = await _states(s, org)
        v = await _complete(s, org=org, user=user, task_id=t.id, version=v0, st=st)
        assert v == v0 + 2  # in_progress then done, both version bumps
        got = await tasks_svc.get_task(s, org_id=org, task_id=t.id)
        assert got.state_id == st["done"]
        assert (await s.execute(select(func.count()).select_from(TaskHandoff))).scalar_one() == 0


async def test_handoff_delivery_failure_never_blocks_transition(
    _fake_embedder: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coordination failure inside the on-completion hook must NOT
    roll back the workflow state transition (the transition is the
    source of truth). Force ``_deliver_one`` to raise; the predecessor
    still reaches the terminal state and the call returns normally."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="NF")
        b = await signup(s, email=_email(), password="pw-strong-123", org_name="NF2")
    org, user, succ_user = a.org_id, a.user_id, b.user_id

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("delivery exploded")

    monkeypatch.setattr(coord, "_deliver_one", _boom)

    async with tenant_session(str(org), str(user)) as s:
        s.add(Membership(org_id=org, user_id=succ_user, role=Role.member))
        await s.flush()
        pred = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="P", estimate_effort_h=Decimal(1)
        )
        succ = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="S",
            estimate_effort_h=Decimal(1),
            assignee_ids=[succ_user],
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=pred.id,
            successor_id=succ.id,
            type=DependencyType.FS,
        )
        v0 = pred.version  # capture BEFORE the hook refreshes the row
        st = await _states(s, org)
        # Must NOT raise despite _deliver_one blowing up.
        v = await _complete(s, org=org, user=user, task_id=pred.id, version=v0, st=st)
        got = await tasks_svc.get_task(s, org_id=org, task_id=pred.id)
        assert got.state_id == st["done"]  # transition stands
        assert v == v0 + 2
        # The failure left no active handoff (the per-edge attempt was
        # the thing that failed) -- and certainly no crash.
        assert (
            await s.execute(
                select(func.count())
                .select_from(TaskHandoff)
                .where(TaskHandoff.successor_task_id == succ.id)
            )
        ).scalar_one() == 0


# --- service-level RBAC parity (offer owner-gated) --------------------


async def test_offer_owner_gated_service_level(_fake_embedder: None) -> None:
    """The contract-net ``offer`` is owner-gated at the service choke
    point: a plain member cannot offer (ForbiddenError); claim/decline
    are member-level."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="OG")
    owner_org, owner_user = a.org_id, a.user_id

    async with tenant_session(str(owner_org), str(owner_user)) as s:
        t = await tasks_svc.create_task(
            s, org_id=owner_org, actor_id=owner_user, title="T", estimate_effort_h=Decimal(1)
        )
        other = await signup(s, email=_email(), password="pw-strong-123", org_name="OG2")
        s.add(Membership(org_id=owner_org, user_id=other.user_id, role=Role.member))
        await s.flush()
        tid = t.id

    async with tenant_session(str(owner_org), str(other.user_id)) as s:
        with pytest.raises(ForbiddenError):
            await coord.offer_task(s, org_id=owner_org, actor_id=other.user_id, task_id=tid)
        # claim of a non-offered task is a DomainError (not Forbidden):
        # a member MAY attempt to claim (member-gated), it is just not
        # offered.
        with pytest.raises(DomainError) as ei:
            await coord.claim_task(s, org_id=owner_org, actor_id=other.user_id, task_id=tid)
        assert ei.value.code.value == "task.not_offered"
        assert not isinstance(ei.value, ForbiddenError)


async def test_handoff_not_found_message_code_registered() -> None:
    """The new HANDOFF_NOT_FOUND / TASK_NOT_OFFERED /
    TASK_ALREADY_CLAIMED codes render English (catalog completeness;
    docs/adr/0017 -- no hardcoded user strings)."""
    from mycelium_core.i18n import MessageCode, render

    assert render(MessageCode.HANDOFF_NOT_FOUND) == "Handoff not found"
    assert "offered" in render(MessageCode.TASK_NOT_OFFERED)
    assert "claimed" in render(MessageCode.TASK_ALREADY_CLAIMED)


# --- offering to principals who cannot take the task ------------------


async def test_offer_skips_deactivated_members_and_refuses_when_nobody_is_left(
    _fake_embedder: None,
) -> None:
    """A ``task_offer`` notification is the ONLY discovery channel for an
    offer: there is no bid table and no offered-tasks queue, so an offer
    that reaches nobody sets ``offered=True``, tells no one, and strands
    the task -- precisely what the "never strand a task" fallback in
    ``_eligible_member_users`` exists to prevent. So a deactivated member
    is skipped, and an offer with no one left to receive it is refused to
    a caller who can act on the refusal.
    """
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="OF")
        live = await signup(s, email=_email(), password="pw-strong-123", org_name="L")
        dead = await signup(s, email=_email(), password="pw-strong-123", org_name="D")
    org, owner = a.org_id, a.user_id

    async with tenant_session(str(org), str(owner)) as s:
        s.add(Membership(org_id=org, user_id=live.user_id, role=Role.member))
        s.add(Membership(org_id=org, user_id=dead.user_id, role=Role.member))
        t = await tasks_svc.create_task(
            s, org_id=org, actor_id=owner, title="T", estimate_effort_h=Decimal(1)
        )
        tid = t.id

    async with admin_session() as s:
        await s.execute(update(User).where(User.id == dead.user_id).values(is_active=False))

    async with tenant_session(str(org), str(owner)) as s:
        await coord.offer_task(s, org_id=org, actor_id=owner, task_id=tid)
        offered_to = {
            n.user_id
            for n in (
                await s.execute(select(Notification).where(Notification.kind == "task_offer"))
            )
            .scalars()
            .all()
        }
        assert live.user_id in offered_to
        assert dead.user_id not in offered_to, "a deactivated member cannot claim it"

    # Now deactivate everyone: the offer has nowhere to go and must refuse
    # rather than silently mark the task offered.
    async with admin_session() as s:
        await s.execute(
            update(User).where(User.id.in_([owner, live.user_id])).values(is_active=False)
        )
    async with tenant_session(str(org), str(owner)) as s:
        t2 = await tasks_svc.create_task(
            s, org_id=org, actor_id=owner, title="T2", estimate_effort_h=Decimal(1)
        )
        t2_id = t2.id
    async with tenant_session(str(org), str(owner)) as s:
        with pytest.raises(DomainError) as exc:
            await coord.offer_task(s, org_id=org, actor_id=owner, task_id=t2_id)
        assert exc.value.code is MessageCode.TASK_OFFER_NO_RECIPIENTS
    async with tenant_session(str(org), str(owner)) as s:
        untouched = await tasks_svc.get_task(s, org_id=org, task_id=t2_id)
        assert untouched.offered is False, "a refused offer must not mark the task"


# --- C1: the award is a compare-and-swap ------------------------------


async def _member_of(org: uuid.UUID, owner: uuid.UUID) -> uuid.UUID:
    """A second real user holding ``member`` in the owner's workspace."""
    async with admin_session() as s:
        m = await signup(s, email=_email(), password="pw-strong-123", org_name="RACE-HOME")
    async with tenant_session(str(org), str(owner)) as s:
        s.add(Membership(org_id=org, user_id=m.user_id, role=Role.member))
        await s.flush()
    return m.user_id


async def test_two_members_racing_to_claim_leave_one_winner_and_one_version_bump() -> None:
    """The defect this fix exists for, exercised as a real race.

    Both claimers pass all three pre-reads -- under READ COMMITTED
    neither sees the other's uncommitted collaborator row or cleared
    ``offered`` flag -- and the old code then had each write
    ``offered = false`` and ``version += 1`` through a predicate-less
    ORM flush. The row lock serialized those writes without detecting
    the conflict: two winners on a contract-net award, and a version
    counter that advanced ONCE for two logical updates, leaving every
    later ``expected_version`` gate on that row off by one.

    Two overlapping transactions, not two sequential calls: the winner
    holds its row lock while the loser attempts the same claim, so the
    loser's UPDATE blocks and re-evaluates its predicate against the
    committed result. A same-transaction double call would exercise the
    compare-and-swap but not the race, and would not have failed against
    the old code either.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "RACE"},
            )
        ).json()
    org = uuid.UUID(a["workspace_id"])
    owner = uuid.UUID(decode_token(a["token"])["sub"])
    other = await _member_of(org, owner)

    async with tenant_session(str(org), str(owner)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=owner, title="Up for grabs")
        tid = task.id
        await coord.offer_task(s, org_id=org, actor_id=owner, task_id=tid)
    async with tenant_session(str(org), str(owner)) as s:
        before = (await tasks_svc.get_task(s, org_id=org, task_id=tid)).version

    # The first claimer holds its lock long enough for the second to
    # reach its own UPDATE and block there.
    hold, stagger = 0.6, 0.15

    async def _claim(user_id: uuid.UUID, start_delay: float, hold_for: float) -> str:
        await asyncio.sleep(start_delay)
        try:
            async with tenant_session(str(org), str(user_id)) as s:
                await coord.claim_task(s, org_id=org, actor_id=user_id, task_id=tid)
                await asyncio.sleep(hold_for)
            return "won"
        except DomainError as exc:
            return exc.code.value

    outcomes = await asyncio.gather(
        _claim(owner, 0.0, hold),
        _claim(other, stagger, 0.0),
    )

    assert sorted(outcomes) == ["task.already_claimed", "won"], outcomes

    async with tenant_session(str(org), str(owner)) as s:
        winners = (
            (
                await s.execute(
                    select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == tid)
                )
            )
            .scalars()
            .all()
        )
        assert len(winners) == 1, winners
        row = await tasks_svc.get_task(s, org_id=org, task_id=tid)
        assert row.offered is False
        # One award, one increment. Two would mean the loser wrote too;
        # zero would mean nobody did.
        assert row.version == before + 1


async def test_a_won_claim_returns_the_version_the_database_holds() -> None:
    """Regression on the ORM reconciliation, which is the easy half to
    get wrong.

    The award is now a Core UPDATE that bypasses the mapper, so the
    loaded instance still carries the pre-claim ``offered`` and
    ``version`` while both adapters serialize their response off exactly
    those two attributes. Re-assigning them in Python instead of
    refreshing would look right and be a second increment.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "RECON"},
            )
        ).json()
        owner_h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        org = uuid.UUID(a["workspace_id"])
        owner = uuid.UUID(decode_token(a["token"])["sub"])

        tid = (await c.post("/tasks", headers=owner_h, json={"title": "Claim me"})).json()["id"]
        offered = (await c.post(f"/tasks/{tid}/offer", headers=owner_h)).json()
        assert offered["offered"] is True

        claimed = (await c.post(f"/tasks/{tid}/claim", headers=owner_h)).json()
        assert claimed["offered"] is False
        assert claimed["version"] == offered["version"] + 1

    async with tenant_session(str(org), str(owner)) as s:
        row = await tasks_svc.get_task(s, org_id=org, task_id=uuid.UUID(tid))
        assert row.version == claimed["version"]
        assert row.offered is False
