"""Deterministic resource-aware scheduler (docs/adr/0004 + 0025, FR-4).

Stage 1 (unchanged): a logical CPM forward/backward pass over working
calendars -> ES/EF/LS/LF, logical slack, ``on_logical_critical_path``.
This assumes infinite resources and is the heuristic *input*, not the
final answer (ADR-0025).

Stage 2 (resource-constrained list scheduling, ADR-0025 P1):
- **Humans** are serial unit-capacity resources on their working
  calendar; consecutive distinct tasks for a person cost the human
  executor's ``context_switch_cost_minutes``. Fixed events are avoided
  (no-ubiquity). Order = the selected policy's priority rule.
- **LLM agents** are a K-parallel pool (per ``llm_agent`` executor's
  ``max_parallel``), 24/7 (no working calendar, no daily cap); a task
  starts at max(its precedence-driven earliest, the earliest freed pool
  slot). Cost is projected against the agent's ``credit_rate_per_hour``
  and flagged (not dropped) past ``credit_budget`` -- P1 projects only.

The objective is **multi-policy** (``SchedulePolicy``), selected per
recompute. Outputs additionally include the **resource-aware critical
chain** (zero float in the *leveled* plan, distinct from the logical
critical path), the projected makespan and the projected credit cost.

Manual/pinned and in-progress tasks survive recompute. Deterministic:
stable ordering and an id final tie-break everywhere -- a
non-deterministic schedule is a failure (it is the product's
deterministic core).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.dependency import DependencyType, TaskDependency
from mycelium_core.models.executor import Executor, ExecutorKind
from mycelium_core.models.identity import Identity
from mycelium_core.models.membership import Role
from mycelium_core.models.schedule import Schedule
from mycelium_core.models.task import ExecKind, ScheduleMode, SchedulePolicy, Task
from mycelium_core.models.task_collaborator import TaskCollaborator
from mycelium_core.models.task_participant import TaskParticipant
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import audit
from mycelium_core.services import executors as executors_svc
from mycelium_core.services import identities as identities_svc
from mycelium_core.services.calendar import WorkCalendar, build_work_calendar
from mycelium_core.services.rbac import require_role

# Fixed, stable set of ``Schedule.unassignable_reason`` strings (English,
# enum-ish; documented in docs/adr/0025 P2). Never free-form prose.
#  - no_capable_agent: no enabled llm_agent advertises every capability
#    the task requires (or there is no enabled llm_agent at all).
#  - budget_exhausted: every eligible enabled agent would exceed its
#    credit_budget if this task were placed on it within the horizon.
#  - assignee_inactive: the task is addressed to an ai_assistant that has
#    since been deactivated. Same class of event as deleting the agent
#    that could do the work, which already lands here: the scheduler
#    flags a visible dispatch gap, it never silently reroutes.
_UNASSIGNABLE_NO_CAPABLE_AGENT = "no_capable_agent"
_UNASSIGNABLE_BUDGET_EXHAUSTED = "budget_exhausted"
_UNASSIGNABLE_ASSIGNEE_INACTIVE = "assignee_inactive"


@dataclass
class _Node:
    task: Task
    assignee: uuid.UUID | None
    duration_min: int
    terminal: bool
    # docs/adr/0028: the routing kind (human vs llm_agent) is no
    # longer stored on the task; it comes from the joined
    # ``identities.kind``. We materialise it here so the rest of the
    # scheduler logic reads ``n.kind`` instead of
    # ``n.task.executor_kind``.
    kind: ExecKind = ExecKind.human
    es: dt.datetime = field(default=dt.datetime.min)
    ef: dt.datetime = field(default=dt.datetime.min)
    ls: dt.datetime = field(default=dt.datetime.min)
    lf: dt.datetime = field(default=dt.datetime.min)
    # Resource-leveled placement (Stage 2). ``ss``/``se`` are the final
    # scheduled start/end; ``llf`` is the leveled latest finish from the
    # backward pass over the leveled graph (precedence + per-resource
    # successor edges) used to derive ``on_critical_chain``.
    ss: dt.datetime = field(default=dt.datetime.min)
    se: dt.datetime = field(default=dt.datetime.min)
    llf: dt.datetime = field(default=dt.datetime.min)
    # Admission-control dispatch result (P2). ``assigned`` is the chosen
    # llm_agent (None for human/off-timeline/unassignable rows);
    # ``unassignable_reason`` is set (from the fixed set) iff this llm
    # task could not be dispatched.
    assigned: uuid.UUID | None = None
    unassignable_reason: str | None = None


@dataclass
class RecomputeSummary:
    """What a recompute produced: row count + the comparable projections
    (makespan, credit cost), the policy that produced them, and the
    count of llm tasks with no admissible executor (P2 dispatch gaps)."""

    count: int
    makespan_minutes: int
    projected_credit_cost: Decimal
    policy: SchedulePolicy
    unassignable_count: int = 0


def _logical_slack_min(n: _Node) -> int:
    return max(0, int((n.ls - n.es).total_seconds() // 60))


def _policy_key(
    policy: SchedulePolicy,
    rate_of: Callable[[_Node], Decimal],
) -> Callable[[_Node], tuple[object, ...]]:
    """The deterministic priority/break rule for the selected policy.

    Every key ends with ``str(task.id)`` so ordering is total and stable
    regardless of equal leading fields (the deterministic-core
    contract). ``priority`` is P1..P4 with P1 (=1) highest, so ascending
    priority = most important first.

    - ``balanced`` (default, the pre-P1 ADR-0004 rule): most important
      first -> (priority, due, created, id).
    - ``fastest`` (compress makespan): least logical slack first, then
      logical-critical first, then the balanced rule -> tasks that bind
      the makespan are placed earliest.
    - ``cheapest`` (defer LLM credit spend): zero-credit executors
      first (prefer free human/zero-rate work; push paid-LLM work
      later where the deadline still allows), then the balanced rule.
    - ``throughput`` (maximize concurrent fill): shortest effort first
      so more tasks fit per unit time, then the balanced rule.
    """
    # Sort sentinel: due_date is timestamptz (migration 0005), so a
    # missing deadline still needs a value comparable to a real
    # datetime. ``dt.datetime.max`` lacks tzinfo and would raise on
    # comparison with the aware values returned by SQLAlchemy.
    _DUE_MAX = dt.datetime.max.replace(tzinfo=dt.UTC)
    if policy is SchedulePolicy.fastest:
        return lambda x: (
            _logical_slack_min(x),
            0 if (x.ls - x.es).total_seconds() <= 0 else 1,
            x.task.priority,
            x.task.due_date or _DUE_MAX,
            x.task.created_at,
            str(x.task.id),
        )
    if policy is SchedulePolicy.cheapest:
        return lambda x: (
            1 if rate_of(x) > 0 else 0,
            x.task.priority,
            x.task.due_date or _DUE_MAX,
            x.task.created_at,
            str(x.task.id),
        )
    if policy is SchedulePolicy.throughput:
        return lambda x: (
            x.duration_min,
            x.task.priority,
            x.task.due_date or _DUE_MAX,
            x.task.created_at,
            str(x.task.id),
        )
    # balanced: the canonical ADR-0004 priority rule.
    return lambda x: (
        x.task.priority,
        x.task.due_date or _DUE_MAX,
        x.task.created_at,
        str(x.task.id),
    )


def _effort_minutes(task: Task) -> int:
    hours = task.remaining_effort_h
    if hours is None:
        hours = task.estimate_effort_h
    if hours is None:
        return 0
    return round(float(hours) * 60)


def _earliest_free_slot(free: list[dt.datetime]) -> int:
    """Index of the slot that frees earliest (deterministic: smallest
    free time, lowest slot index on a tie)."""
    return min(range(len(free)), key=lambda i: (free[i], i))


def _agent_rank(
    agent: Executor, free: list[dt.datetime], earliest: dt.datetime
) -> tuple[Decimal, dt.datetime, str]:
    """Deterministic admission rank for an eligible+admissible agent:
    lowest ``credit_rate_per_hour``, then the agent's earliest effective
    free time (most free capacity / earliest-free), then
    ``str(agent.id)`` as the final stable tie-break."""
    slot_free = min(max(earliest, f) for f in free)
    return (agent.credit_rate_per_hour, slot_free, str(agent.id))


def _manual_pin_start(task: Task, prev: dict[uuid.UUID, Schedule]) -> dt.datetime | None:
    """Pinned start that must survive recompute (schedule_mode=manual,
    previously placed). Single source of truth for both passes."""
    if task.schedule_mode is not ScheduleMode.manual:
        return None
    row = prev.get(task.id)
    return row.scheduled_start if row is not None else None


def _topo(
    ids: list[uuid.UUID],
    deps: list[TaskDependency],
) -> list[uuid.UUID]:
    succ: dict[uuid.UUID, set[uuid.UUID]] = {i: set() for i in ids}
    indeg: dict[uuid.UUID, int] = {i: 0 for i in ids}
    for d in deps:
        if d.successor_id not in succ[d.predecessor_id]:
            succ[d.predecessor_id].add(d.successor_id)
            indeg[d.successor_id] += 1
    ready = sorted(i for i in ids if indeg[i] == 0)
    order: list[uuid.UUID] = []
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for s in sorted(succ[nid]):
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(s)
        ready.sort()
    if len(order) != len(ids):
        raise DomainError(MessageCode.DEPENDENCY_CYCLE)
    return order


class Scheduler:
    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self._s = session
        self._org = org_id
        self._cal: dict[uuid.UUID | None, tuple[WorkCalendar, float]] = {}

    async def _calendar(self, assignee: uuid.UUID | None) -> tuple[WorkCalendar, float]:
        if assignee not in self._cal:
            wc, cap = await build_work_calendar(self._s, self._org, assignee)
            self._cal[assignee] = (wc, float(cap) * 60.0)
        return self._cal[assignee]

    async def recompute(
        self,
        *,
        actor_id: uuid.UUID,
        project_tag_id: uuid.UUID | None = None,
        as_of: dt.datetime | None = None,
        policy: SchedulePolicy = SchedulePolicy.balanced,
    ) -> RecomputeSummary:
        await require_role(self._s, self._org, actor_id, Role.member)
        now = (as_of or dt.datetime.now(tz=dt.UTC)).astimezone(dt.UTC)

        # Lazy + idempotent: a fresh workspace gets its executor rows
        # (human-per-member + the default LLM pool) with zero manual
        # config (defaults make this a no-op vs the pre-P1 scheduler).
        await executors_svc.ensure_workspace_executors(self._s, org_id=self._org)

        stmt = select(Task).where(Task.deleted_at.is_(None), Task.is_archived.is_(False))
        if project_tag_id is not None:
            stmt = stmt.join(TaskTag, TaskTag.task_id == Task.id).where(
                TaskTag.tag_id == project_tag_id
            )
        tasks = list((await self._s.execute(stmt)).scalars().unique().all())
        if not tasks:
            return RecomputeSummary(
                count=0,
                makespan_minutes=0,
                projected_credit_cost=Decimal(0),
                policy=policy,
                unassignable_count=0,
            )
        ids = [t.id for t in tasks]
        id_set = set(ids)

        deps = [
            d
            for d in (await self._s.execute(select(TaskDependency))).scalars().all()
            if d.predecessor_id in id_set and d.successor_id in id_set
        ]
        terminal_state_ids = set(
            (
                await self._s.execute(
                    select(WorkflowState.id).where(WorkflowState.is_terminal.is_(True))
                )
            )
            .scalars()
            .all()
        )
        assignee_rows = (
            await self._s.execute(
                select(TaskCollaborator.task_id, TaskCollaborator.user_id).order_by(
                    TaskCollaborator.user_id
                )
            )
        ).all()
        first_assignee: dict[uuid.UUID, uuid.UUID] = {}
        for tid, uid in assignee_rows:
            first_assignee.setdefault(tid, uid)

        # docs/adr/0028: resolve the task's primary assignee through
        # identities. ``assignee_kind`` per task is derived from
        # ``identities.kind``; ``assignee_human_user_id`` is the
        # ``identities.user_id`` for human identities (NULL for
        # ai_assistants and for unassigned tasks).
        identity_rows = (
            await self._s.execute(
                select(
                    Task.id,
                    Identity.kind,
                    Identity.user_id,
                    AiAssistant.is_active,
                    AiAssistant.runtime,
                )
                .join(Identity, Identity.id == Task.assignee_id)
                .outerjoin(AiAssistant, AiAssistant.id == Identity.ai_assistant_id)
            )
        ).all()
        task_kind: dict[uuid.UUID, ExecKind] = {}
        task_human_user: dict[uuid.UUID, uuid.UUID | None] = {}
        # Tasks addressed to a deactivated assistant. Kept in the llm pool
        # (the assignment is a fact about the task) and flagged
        # ``assignee_inactive`` at admission, rather than dropped from
        # routing: an excluded task would keep a schedule row claiming it
        # is planned and on time while nothing ever dispatches it.
        self._assignee_inactive: set[uuid.UUID] = set()
        for tid, ikind, iuser, a_active, a_runtime in identity_rows:
            if identities_svc.is_internal_agent(ikind, a_runtime):
                task_kind[tid] = ExecKind.llm_agent
                task_human_user[tid] = None
                if a_active is not True:
                    self._assignee_inactive.add(tid)
            else:
                # An EXTERNAL assistant lands here with the humans, and
                # its ``task_human_user`` stays NULL: nothing on this
                # side of the system starts it, so it gets no executor,
                # no projected cost and no scheduled window, and
                # ``_admitted_agent_rows`` never sees it because that
                # query requires ``assigned_executor_id IS NOT NULL``.
                # Off-calendar rather than mis-scheduled -- unless a
                # task_assignee row names a person, in which case the
                # task is that person's planned work and belongs on
                # their calendar.
                task_kind[tid] = ExecKind.human
                task_human_user[tid] = iuser

        prev = {
            row.task_id: row
            for row in (await self._s.execute(select(Schedule))).scalars().all()
            if row.task_id in id_set
        }

        nodes: dict[uuid.UUID, _Node] = {}
        for t in tasks:
            # Three fallbacks for the human executor id (used by the
            # calendar resolver / serial-resource scheduler): the
            # first task_assignee row -> the identity-resolved human
            # user -> NULL (unassigned, off-calendar).
            assignee = first_assignee.get(t.id) or task_human_user.get(t.id)
            dur = 0 if t.is_milestone else _effort_minutes(t)
            # Kind via identity when an assignee is set, else fall
            # back to the task's ``executor_kind`` hint (docs/adr/0028).
            n_kind = task_kind.get(t.id, t.executor_kind)
            nodes[t.id] = _Node(
                task=t,
                assignee=assignee,
                duration_min=dur,
                terminal=t.state_id in terminal_state_ids,
                kind=n_kind,
            )

        incoming: dict[uuid.UUID, list[TaskDependency]] = {i: [] for i in ids}
        outgoing: dict[uuid.UUID, list[TaskDependency]] = {i: [] for i in ids}
        for d in deps:
            incoming[d.successor_id].append(d)
            outgoing[d.predecessor_id].append(d)

        order = _topo(ids, deps)

        # Forward pass: ES/EF.
        for nid in order:
            n = nodes[nid]
            cal, cap = await self._calendar(n.assignee)
            if n.terminal:
                anchor = n.task.actual_start or now
                n.es = anchor
                n.ef = anchor
                continue
            start_lb = [now]
            finish_lb: list[dt.datetime] = []
            if n.task.actual_start is not None:
                start_lb.append(n.task.actual_start)
            cd = n.task.constraint_date
            ck = n.task.constraint_kind.value
            if cd is not None and ck == "SNET":
                start_lb.append(cd)
            for d in incoming[nid]:
                p = nodes[d.predecessor_id]
                pcal, _ = await self._calendar(p.assignee)
                lag = d.lag_working_minutes
                if d.type is DependencyType.FS:
                    start_lb.append(pcal.add(p.ef, lag))
                elif d.type is DependencyType.SS:
                    start_lb.append(pcal.add(p.es, lag))
                elif d.type is DependencyType.FF:
                    finish_lb.append(pcal.add(p.ef, lag))
                else:  # SF
                    finish_lb.append(pcal.add(p.es, lag))
            es = cal.snap_forward(max(start_lb))
            if finish_lb:
                need = max(finish_lb)
                es = max(es, cal.add(need, -n.duration_min))
            if cd is not None and ck == "MSO":
                es = cd
            pin = _manual_pin_start(n.task, prev)
            if pin is not None:
                es = pin
            if cd is not None and ck == "MFO":
                n.ef = cd
                n.es = cal.add(cd, -n.duration_min)
            else:
                n.es = es
                n.ef = es if n.duration_min == 0 else cal.add_capped(es, n.duration_min, cap)

        project_end = max(n.ef for n in nodes.values())

        # Backward pass: LF/LS, slack, logical critical path.
        for nid in reversed(order):
            n = nodes[nid]
            cal, _ = await self._calendar(n.assignee)
            finish_ub = [project_end]
            for d in outgoing[nid]:
                snode = nodes[d.successor_id]
                lag = d.lag_working_minutes
                if d.type is DependencyType.FS:
                    finish_ub.append(cal.add(snode.ls, -lag))
                elif d.type is DependencyType.SS:
                    finish_ub.append(cal.add(cal.add(snode.ls, -lag), n.duration_min))
                elif d.type is DependencyType.FF:
                    finish_ub.append(cal.add(snode.lf, -lag))
                else:  # SF
                    finish_ub.append(cal.add(cal.add(snode.ls, -lag), n.duration_min))
            n.lf = min(finish_ub)
            n.ls = cal.add(n.lf, -n.duration_min)

        # --- Stage 2: resource-constrained list scheduling (ADR-0025) ---
        # Executors were just ensured. Index humans by user (the row
        # carries only the switch penalty; the calendar is still
        # resolved by user via build_work_calendar) and resolve the SET
        # of enabled llm_agent executors (P2: capability-matched
        # admission control across multiple agents, replacing P1's single
        # default pool). Deterministic order: (kind, name, id).
        exec_rows = list(
            (
                await self._s.execute(
                    select(Executor).order_by(Executor.kind, Executor.name, Executor.id)
                )
            )
            .scalars()
            .all()
        )
        human_exec: dict[uuid.UUID, Executor] = {}
        for e in exec_rows:
            if e.kind is ExecutorKind.human and e.user_id is not None:
                human_exec.setdefault(e.user_id, e)
        # Enabled llm_agent executors, deterministically ordered. Each is
        # an admission target: K=max_parallel concurrent slots (24/7, no
        # calendar) + an optional credit_budget.
        agents: list[Executor] = [
            e for e in exec_rows if e.kind is ExecutorKind.llm_agent and e.enabled
        ]
        agent_caps: dict[uuid.UUID, set[str]] = {a.id: set(a.capability_tags or []) for a in agents}
        # Per-agent rate, for the projected cost of a task assigned to it.
        agent_rate_by_id: dict[uuid.UUID, Decimal] = {a.id: a.credit_rate_per_hour for a in agents}

        def _effort_hours(n: _Node) -> Decimal:
            h = n.task.remaining_effort_h
            if h is None:
                h = n.task.estimate_effort_h
            return Decimal(0) if h is None else Decimal(h)

        def _eligible_agents(n: _Node) -> list[Executor]:
            """Enabled agents whose capability_tags ⊇ the task's
            required_capabilities (empty required = any enabled agent)."""
            req = set(n.task.required_capabilities or [])
            return [a for a in agents if req <= agent_caps[a.id]]

        def _node_rate(n: _Node) -> Decimal:
            # Policy-key rate proxy: an llm task's projected rate is the
            # cheapest eligible agent's rate (the dispatcher prefers the
            # lowest rate); 0 for human work or no eligible agent.
            if n.kind is not ExecKind.llm_agent:
                return Decimal(0)
            elig = _eligible_agents(n)
            if not elig:
                return Decimal(0)
            return min(a.credit_rate_per_hour for a in elig)

        key = _policy_key(policy, _node_rate)

        # Partition: human serial timeline vs LLM K-pool vs the
        # off-timeline rest (terminals, zero-duration milestones,
        # human tasks with no assignee) which keep their CPM ES/EF.
        sched: dict[uuid.UUID, tuple[dt.datetime, dt.datetime]] = {}
        # Per-resource successor edges built as we place, for the
        # leveled critical-chain backward pass (the task placed right
        # after this one on the same human / freed pool slot).
        res_succ: dict[uuid.UUID, set[uuid.UUID]] = {i: set() for i in ids}
        by_person: dict[uuid.UUID, list[_Node]] = {}
        llm_nodes: list[_Node] = []
        # Migration 0094 / ADR-0008 addendum: appointment-tasks
        # (``duration_minutes`` NOT NULL on the row) are pre-placed
        # hard constraints. They never enter the placement loop:
        # ``start_at`` is the fixed pin, ``start_at + duration`` the
        # end. They also feed the per-person busy list so plain work
        # is scheduled around them. The legacy ``events`` table was
        # dropped in migration 0097.
        appt_busy_by_user: dict[uuid.UUID, list[tuple[dt.datetime, dt.datetime]]] = {}
        # Migration 0095/0096: every participant of an appointment-task
        # (including the assignee, mirrored by the 0096 trigger) lives
        # in ``task_participants`` with the denormalised window. Pull
        # them all in one query and bucket by ``users.id`` (resolved
        # through ``identities`` -- AI assistants have no users.id and
        # are not put on the human serialization timeline). This way
        # the scheduler sees N-way appointments: every participant has
        # the slot in their busy list, not just the assignee.
        participant_rows = (
            await self._s.execute(
                select(
                    Identity.user_id,
                    TaskParticipant.start_at,
                    TaskParticipant.duration_minutes,
                )
                .join(Identity, Identity.id == TaskParticipant.identity_id)
                .where(Identity.user_id.is_not(None))
            )
        ).all()
        for uid, p_start, p_dur in participant_rows:
            p_end = p_start + dt.timedelta(minutes=p_dur)
            appt_busy_by_user.setdefault(uid, []).append((p_start, p_end))
        for n in nodes.values():
            t = n.task
            if t.start_at is not None and t.duration_minutes is not None:
                # Pre-placed: fixed window, never moves. Busy list is
                # populated above from task_participants (covers the
                # assignee via the 0096 mirror trigger and every extra
                # participant), so we only place the row in ``sched``.
                appt_end = t.start_at + dt.timedelta(minutes=t.duration_minutes)
                sched[t.id] = (t.start_at, appt_end)
                n.ss, n.se = t.start_at, appt_end
                continue
            if (
                n.kind is ExecKind.human
                and n.assignee is not None
                and not n.terminal
                and n.duration_min > 0
            ):
                by_person.setdefault(n.assignee, []).append(n)
            elif n.kind is ExecKind.llm_agent and not n.terminal and n.duration_min > 0:
                llm_nodes.append(n)
            else:
                sched[n.task.id] = (n.es, n.ef)

        # Humans: per-person serialization + event-clash avoidance +
        # the policy priority rule. Between consecutive distinct tasks
        # for the person, advance the cursor by that human executor's
        # context_switch_cost_minutes (snap to the calendar). No penalty
        # before the first task and none for a manual pin (a pin is
        # fixed and does not move the switching cursor).
        for user_id, plist in sorted(by_person.items(), key=lambda kv: str(kv[0])):
            cal, cap = await self._calendar(user_id)
            switch_min = (
                human_exec[user_id].context_switch_cost_minutes if user_id in human_exec else 0
            )
            # Hard constraints on the person's timeline: appointment-
            # tasks (migration 0094) collected above into
            # ``appt_busy_by_user`` via task_participants (the 0096
            # trigger mirrors the assignee so the loop sees both the
            # primary owner and every extra participant).
            busy: list[tuple[dt.datetime, dt.datetime]] = sorted(appt_busy_by_user.get(user_id, []))
            plist.sort(key=key)
            cursor = now
            placed_first = False
            prev_node_id: uuid.UUID | None = None
            for n in plist:
                pin = _manual_pin_start(n.task, prev)
                if pin is not None:
                    end = cal.add_capped(pin, n.duration_min, cap)
                    sched[n.task.id] = (pin, end)
                    n.ss, n.se = pin, end
                    # A pin does not advance the switching cursor and
                    # creates no resource-successor edge (it is fixed).
                    continue
                base = max(n.es, cursor)
                if placed_first and switch_min > 0:
                    # Context switch: lose switch_min of working time
                    # before the next distinct task can start.
                    base = cal.add(cal.snap_forward(base), switch_min)
                start = cal.snap_forward(base)
                end = start
                for _ in range(0, 1000):
                    end = cal.add_capped(start, n.duration_min, cap)
                    clash = next(
                        (b for b in busy if b[0] < end and b[1] > start),
                        None,
                    )
                    if clash is None:
                        break
                    start = cal.snap_forward(clash[1])
                sched[n.task.id] = (start, end)
                n.ss, n.se = start, end
                if prev_node_id is not None:
                    res_succ[prev_node_id].add(n.task.id)
                cursor = end
                placed_first = True
                prev_node_id = n.task.id

        # LLM agents (P2 admission-control dispatch, replacing P1's
        # single default pool). Each enabled agent is a K-parallel
        # resource (max_parallel slots, 24/7 -- no working calendar, no
        # daily cap) with an optional credit_budget. For each llm task,
        # in deterministic policy order, among its CAPABILITY-ELIGIBLE
        # enabled agents pick the admissible one (a slot free at the
        # earliest time AND projected cumulative spend on that agent +
        # this task's cost <= its credit_budget if set). Assign it, place
        # it on that agent's pool timeline, accumulate that agent's
        # spend. A task with no admissible eligible agent is FLAGGED
        # ``unassignable`` (it is NOT silently scheduled as in P1) and
        # keeps its logical ES/EF as a visible dispatch gap.
        #
        # Processed in topo order so a predecessor's leveled finish gates
        # its successor; within that, ties broken by the policy key then
        # str(id) (deterministic). Per-agent slot state:
        agent_state: dict[uuid.UUID, tuple[list[dt.datetime], list[uuid.UUID | None], Decimal]] = {}
        for a in agents:
            k = max(1, a.max_parallel)
            agent_state[a.id] = ([now] * k, [None] * k, Decimal(0))
        unassignable_ids: set[uuid.UUID] = set()
        # Topo order tie-broken by the policy key (then str(id) inside
        # the key): a fully deterministic dispatch sequence.
        order_pos = {nid: i for i, nid in enumerate(order)}
        llm_seq = sorted(
            llm_nodes,
            key=lambda n: (order_pos[n.task.id], key(n)),
        )
        for ln in llm_seq:
            nid = ln.task.id
            pin = _manual_pin_start(ln.task, prev)
            if nid in self._assignee_inactive:
                # Addressed to an assistant that can no longer act. Not a
                # capability problem and not a budget problem, so it gets
                # its own reason: "no_capable_agent" would send the owner
                # looking for a missing capability tag.
                ln.assigned = None
                ln.unassignable_reason = _UNASSIGNABLE_ASSIGNEE_INACTIVE
                unassignable_ids.add(nid)
                sched[nid] = (ln.es, ln.ef)
                continue
            elig = _eligible_agents(ln)
            if not elig:
                # No enabled agent advertises every required capability
                # (or no enabled agent at all). Flagged dispatch gap.
                ln.assigned = None
                ln.unassignable_reason = _UNASSIGNABLE_NO_CAPABLE_AGENT
                unassignable_ids.add(nid)
                sched[nid] = (ln.es, ln.ef)
                continue
            eff_h = _effort_hours(ln)
            earliest = max(ln.es, now)
            if pin is not None:
                earliest = pin
            else:
                for d in incoming[nid]:
                    p = nodes[d.predecessor_id]
                    if p.se != dt.datetime.min and d.type in (
                        DependencyType.FS,
                        DependencyType.FF,
                    ):
                        earliest = max(earliest, p.se)
                    elif p.ss != dt.datetime.min:
                        earliest = max(earliest, p.ss)
            # Among eligible agents, those that ADMIT this task on budget
            # (no budget set = unlimited; budget set = cumulative + this
            # task's cost on that agent must not exceed it).
            admissible: list[Executor] = []
            for a in elig:
                free, _last, spent = agent_state[a.id]
                cost_on_a = eff_h * agent_rate_by_id[a.id]
                if a.credit_budget is not None and spent + cost_on_a > a.credit_budget:
                    continue
                admissible.append(a)
            if not admissible:
                # Eligible agents exist but every one would exceed budget.
                ln.assigned = None
                ln.unassignable_reason = _UNASSIGNABLE_BUDGET_EXHAUSTED
                unassignable_ids.add(nid)
                sched[nid] = (ln.es, ln.ef)
                continue
            # Deterministic executor pick: lowest credit_rate_per_hour,
            # then earliest-free / most free capacity (the smallest
            # earliest-free slot time across the agent's slots), then
            # str(executor.id) as the final stable tie-break.
            chosen = min(
                admissible,
                key=lambda a: _agent_rank(a, agent_state[a.id][0], earliest),
            )
            free, last, spent = agent_state[chosen.id]
            # Earliest freed slot on the chosen agent (deterministic:
            # smallest free time, lowest slot index on a tie).
            slot = _earliest_free_slot(free)
            prev_in_slot = last[slot]
            start = max(earliest, free[slot])
            end = start + dt.timedelta(minutes=ln.duration_min)
            free[slot] = end
            last[slot] = nid
            agent_state[chosen.id] = (
                free,
                last,
                spent + eff_h * agent_rate_by_id[chosen.id],
            )
            ln.assigned = chosen.id
            ln.unassignable_reason = None
            sched[nid] = (start, end)
            ln.ss, ln.se = start, end
            if prev_in_slot is not None:
                res_succ[prev_in_slot].add(nid)

        # Resource-aware critical chain (ADR-0025). Definition: a task
        # is on the critical chain iff it has ZERO float in the LEVELED
        # plan -- i.e. delaying it delays the project makespan once
        # resource contention is accounted for. Computed by a backward
        # pass over the leveled graph = precedence successors UNION the
        # per-resource successor (next task on the same human / the same
        # freed pool slot). leveled_lf(n) = makespan if n has no leveled
        # successor else min(leveled_start(succ)); n is on the chain iff
        # leveled_lf(n) - scheduled_end(n) <= 0. This is distinct from
        # on_logical_critical_path (infinite-resource CPM) and is a
        # superset of it under resource contention. Fully deterministic.
        #
        # P2: an UNASSIGNABLE llm task is a dispatch gap, not a placed
        # task -- it has no scheduled_start/end, does not drive the
        # makespan, and is excluded from the leveled graph (it cannot be
        # on the critical chain). Its logical ES/EF/LS/LF still emit.
        for nid in ids:
            n = nodes[nid]
            if nid not in sched:
                sched[nid] = (n.es, n.ef)
            n.ss, n.se = sched[nid]
        makespan_end = max(
            (n.se for nid, n in nodes.items() if nid not in unassignable_ids),
            default=now,
        )
        leveled_succ: dict[uuid.UUID, set[uuid.UUID]] = {
            i: set(res_succ[i]) for i in ids if i not in unassignable_ids
        }
        for d in deps:
            if d.predecessor_id in unassignable_ids or d.successor_id in unassignable_ids:
                continue
            leveled_succ[d.predecessor_id].add(d.successor_id)
        for nid in reversed(order):
            if nid in unassignable_ids:
                continue
            n = nodes[nid]
            succs = leveled_succ[nid]
            n.llf = makespan_end if not succs else min(nodes[s].ss for s in succs)
        on_chain: dict[uuid.UUID, bool] = {
            nid: (
                False
                if nid in unassignable_ids
                else int((nodes[nid].llf - nodes[nid].se).total_seconds()) <= 0
            )
            for nid in ids
        }

        await self._s.execute(delete(Schedule).where(Schedule.task_id.in_(ids)))
        # Fingerprint folds the policy AND the executor set
        # (id:version, deterministically ordered): a registry change
        # (a new agent, an edited budget/capability, an enable toggle)
        # reschedules even when no task changed.
        exec_fp = "|".join(
            f"{e.id}:{e.version}" for e in sorted(exec_rows, key=lambda x: str(x.id))
        )
        fp = hashlib.sha256(
            (
                f"{policy.value}|"
                + "|".join(f"{t.id}:{t.version}" for t in sorted(tasks, key=lambda x: str(x.id)))
                + f"|exec:{exec_fp}"
            ).encode()
        ).hexdigest()
        total_cost = Decimal(0)
        for nid, n in nodes.items():
            ss, se = sched[nid]
            is_unassignable = nid in unassignable_ids
            # Projected cost = effort_h * the ASSIGNED agent's rate
            # (0 for human work or an unassignable llm task -- no
            # executor, no cost).
            if n.kind is ExecKind.llm_agent and n.assigned is not None and not is_unassignable:
                pcost = _effort_hours(n) * agent_rate_by_id.get(n.assigned, Decimal(0))
            else:
                pcost = Decimal(0)
            total_cost += pcost
            self._s.add(
                Schedule(
                    task_id=nid,
                    org_id=self._org,
                    es=n.es,
                    ef=n.ef,
                    ls=n.ls,
                    lf=n.lf,
                    slack_minutes=max(
                        0,
                        int((n.ls - n.es).total_seconds() // 60),
                    ),
                    on_logical_critical_path=(int((n.ls - n.es).total_seconds()) <= 0),
                    on_critical_chain=on_chain[nid],
                    projected_cost=pcost,
                    # Unassignable llm tasks have no placement: NULL
                    # scheduled_start/end (a visible dispatch gap).
                    scheduled_start=None if is_unassignable else ss,
                    scheduled_end=None if is_unassignable else se,
                    assigned_executor_id=n.assigned,
                    unassignable=is_unassignable,
                    unassignable_reason=n.unassignable_reason,
                    computed_at=now,
                    input_fingerprint=fp,
                )
            )
        await self._s.flush()
        await audit.log(
            self._s,
            org_id=self._org,
            actor_id=actor_id,
            entity="schedule",
            entity_id=None,
            action="recompute",
        )
        makespan_minutes = max(0, int((makespan_end - now).total_seconds() // 60))
        unassignable_count = len(unassignable_ids)
        if unassignable_count:
            # Surface the dispatch gaps in the audit trail (the count is
            # also returned in the summary and per-row on the schedule).
            await audit.log(
                self._s,
                org_id=self._org,
                actor_id=actor_id,
                entity="schedule",
                entity_id=None,
                action="dispatch_unassignable",
                diff={"count": str(unassignable_count)},
            )
        return RecomputeSummary(
            count=len(tasks),
            makespan_minutes=makespan_minutes,
            projected_credit_cost=total_cost,
            policy=policy,
            unassignable_count=unassignable_count,
        )


async def recompute(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_tag_id: uuid.UUID | None = None,
    as_of: dt.datetime | None = None,
    policy: SchedulePolicy = SchedulePolicy.balanced,
) -> RecomputeSummary:
    return await Scheduler(session, org_id).recompute(
        actor_id=actor_id,
        project_tag_id=project_tag_id,
        as_of=as_of,
        policy=policy,
    )


async def get_schedule(
    session: AsyncSession, *, org_id: uuid.UUID, task_id: uuid.UUID
) -> Schedule | None:
    return (
        await session.execute(select(Schedule).where(Schedule.task_id == task_id))
    ).scalar_one_or_none()


async def list_schedule(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_tag_id: uuid.UUID | None = None,
) -> list[Schedule]:
    if project_tag_id is None:
        return list((await session.execute(select(Schedule))).scalars().all())
    rows = (
        (
            await session.execute(
                select(Schedule)
                .join(TaskTag, TaskTag.task_id == Schedule.task_id)
                .where(TaskTag.tag_id == project_tag_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
