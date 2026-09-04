"""Closed-loop dispatch + approval gates (docs/adr/0025, P5).

The closed cycle that ties P1-P4 together:

    tick -> recompute (P1/P2 scheduler) -> read the admitted llm_agent
    set -> (governance gate) -> start runs via P3 -> [agent runs
    execute out-of-band; the P4 set_state hook fires handoffs on
    completion] -> next tick recompute picks up the new ready set.

This module is a PURE deterministic orchestration service. It does NOT
re-derive scheduling, admission, execution or coordination -- it calls:

- ``scheduler.recompute`` (P1/P2): the ONLY source of the feasible plan
  and the admitted/dispatchable assignment. The admitted llm_agent set
  is exactly the ``Schedule`` rows that are ``llm_agent``, NOT
  ``unassignable``, and carry an ``assigned_executor_id`` (the same
  notion ``agent_runtime._assigned_executor`` /
  ``coordination._resolved_executor`` use). Per-agent WIP
  (``max_parallel``) and Σ credit budget were already enforced by the
  scheduler's admission; the loop re-checks live in-flight runs as a
  defense-in-depth before each start.
- ``agent_runtime.start_run`` (P3): the ONLY execution path -- bounded,
  metered (idempotent ``agentrun:{run.id}:{step}``), killable, confined
  to the actor's effective RBAC. The loop never bypasses it.
- the P4 ``tasks.set_state`` -> ``coordination.on_task_completed`` hook
  fires the handoff fan-out on run completion; the loop only RESCHEDULES
  (recompute again) and picks the next batch.

Governance (ADR-0025 §Governance, non-negotiable): the default is
human-in-the-loop. ``tick`` NEVER spends credits / starts a run without
either an explicit per-dispatch ``approve`` (the API/MCP) OR an explicit
workspace opt-in to ``auto`` mode. ``auto`` removes the human click; it
does NOT remove the per-agent WIP cap, the credit budget, the tool
allowlist or the RBAC ceiling. Every privileged op (approve / deny /
policy change / manual tick) is owner-gated through the effective-role
choke point (``rbac.require_role(... owner)``), exactly like the P2
executor CRUD and the P3 run start (running an agent spends credits).

Determinism: the admitted set is processed in the SAME order the
scheduler uses -- scheduler priority is reflected by
``Schedule.scheduled_start`` (the leveled placement), with
``str(task_id)`` as the final stable tie-break (the deterministic-core
contract). Idempotent: a second ``tick`` with no state change creates
no duplicate ``dispatch_request`` and starts no duplicate run (the
at-most-one-active-request-per-task invariant + the P3
at-most-one-active-run guard). NON-FATAL per task: a failure starting
one task's run sets THAT request ``failed`` with a short ``reason`` and
the tick continues with the others -- one bad task never aborts the
tick.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_run import AgentRun, AgentRunStatus
from mycelium_core.models.ai_assistant import AiAssistant, AssistantRuntime
from mycelium_core.models.dispatch_request import (
    ACTIVE_DISPATCH_STATUSES,
    DEFAULT_AUTONOMOUS_DISPATCH,
    AutonomousDispatch,
    DispatchRequest,
    DispatchStatus,
)
from mycelium_core.models.executor import Executor
from mycelium_core.models.identity import Identity, IdentityKind
from mycelium_core.models.membership import Role
from mycelium_core.models.organization import Organization
from mycelium_core.models.schedule import Schedule
from mycelium_core.models.task import ExecKind, SchedulePolicy, Task
from mycelium_core.services import agent_runtime as agent_runtime_svc
from mycelium_core.services import audit
from mycelium_core.services import identities as identities_svc
from mycelium_core.services import scheduler as scheduler_svc
from mycelium_core.services.rbac import require_role

# The per-tick dispatch cap (docs/adr/0025 §Admission control,
# Little's-law-informed "the right number to delegate"). The scheduler
# already bounds the plan by per-agent ``max_parallel`` WIP and Σ
# credit budget, so the feasible set is capacity-bounded; this constant
# is a deterministic throttle on CHURN -- how many runs one cycle kicks
# off -- so a burst of newly-admitted tasks does not all start at once
# and the loop stays observable/cancellable between ticks. A small
# constant (not config) keeps the contract deterministic; the real
# capacity caps are the executor budget/WIP, not this number.
MAX_DISPATCHES_PER_TICK = 8

# The settings key for the per-workspace autonomous policy
# (organizations.settings JSON bag, same mechanism as
# ``estimate_presets``; default resolves to ``approval_required``).
SETTINGS_KEY = "autonomous_dispatch"


@dataclass(frozen=True)
class DispatchTickResult:
    """What one ``tick`` produced (the UI's "last tick" panel). Counts
    are of dispatch_requests touched this tick; the projections come
    straight from the scheduler summary so the loop and the schedule
    view agree."""

    policy: AutonomousDispatch
    enabled: bool
    created: int
    approved: int
    dispatched: int
    skipped: int
    failed: int
    # Straight from the scheduler RecomputeSummary (the same numbers the
    # schedule view shows): comparable makespan + projected credit cost.
    projected_makespan_minutes: int
    projected_credit_cost: Decimal


def resolve_policy(org: Organization) -> AutonomousDispatch:
    """The workspace's autonomous-dispatch policy from the settings bag.

    Governance default (ADR-0025 §Governance): an unset / unknown /
    malformed value resolves to ``approval_required`` -- never ``auto``.
    No silent auto-spend without an explicit, valid opt-in.
    """
    raw = (org.settings or {}).get(SETTINGS_KEY)
    if not isinstance(raw, str):
        return DEFAULT_AUTONOMOUS_DISPATCH
    try:
        return AutonomousDispatch(raw)
    except ValueError:
        return DEFAULT_AUTONOMOUS_DISPATCH


def normalize_policy(value: Any) -> AutonomousDispatch:
    """Validate an incoming policy value (settings PUT). Raises
    ``AUTONOMOUS_POLICY_INVALID`` with the offending value as detail --
    no hardcoded prose (docs/adr/0017)."""
    if isinstance(value, AutonomousDispatch):
        return value
    try:
        return AutonomousDispatch(str(value))
    except ValueError as exc:
        raise DomainError(MessageCode.AUTONOMOUS_POLICY_INVALID, detail=str(value)) from exc


async def _org(session: AsyncSession, *, org_id: uuid.UUID) -> Organization:
    org = (
        await session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise NotFoundError(MessageCode.ORG_NOT_FOUND)
    return org


async def _active_run_task_ids(session: AsyncSession) -> set[uuid.UUID]:
    """Tasks with an in-flight (queued|running) agent run -- they must
    NOT get a new dispatch_request / a second run (P3 also guards, this
    is the loop-side dedupe)."""
    rows = (
        (
            await session.execute(
                select(AgentRun.task_id).where(
                    AgentRun.status.in_([AgentRunStatus.queued, AgentRunStatus.running])
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def _tasks_with_any_run(session: AsyncSession) -> set[uuid.UUID]:
    """Tasks whose agent run COUNTS as having run. The loop proposes a
    dispatch AT MOST ONCE per task: re-executing an agent that already
    produced its artifact is an explicit human/owner action (a fresh
    approval), NEVER an automatic per-tick credit burn -- that would
    violate the governance no-silent-auto-spend guarantee and spin the
    loop forever on every completed task. The recompute still reflects
    the completion/variance (the "reschedule" half of the loop).

    The three terms say that in SQL instead of leaving it to be inferred
    from the order of two statements in ``_drive``:

    - not ``failed``: succeeded, blocked, cancelled, queued and running
      all count. ``blocked`` especially -- a blocked run at zero steps
      is ``budget_exhausted`` or ``tool_not_allowed``, a governance
      decision taken, not an attempt that missed. A predicate written on
      ``steps == 0`` alone would reopen it and re-propose the task on
      every tick against an exhausted budget.
    - ``steps > 0`` or ``credits_spent > 0``: a failed run that got far
      enough to do something still counts, so nothing that spent credit
      can come back on its own.

    What drops out is exactly the run that reached no provider: failed,
    zero steps, zero credit. That row used to remove its task from the
    loop permanently, and nothing in this codebase deletes ``AgentRun``
    rows, so there was no way back -- while the module docstring's stated
    reason for the rule, "already produced its artifact", was never true
    of it.
    """
    rows = (
        (
            await session.execute(
                select(AgentRun.task_id).where(
                    or_(
                        AgentRun.status != AgentRunStatus.failed,
                        AgentRun.steps > 0,
                        AgentRun.credits_spent > 0,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def _in_flight_by_executor(session: AsyncSession) -> dict[uuid.UUID, int]:
    """Live in-flight (queued|running) run count per executor -- the
    defense-in-depth WIP re-check before starting a run (the scheduler
    already admitted within ``max_parallel`` on its snapshot; tasks
    completing/starting between recompute and dispatch are caught
    here)."""
    rows = (
        await session.execute(
            select(AgentRun.executor_id).where(
                AgentRun.status.in_([AgentRunStatus.queued, AgentRunStatus.running]),
                AgentRun.executor_id.is_not(None),
            )
        )
    ).all()
    counts: dict[uuid.UUID, int] = {}
    for (eid,) in rows:
        if eid is not None:
            counts[eid] = counts.get(eid, 0) + 1
    return counts


async def _active_request_for_task(
    session: AsyncSession, *, task_id: uuid.UUID
) -> DispatchRequest | None:
    """The single ACTIVE (pending|approved) request for a task, if any
    (the at-most-one-active invariant; deterministic newest-first then
    str(id))."""
    return (
        await session.execute(
            select(DispatchRequest)
            .where(
                DispatchRequest.task_id == task_id,
                DispatchRequest.status.in_(ACTIVE_DISPATCH_STATUSES),
            )
            .order_by(DispatchRequest.created_at, DispatchRequest.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _admitted_agent_rows(
    session: AsyncSession,
) -> list[tuple[Schedule, Task]]:
    """The scheduler's admitted llm_agent dispatch set: every Schedule
    row that is an ``llm_agent`` task, NOT ``unassignable`` and has an
    ``assigned_executor_id`` -- exactly P3's ``_assigned_executor`` /
    P4's ``_resolved_executor`` notion (the loop does NOT re-derive
    scheduling). Deterministic order: the leveled placement
    (``scheduled_start``, NULLs last) then ``str(task_id)`` final
    tie-break (the scheduler's own deterministic-core contract)."""
    # docs/adr/0028: kind comes from the assignee identity when set,
    # else from the task's ``executor_kind`` hint. The dispatcher
    # picks tasks routed to the llm_agent pool either way.
    rows = (
        await session.execute(
            select(Schedule, Task, Identity.kind, AiAssistant.runtime)
            .join(Task, Task.id == Schedule.task_id)
            .outerjoin(Identity, Identity.id == Task.assignee_id)
            .outerjoin(AiAssistant, AiAssistant.id == Identity.ai_assistant_id)
            .where(
                Schedule.unassignable.is_(False),
                Schedule.assigned_executor_id.is_not(None),
                Task.deleted_at.is_(None),
                Task.is_archived.is_(False),
            )
        )
    ).all()

    def _is_agent(task: Task, ikind: IdentityKind | None, runtime: AssistantRuntime | None) -> bool:
        if ikind is not None:
            return identities_svc.is_internal_agent(ikind, runtime)
        # No assignee: the persisted routing hint decides (docs/adr/0028).
        # There is no assistant row to ask, so ``runtime`` says nothing
        # here and this branch is unchanged.
        return task.executor_kind is ExecKind.llm_agent

    agent_rows = [
        (sch, task) for sch, task, ikind, runtime in rows if _is_agent(task, ikind, runtime)
    ]
    _far = dt.datetime.max.replace(tzinfo=dt.UTC)
    agent_rows.sort(key=lambda st: (st[0].scheduled_start or _far, str(st[1].id)))
    return agent_rows


async def _dispatch_one(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    req: DispatchRequest,
    in_flight: dict[uuid.UUID, int],
) -> bool:
    """Start the P3 run for ONE approved request and move it to
    ``dispatched`` (recording the ``agent_run_id``). NON-FATAL: any
    failure (live WIP/budget re-check, provider error, blocked, already
    active) marks the request ``failed`` with a short stable ``reason``
    and returns ``False`` -- it never raises to the caller, so one bad
    task cannot abort the tick. Returns ``True`` iff a run was started.

    ``in_flight`` is the per-executor live run count; on a successful
    start it is incremented so subsequent dispatches in the SAME tick
    respect the agent's ``max_parallel`` without an extra round-trip.
    """
    executor: Executor | None = None
    if req.executor_id is not None:
        executor = (
            await session.execute(select(Executor).where(Executor.id == req.executor_id))
        ).scalar_one_or_none()

    # Live WIP re-check (defense-in-depth beyond the scheduler snapshot):
    # never exceed the agent's max_parallel with in-flight runs.
    if executor is not None:
        cap = max(1, executor.max_parallel)
        if in_flight.get(executor.id, 0) >= cap:
            req.status = DispatchStatus.failed
            req.reason = "wip_exhausted"
            req.decided_at = dt.datetime.now(tz=dt.UTC)
            req.version += 1
            await session.flush()
            await _audit(session, org_id=org_id, actor_id=actor_id, req=req, action="failed")
            return False

    try:
        run = await agent_runtime_svc.start_run(
            session,
            org_id=org_id,
            actor_id=actor_id,
            task_id=req.task_id,
        )
    except (DomainError, NotFoundError) as exc:
        # Budget exhausted at dispatch time, task no longer
        # dispatchable, a concurrent active run, ... A guarded,
        # inspectable stop -- NOT a crash, NOT fatal to the tick.
        req.status = DispatchStatus.failed
        req.reason = _reason_code(exc)
        req.decided_at = dt.datetime.now(tz=dt.UTC)
        req.version += 1
        await session.flush()
        await _audit(session, org_id=org_id, actor_id=actor_id, req=req, action="failed")
        return False
    except Exception as exc:  # provider/runtime boundary: still non-fatal
        req.status = DispatchStatus.failed
        req.reason = "run_error"
        req.decided_at = dt.datetime.now(tz=dt.UTC)
        req.version += 1
        await session.flush()
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="dispatch_request",
            entity_id=req.id,
            action="failed",
            diff={"error": str(exc)[:200]},
        )
        return False

    req.status = DispatchStatus.dispatched
    req.agent_run_id = run.id
    req.decided_at = dt.datetime.now(tz=dt.UTC)
    if req.decided_by is None:
        req.decided_by = actor_id
    req.version += 1
    await session.flush()
    if executor is not None:
        in_flight[executor.id] = in_flight.get(executor.id, 0) + 1
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        req=req,
        action="dispatched",
        extra={"agent_run": str(run.id), "run_status": run.status.value},
    )
    return True


def _reason_code(exc: DomainError | NotFoundError) -> str:
    """Map a domain/notfound error to a short stable reason slug (never
    free prose; docs/adr/0017). The MessageCode value is already a
    stable machine string."""
    return str(getattr(exc, "code", MessageCode.DOMAIN_ERROR).value)[:200]


async def _audit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    req: DispatchRequest,
    action: str,
    extra: dict[str, str] | None = None,
) -> None:
    diff = {"task": str(req.task_id), "status": req.status.value}
    if req.reason:
        diff["reason"] = req.reason
    if extra:
        diff.update(extra)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="dispatch_request",
        entity_id=req.id,
        action=action,
        diff=diff,
    )


async def tick(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    as_of: dt.datetime | None = None,
    policy: SchedulePolicy = SchedulePolicy.balanced,
    max_dispatches: int = MAX_DISPATCHES_PER_TICK,
) -> DispatchTickResult:
    """Run ONE closed-loop cycle for a workspace.

    Owner-gated (a tick can spend credits via P3): the effective-role
    choke point, exactly like ``agent_runtime.start_run`` and the P2
    executor CRUD. The worker passes the workspace owner as ``actor_id``
    (the fallback-to-stored-membership path in ``rbac.require_role``);
    the API passes the calling owner with the sudo-clamped GUC role.

    Steps:
      1. Resolve the workspace policy. ``off`` -> no-op result.
      2. Recompute the P1/P2 schedule (this also reflects completed /
         failed tasks and the P4 handoff fan-out from the previous
         cycle -- the "reschedule on completion/variance" half of the
         loop).
      3. For each scheduler-admitted llm_agent task with NO active run
         and NO active request, create one ``pending`` request
         (projected cost = the row's ``Schedule.projected_cost``). Under
         ``auto`` the request is created already ``approved``.
      4. Retire (``skipped``) any still-active request whose task is no
         longer admitted (deps changed / budget exhausted / executor
         removed) -- a fresh request is created if it becomes
         admissible again.
      5. Dispatch every ``approved`` request via P3 ``start_run``
         (bounded by ``max_dispatches`` and the live per-agent WIP).
         NON-FATAL per task; deterministic order.
    """
    await require_role(session, org_id, actor_id, Role.owner)
    org = await _org(session, org_id=org_id)
    pol = resolve_policy(org)
    if pol is AutonomousDispatch.off:
        return DispatchTickResult(
            policy=pol,
            enabled=False,
            created=0,
            approved=0,
            dispatched=0,
            skipped=0,
            failed=0,
            projected_makespan_minutes=0,
            projected_credit_cost=Decimal(0),
        )

    # (2) Reschedule first: the recompute reflects the previous cycle's
    # completed/failed tasks and the P4 handoff-driven new ready set.
    summary = await scheduler_svc.recompute(
        session,
        org_id=org_id,
        actor_id=actor_id,
        as_of=as_of,
        policy=policy,
    )

    admitted = await _admitted_agent_rows(session)
    admitted_task_ids = {task.id for _sch, task in admitted}
    active_run_tasks = await _active_run_task_ids(session)
    ran_tasks = await _tasks_with_any_run(session)

    created = 0
    approved = 0
    auto = pol is AutonomousDispatch.auto
    now = dt.datetime.now(tz=dt.UTC)

    # (3) Create one request per newly-admitted agent task with NO agent
    # run yet and no active request. Deterministic order (already sorted
    # by the leveled placement then str(id)).
    for sch, task in admitted:
        existing = await _active_request_for_task(session, task_id=task.id)
        if existing is not None:
            # At-most-one-active-request invariant: reuse, never
            # duplicate. Keep the assignment/cost fresh from the latest
            # recompute, and under ``auto`` promote a still-pending row.
            # (A task with an active request has not run yet -- a run
            # terminalizes its request to ``dispatched`` -- so this
            # branch never collides with the dispatch-once rule.)
            existing.executor_id = sch.assigned_executor_id
            existing.projected_credit_cost = sch.projected_cost
            if auto and existing.status is DispatchStatus.pending:
                existing.status = DispatchStatus.approved
                existing.decided_at = now
                existing.decided_by = actor_id
                approved += 1
            existing.version += 1
            await session.flush()
            continue
        if task.id in ran_tasks:
            # Dispatch-once: this task already had an agent run (in
            # flight or finished). The recompute above already reflected
            # its completion/variance; the loop does NOT auto-re-execute
            # a finished agent (governance: no automatic repeated credit
            # spend; re-running is an explicit owner action).
            continue
        req = DispatchRequest(
            org_id=org_id,
            task_id=task.id,
            executor_id=sch.assigned_executor_id,
            status=(DispatchStatus.approved if auto else DispatchStatus.pending),
            projected_credit_cost=sch.projected_cost,
            requested_at=now,
            decided_at=now if auto else None,
            decided_by=actor_id if auto else None,
        )
        session.add(req)
        await session.flush()
        created += 1
        if auto:
            approved += 1
        await _audit(
            session,
            org_id=org_id,
            actor_id=actor_id,
            req=req,
            action="auto_approved" if auto else "created",
        )

    # (4) Retire still-active requests whose task is no longer admitted
    # (a visible "the plan changed" terminal, not a silent drop). A
    # fresh request is created on a later tick if it is re-admitted.
    skipped = 0
    stale = (
        (
            await session.execute(
                select(DispatchRequest).where(DispatchRequest.status.in_(ACTIVE_DISPATCH_STATUSES))
            )
        )
        .scalars()
        .all()
    )
    for req in sorted(stale, key=lambda r: (r.created_at, str(r.id))):
        if req.task_id in admitted_task_ids or req.task_id in active_run_tasks:
            continue
        req.status = DispatchStatus.skipped
        req.reason = "not_admitted"
        req.decided_at = now
        req.version += 1
        await session.flush()
        skipped += 1
        await _audit(session, org_id=org_id, actor_id=actor_id, req=req, action="skipped")

    # (5) Dispatch approved requests via the P3 metered path. Bounded by
    # max_dispatches and the live per-agent WIP; NON-FATAL per task;
    # deterministic order (leveled placement then str(id), matching the
    # admitted ordering).
    in_flight = await _in_flight_by_executor(session)
    order_index = {task.id: i for i, (_sch, task) in enumerate(admitted)}
    approved_rows = list(
        (
            await session.execute(
                select(DispatchRequest).where(DispatchRequest.status == DispatchStatus.approved)
            )
        )
        .scalars()
        .all()
    )
    approved_rows.sort(key=lambda r: (order_index.get(r.task_id, len(order_index)), str(r.task_id)))
    dispatched = 0
    failed = 0
    for req in approved_rows:
        if dispatched >= max_dispatches:
            # Per-tick churn cap reached: the rest stay ``approved`` and
            # are dispatched on the next tick (the plan is unchanged;
            # this is a throttle, not a drop).
            break
        if req.task_id in active_run_tasks:
            # A run started for this task already (e.g. an inline
            # approve dispatch earlier this cycle): leave it, no
            # duplicate run.
            continue
        ok = await _dispatch_one(
            session,
            org_id=org_id,
            actor_id=actor_id,
            req=req,
            in_flight=in_flight,
        )
        if ok:
            dispatched += 1
            active_run_tasks.add(req.task_id)
        else:
            failed += 1

    return DispatchTickResult(
        policy=pol,
        enabled=True,
        created=created,
        approved=approved,
        dispatched=dispatched,
        skipped=skipped,
        failed=failed,
        projected_makespan_minutes=summary.makespan_minutes,
        projected_credit_cost=summary.projected_credit_cost,
    )


async def list_requests(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    status: DispatchStatus | None = None,
) -> list[DispatchRequest]:
    """The dispatch queue (member-level read, RLS-scoped), newest first
    then ``str(id)`` (stable). Optionally filtered to one status."""
    stmt = select(DispatchRequest)
    if status is not None:
        stmt = stmt.where(DispatchRequest.status == status)
    stmt = stmt.order_by(DispatchRequest.created_at.desc(), DispatchRequest.id)
    return list((await session.execute(stmt)).scalars().all())


async def get_request(
    session: AsyncSession, *, org_id: uuid.UUID, request_id: uuid.UUID
) -> DispatchRequest:
    req = (
        await session.execute(select(DispatchRequest).where(DispatchRequest.id == request_id))
    ).scalar_one_or_none()
    if req is None:
        raise NotFoundError(MessageCode.DISPATCH_NOT_FOUND)
    return req


async def approve_request(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    request_id: uuid.UUID,
    expected_version: int,
) -> DispatchRequest:
    """Owner: approve a ``pending`` request, then IMMEDIATELY attempt
    the dispatch inline (the design choice -- see the module docstring:
    approve-then-inline-dispatch so the caller/test can assert the run
    started in the same request; the worker tick dispatches any
    ``approved`` row left over identically). Owner-gated through the
    effective-role choke point (a tick spends credits, like
    ``start_run``). Optimistic concurrency on ``expected_version``.

    NON-FATAL: if the inline dispatch fails the request is left
    ``failed`` with a stable ``reason`` (never raises for a run/budget
    failure -- same contract as the loop). ``DISPATCH_NOT_PENDING`` if
    it is not ``pending`` (already approved/decided)."""
    await require_role(session, org_id, actor_id, Role.owner)
    req = await get_request(session, org_id=org_id, request_id=request_id)
    if req.status is not DispatchStatus.pending:
        raise DomainError(MessageCode.DISPATCH_NOT_PENDING)
    if req.version != expected_version:
        raise DomainError(MessageCode.CONFLICT_STALE_VERSION)
    req.status = DispatchStatus.approved
    req.decided_at = dt.datetime.now(tz=dt.UTC)
    req.decided_by = actor_id
    req.version += 1
    await session.flush()
    await _audit(session, org_id=org_id, actor_id=actor_id, req=req, action="approved")
    # Inline dispatch (the documented choice). Idempotent: if a run is
    # somehow already active for the task, _dispatch_one -> start_run
    # raises AGENT_RUN_ALREADY_ACTIVE which is captured as a non-fatal
    # ``failed`` (no duplicate run).
    in_flight = await _in_flight_by_executor(session)
    await _dispatch_one(
        session,
        org_id=org_id,
        actor_id=actor_id,
        req=req,
        in_flight=in_flight,
    )
    return req


async def deny_request(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    request_id: uuid.UUID,
    expected_version: int,
    reason: str | None = None,
) -> DispatchRequest:
    """Owner: deny an ACTIVE (pending|approved) request -> ``denied``;
    NEVER starts a run. Owner-gated (effective-role choke point),
    optimistic concurrency. ``DISPATCH_ALREADY_DECIDED`` if it is
    already terminal (dispatched/denied/skipped/failed)."""
    await require_role(session, org_id, actor_id, Role.owner)
    req = await get_request(session, org_id=org_id, request_id=request_id)
    if req.status not in ACTIVE_DISPATCH_STATUSES:
        raise DomainError(MessageCode.DISPATCH_ALREADY_DECIDED)
    if req.version != expected_version:
        raise DomainError(MessageCode.CONFLICT_STALE_VERSION)
    req.status = DispatchStatus.denied
    req.reason = (reason or "").strip()[:200] or None
    req.decided_at = dt.datetime.now(tz=dt.UTC)
    req.decided_by = actor_id
    req.version += 1
    await session.flush()
    await _audit(session, org_id=org_id, actor_id=actor_id, req=req, action="denied")
    return req
