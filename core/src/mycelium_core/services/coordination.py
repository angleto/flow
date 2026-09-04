"""Coordination / handoff protocol + contract-net delegation
(docs/adr/0025, P4).

A handoff is a typed message bound to a dependency edge. It is the
SAME artifact+message primitive for every recipient kind (LLM<->LLM,
LLM<->human, human<->human, human<->LLM):

- **On task completion** (the single ``tasks.set_state`` hook, fired
  only when the workflow transition crosses INTO a terminal state and
  the old state was not terminal): for every ``TaskDependency`` where
  the completed task is the predecessor, resolve the producer artifact
  (the predecessor's latest ``AgentRun.artifact_note_id`` if any, else
  its latest work note id -- may be ``None``: a message-only handoff is
  valid) and create/refresh a ``TaskHandoff`` to the successor. Then
  deliver it by the successor's RESOLVED executor:
    - human executor (or no executor row -> the successor's
      ``TaskCollaborator`` users): a ``task_handoff`` notification per user
      + the artifact note linked to the successor task (idempotent);
      status -> ``delivered``.
    - llm_agent executor: stays ``pending`` (no notification); it is
      consumed by P3 -- ``agent_runtime._build_context`` surfaces
      pending incoming handoffs and ``start_run`` marks them
      ``consumed``.
  The hook is ADDITIVE and NON-FATAL: any delivery error is swallowed
  (logged via audit), the state transition is the source of truth and
  must never be blocked by a coordination failure. Re-entering the same
  terminal state is a no-op (idempotent: at most one ACTIVE --
  pending|delivered -- handoff per (predecessor, successor) edge).

- **Contract-net** (the human-side primitive; the llm_agent "award" is
  already the P2 admission dispatch and is NOT re-implemented here):
  ``offer`` (owner-gated) marks ``tasks.offered`` and announces a
  ``task_offer`` notification to the eligible human members (capability
  match against ``Task.required_capabilities`` ⊆ a human Executor's
  ``capability_tags``, else all members). ``claim`` (member) awards the
  task to the caller (becomes a ``TaskCollaborator``), clears ``offered``,
  notifies the offerer. ``decline`` (member) is lightweight: an audit
  record + a notification back to the offerer (no bid table).

Determinism: handoff fan-out iterates dependencies and recipients in a
stable order (created_at, then ``str(id)``); ``incoming_for_context``
is ordered identically so a scripted agent run is reproducible.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_run import AgentRun
from mycelium_core.models.dependency import TaskDependency
from mycelium_core.models.executor import Executor, ExecutorKind
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.note import Note
from mycelium_core.models.notification import NotificationChannelKind
from mycelium_core.models.schedule import Schedule
from mycelium_core.models.task import Task
from mycelium_core.models.task_collaborator import TaskCollaborator
from mycelium_core.models.task_handoff import HandoffStatus, TaskHandoff
from mycelium_core.models.user import User
from mycelium_core.services import audit
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services import notifications as notif_svc
from mycelium_core.services.note_effective import effective_note_clause
from mycelium_core.services.rbac import require_role

# In-app coordination notifications are an inbox primitive, not an
# external dispatch: they are recorded on a fixed canonical channel
# regardless of per-user channel prefs (dispatch_pending still honours
# prefs if/when these are externally delivered). Reusing the existing
# notifications substrate keeps a single delivery model.
_HANDOFF_CHANNEL = NotificationChannelKind.email

_ACTIVE_STATUSES = (HandoffStatus.pending, HandoffStatus.delivered)


# --------------------------------------------------------------------- #
# Producer-artifact + executor resolution
# --------------------------------------------------------------------- #


async def _producer_artifact_note_id(
    session: AsyncSession, *, org_id: uuid.UUID, task_id: uuid.UUID
) -> uuid.UUID | None:
    """The predecessor's produced artifact: its latest agent-run
    artifact note (P3 producer) if any, else its latest work note
    (a note linked to the task). ``None`` => a message-only handoff
    (valid). Deterministic: newest by (created_at, id)."""
    run_artifact = (
        await session.execute(
            select(AgentRun.artifact_note_id)
            .where(
                AgentRun.task_id == task_id,
                AgentRun.artifact_note_id.is_not(None),
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run_artifact is not None:
        return run_artifact
    # docs/adr/0029 P3: resolve the producer's artifact note through
    # the typed link (kind='artifact') instead of the legacy
    # ``Note.task_id`` column.
    note_ids = await note_links_svc.notes_for_task(
        session, org_id=org_id, task_id=task_id, kinds=("artifact",)
    )
    if not note_ids:
        return None
    return (
        await session.execute(
            select(Note.id)
            .where(Note.id.in_(note_ids), effective_note_clause())
            .order_by(Note.created_at.desc(), Note.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _resolved_executor(session: AsyncSession, *, task_id: uuid.UUID) -> Executor | None:
    """A task's resolved executor = its Schedule row's
    ``assigned_executor_id`` -> the Executor (P2 dispatch). ``None``
    when there is no Schedule row / no assignment."""
    sched = (
        await session.execute(select(Schedule).where(Schedule.task_id == task_id))
    ).scalar_one_or_none()
    if sched is None or sched.assigned_executor_id is None:
        return None
    return (
        await session.execute(select(Executor).where(Executor.id == sched.assigned_executor_id))
    ).scalar_one_or_none()


async def _human_recipients(
    session: AsyncSession, *, task_id: uuid.UUID, executor: Executor | None
) -> list[uuid.UUID]:
    """The human users a handoff/notification for ``task_id`` is
    delivered to: the resolved human executor's ``user_id`` if the
    executor is a human, else (no executor row, or a human executor
    with no user) the successor task's ``TaskCollaborator`` users. An
    llm_agent executor yields NO human recipient (it consumes via the
    P3 context path). Deterministic order: sorted by ``str(uuid)``."""
    if executor is not None and executor.kind is ExecutorKind.human:
        if executor.user_id is not None:
            return [executor.user_id]
        # human executor with no bound user -> fall back to assignees.
    elif executor is not None and executor.kind is ExecutorKind.llm_agent:
        return []
    rows = (
        (
            await session.execute(
                select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    return sorted(set(rows), key=str)


# --------------------------------------------------------------------- #
# On-completion handoff fan-out (called from tasks.set_state)
# --------------------------------------------------------------------- #


async def on_task_completed(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task: Task,
) -> None:
    """Create/refresh + deliver a handoff for every dependent of a task
    that just reached a terminal state. NON-FATAL: this is best-effort
    coordination; the caller's state transition is the source of truth
    and must never be rolled back by a handoff failure. Idempotent: an
    already-active (pending|delivered) handoff for the same edge is
    refreshed in place, never duplicated.

    Determinism: dependencies are processed ordered by (created_at,
    str(id)); human recipients ordered by ``str(uuid)``.
    """
    deps = list(
        (
            await session.execute(
                select(TaskDependency)
                .where(TaskDependency.predecessor_id == task.id)
                .order_by(TaskDependency.created_at, TaskDependency.id)
            )
        )
        .scalars()
        .all()
    )
    if not deps:
        return
    artifact_note_id = await _producer_artifact_note_id(session, org_id=org_id, task_id=task.id)
    from_exec = await _resolved_executor(session, task_id=task.id)
    message = f"Handoff from completed task: {task.title}"[:1000]

    for dep in deps:
        try:
            await _deliver_one(
                session,
                org_id=org_id,
                actor_id=actor_id,
                predecessor=task,
                successor_id=dep.successor_id,
                from_executor=from_exec,
                artifact_note_id=artifact_note_id,
                message=message,
            )
        except Exception as exc:  # coordination boundary: never fatal
            # A handoff-delivery failure must not break the workflow
            # state transition. Record it and leave the handoff (if
            # created) pending; the transition stands.
            await audit.log(
                session,
                org_id=org_id,
                actor_id=actor_id,
                entity="task_handoff",
                entity_id=dep.successor_id,
                action="deliver_failed",
                diff={"predecessor": str(task.id), "error": str(exc)[:200]},
            )


async def _deliver_one(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    predecessor: Task,
    successor_id: uuid.UUID,
    from_executor: Executor | None,
    artifact_note_id: uuid.UUID | None,
    message: str,
) -> None:
    """Create/refresh exactly one edge's handoff and deliver it by the
    successor's resolved executor kind."""
    to_exec = await _resolved_executor(session, task_id=successor_id)
    existing = (
        await session.execute(
            select(TaskHandoff)
            .where(
                TaskHandoff.predecessor_task_id == predecessor.id,
                TaskHandoff.successor_task_id == successor_id,
                TaskHandoff.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(TaskHandoff.created_at, TaskHandoff.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is None:
        ho = TaskHandoff(
            org_id=org_id,
            predecessor_task_id=predecessor.id,
            successor_task_id=successor_id,
            from_executor_id=from_executor.id if from_executor is not None else None,
            to_executor_id=to_exec.id if to_exec is not None else None,
            message=message,
            artifact_note_id=artifact_note_id,
            status=HandoffStatus.pending,
        )
        session.add(ho)
        await session.flush()
        created = True
    else:
        # Re-completion: refresh the active row rather than duplicate
        # (keeps the at-most-one-active invariant; re-arms delivery).
        ho = existing
        ho.from_executor_id = from_executor.id if from_executor is not None else None
        ho.to_executor_id = to_exec.id if to_exec is not None else None
        ho.message = message
        ho.artifact_note_id = artifact_note_id
        ho.status = HandoffStatus.pending
        ho.delivered_at = None
        ho.consumed_at = None
        ho.version += 1
        await session.flush()
        created = False
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_handoff",
        entity_id=ho.id,
        action="create" if created else "refresh",
        diff={
            "predecessor": str(predecessor.id),
            "successor": str(successor_id),
        },
    )

    # Deliver by the successor's resolved executor kind.
    recipients = await _human_recipients(session, task_id=successor_id, executor=to_exec)
    if not recipients:
        # llm_agent successor (or a flagged/absent executor with no
        # assignee): stays pending. The P3 runtime consumes it.
        return
    for uid in recipients:
        await notif_svc.enqueue(
            session,
            org_id=org_id,
            actor_id=actor_id,
            user_id=uid,
            channel=_HANDOFF_CHANNEL,
            kind="task_handoff",
            title=f"Handoff: {predecessor.title}"[:300],
            body=message,
            dedupe_key=f"task_handoff:{ho.id}:{uid}",
        )
    # Give the human context: link the artifact note to the successor
    # task as a typed artifact link (docs/adr/0029 P3: replaces the
    # legacy ``note.task_id`` write). Idempotent; a no-op if the link
    # already exists for this (note, task, artifact) triple.
    if artifact_note_id is not None:
        note = (
            await session.execute(
                select(Note).where(Note.id == artifact_note_id, Note.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if note is not None:
            await note_links_svc.ensure_artifact_link(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=artifact_note_id,
                task_id=successor_id,
            )
    ho.status = HandoffStatus.delivered
    ho.delivered_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_handoff",
        entity_id=ho.id,
        action="deliver",
        diff={"recipients": str(len(recipients))},
    )


# --------------------------------------------------------------------- #
# LLM-recipient delivery path (consumed by agent_runtime, P3)
# --------------------------------------------------------------------- #


async def incoming_for_context(
    session: AsyncSession, *, task_id: uuid.UUID
) -> list[tuple[TaskHandoff, Task, Note | None]]:
    """Pending incoming handoffs for ``task_id`` (the LLM-recipient
    path), each with its predecessor task and artifact note (if any),
    in a STABLE deterministic order (created_at, then str(id)) so a
    scripted agent run is reproducible. Only ``pending`` rows: a
    delivered (human) or already-consumed handoff is excluded."""
    rows = list(
        (
            await session.execute(
                select(TaskHandoff)
                .where(
                    TaskHandoff.successor_task_id == task_id,
                    TaskHandoff.status == HandoffStatus.pending,
                )
                .order_by(TaskHandoff.created_at, TaskHandoff.id)
            )
        )
        .scalars()
        .all()
    )
    out: list[tuple[TaskHandoff, Task, Note | None]] = []
    for ho in rows:
        pred = (
            await session.execute(select(Task).where(Task.id == ho.predecessor_task_id))
        ).scalar_one_or_none()
        if pred is None:
            continue
        note: Note | None = None
        if ho.artifact_note_id is not None:
            # The artifact's title and BODY go straight into the recipient's
            # prompt (``agent_runtime._build_context``), so it has to be an
            # effective note: a trashed artifact is material the user threw
            # away and an un-approved proposal is withheld by ADR-0043. The
            # handoff message itself still reaches the agent -- only the
            # artifact quotation drops (task 854f1c28).
            note = (
                await session.execute(
                    select(Note).where(Note.id == ho.artifact_note_id, effective_note_clause())
                )
            ).scalar_one_or_none()
        out.append((ho, pred, note))
    return out


async def mark_incoming_consumed(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
) -> int:
    """Mark every pending incoming handoff for ``task_id`` as
    ``consumed`` (called when a P3 agent run starts on the task).
    Returns the count consumed. Idempotent: a second call finds none.
    Order stable (created_at, id) for a deterministic audit trail."""
    rows = list(
        (
            await session.execute(
                select(TaskHandoff)
                .where(
                    TaskHandoff.successor_task_id == task_id,
                    TaskHandoff.status == HandoffStatus.pending,
                )
                .order_by(TaskHandoff.created_at, TaskHandoff.id)
            )
        )
        .scalars()
        .all()
    )
    now = dt.datetime.now(tz=dt.UTC)
    for ho in rows:
        ho.status = HandoffStatus.consumed
        ho.consumed_at = now
        ho.version += 1
    if rows:
        await session.flush()
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="task_handoff",
            entity_id=task_id,
            action="consume",
            diff={"count": str(len(rows))},
        )
    return len(rows)


async def list_handoffs(
    session: AsyncSession, *, org_id: uuid.UUID, task_id: uuid.UUID
) -> list[TaskHandoff]:
    """Incoming + outgoing handoffs touching ``task_id`` (member-level
    read), newest first then ``str(id)`` (stable). RLS scopes to the
    org; a foreign task simply yields none (cross-org isolation)."""
    return list(
        (
            await session.execute(
                select(TaskHandoff)
                .where(
                    (TaskHandoff.predecessor_task_id == task_id)
                    | (TaskHandoff.successor_task_id == task_id)
                )
                .order_by(TaskHandoff.created_at.desc(), TaskHandoff.id)
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------- #
# Contract-net delegation (human-side primitive)
# --------------------------------------------------------------------- #


async def _eligible_member_users(
    session: AsyncSession, *, org_id: uuid.UUID, task: Task
) -> list[uuid.UUID]:
    """The human members eligible for an offered task: members bound to
    a human Executor whose ``capability_tags`` ⊇ the task's
    ``required_capabilities``; if the task requires no capability (or no
    human executor is capability-tagged), ALL members of the workspace.
    Deterministic order: sorted by ``str(uuid)``."""
    # Live members only, and filtered HERE rather than at the end: the
    # capability narrowing below falls back to ``member_ids`` when no
    # tagged human matches, so filtering afterwards would defeat that
    # fallback exactly when the capable humans are the deactivated ones.
    # Same ordering argument as the archived-client fix -- a narrowing
    # applied over a set that still holds dead rows silently returns
    # fewer live candidates than intended.
    member_ids = sorted(
        set(
            (
                await session.execute(
                    select(Membership.user_id)
                    .join(User, User.id == Membership.user_id)
                    .where(Membership.org_id == org_id, User.is_active.is_(True))
                )
            )
            .scalars()
            .all()
        ),
        key=str,
    )
    required = set(task.required_capabilities or [])
    if not required:
        return member_ids
    human_execs = list(
        (await session.execute(select(Executor).where(Executor.kind == ExecutorKind.human)))
        .scalars()
        .all()
    )
    capable = {
        e.user_id
        for e in human_execs
        if e.user_id is not None and required <= set(e.capability_tags or [])
    }
    eligible = [u for u in member_ids if u in capable]
    # No capability-tagged human matches -> fall back to all members
    # (capability tags are advisory for humans; never strand a task).
    return eligible or member_ids


async def offer_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
) -> Task:
    """Owner: announce a task to the eligible human members (contract-
    net call-for-proposals). Marks ``tasks.offered`` and enqueues a
    ``task_offer`` notification to each eligible member. Owner-gated
    (effective-role sudo enforced), like the other privileged task
    ops. Idempotent re-offer just re-announces."""
    await require_role(session, org_id, actor_id, Role.owner)
    task = (
        await session.execute(select(Task).where(Task.id == task_id, Task.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if task is None:
        raise DomainError(MessageCode.TASK_NOT_FOUND)
    # Resolve the audience BEFORE marking the task offered. A
    # ``task_offer`` notification is the only discovery channel there is
    # (there is no bid table and no offered-tasks queue), so an offer with
    # zero recipients would set ``offered=True``, tell nobody, and leave
    # the task exactly as stranded as the "never strand a task" fallback
    # in ``_eligible_member_users`` exists to prevent. Reachable: an admin
    # in sudo mode can offer inside a workspace whose members are all
    # deactivated. Refuse instead, to a caller who can act on it.
    recipients = await _eligible_member_users(session, org_id=org_id, task=task)
    if not recipients:
        raise DomainError(MessageCode.TASK_OFFER_NO_RECIPIENTS)
    task.offered = True
    task.version += 1
    await session.flush()
    for uid in recipients:
        await notif_svc.enqueue(
            session,
            org_id=org_id,
            actor_id=actor_id,
            user_id=uid,
            channel=_HANDOFF_CHANNEL,
            kind="task_offer",
            title=f"Task offered: {task.title}"[:300],
            body=f"'{task.title}' is open to claim.",
            dedupe_key=f"task_offer:{task.id}:{task.version}:{uid}",
        )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task.id,
        action="offer",
        diff={"recipients": str(len(recipients))},
    )
    return task


async def claim_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
) -> Task:
    """Member: claim an offered task (contract-net award). The caller
    becomes a ``TaskCollaborator``, ``offered`` is cleared, the offerer is
    notified. Rejects a non-offered task (TASK_NOT_OFFERED) or one
    already claimed (TASK_ALREADY_CLAIMED -- offered but the caller is
    already an assignee, or any assignee exists)."""
    await require_role(session, org_id, actor_id, Role.member)
    task = (
        await session.execute(select(Task).where(Task.id == task_id, Task.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if task is None:
        raise DomainError(MessageCode.TASK_NOT_FOUND)
    if not task.offered:
        raise DomainError(MessageCode.TASK_NOT_OFFERED)
    existing_assignees = (
        (
            await session.execute(
                select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    if existing_assignees:
        # An offered task that already has an assignee was already
        # awarded (claimed); a second claim is rejected.
        raise DomainError(MessageCode.TASK_ALREADY_CLAIMED)
    # The award itself is a compare-and-swap on ``offered``, and the
    # three pre-reads above stay: they are the contract (a re-offered
    # task must still answer TASK_ALREADY_CLAIMED), not the guard.
    #
    # They cannot BE the guard. Two members claiming one offered task
    # both read zero collaborator rows, both insert their own -- the PK
    # is (task_id, user_id), so the rows coexist -- and both write
    # ``offered = false`` with ``version = version + 1`` through a dirty
    # ORM flush carrying no predicate. Under READ COMMITTED the row lock
    # serializes those writes without detecting the conflict: two
    # winners, and a counter that advanced once for two logical updates,
    # after which every ``expected_version`` gate on that row is off by
    # one.
    #
    # ``offered`` is written in exactly two places in the tree (offer to
    # true, this one to false), so it is already a sound CAS token, and
    # it is the right one here: ``optimistic_update`` wants an
    # ``expected_version`` and neither adapter has one to give -- both
    # pass task_id and the actor, nothing else.
    #
    # ``assignee_id`` stays untouched. It is a FK into ``identities``
    # while ``actor_id`` here is a ``users.id``, and today "won" means
    # "a collaborator row exists". Making the claim write the assignee
    # would overwrite the creator's default assignment on every offered
    # task and needs an actor identity the adapters do not pass: a
    # decision of its own, not a side effect of a concurrency fix.
    claimed = (
        await session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.offered.is_(True),
                Task.deleted_at.is_(None),
            )
            .values(offered=False, version=Task.version + 1)
            .returning(Task.version)
        )
    ).first()
    if claimed is None:
        # Lost the race, or the row went away between the pre-read and
        # here. One re-read with the same ``deleted_at`` filter tells
        # which, the way ``optimistic_update`` distinguishes stale from
        # missing: a lost race must never be reported as a task that
        # does not exist, and a soft-delete that landed in between must
        # never be reported as a lost race.
        still_there = (
            await session.execute(
                select(Task.id).where(Task.id == task_id, Task.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if still_there is None:
            raise DomainError(MessageCode.TASK_NOT_FOUND)
        raise DomainError(MessageCode.TASK_ALREADY_CLAIMED)
    # The insert is the record of the award, not the guard.
    session.add(TaskCollaborator(org_id=org_id, task_id=task_id, user_id=actor_id))
    await session.flush()
    # Both adapters serialize the returned instance, and the UPDATE
    # above left parts of it expired: it is ORM-enabled, so session
    # synchronization expires what it wrote, and ``updated_at`` carries
    # an onupdate on top of that.
    #
    # Refreshed WHOLE, and both halves of that are load-bearing.
    # Refreshed rather than assigned, because assigning in Python would
    # leave the instance dirty and the next flush would emit a second,
    # predicate-less UPDATE of the very columns this compare-and-swap
    # exists to protect. Whole rather than a named subset, because an
    # attribute left expired loads lazily, and these are read by the
    # adapters from synchronous code, where a lazy load on an async
    # session raises instead of fetching -- a subset refresh of
    # ``offered`` and ``version`` alone passes every service-level test
    # and 500s on both HTTP claim routes.
    await session.refresh(task)
    await notif_svc.enqueue(
        session,
        org_id=org_id,
        actor_id=actor_id,
        user_id=task.owner_id,
        channel=_HANDOFF_CHANNEL,
        kind="task_claimed",
        title=f"Task claimed: {task.title}"[:300],
        body=f"'{task.title}' was claimed.",
        dedupe_key=f"task_claim:{task.id}:{actor_id}",
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task.id,
        action="claim",
    )
    return task


async def decline_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
) -> Task:
    """Member: decline an offered task. Lightweight (no bid table): an
    audit record + a notification back to the offerer. Does NOT assign
    and does NOT clear ``offered`` (other members may still claim).
    Rejects a non-offered task (TASK_NOT_OFFERED)."""
    await require_role(session, org_id, actor_id, Role.member)
    task = (
        await session.execute(select(Task).where(Task.id == task_id, Task.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if task is None:
        raise DomainError(MessageCode.TASK_NOT_FOUND)
    if not task.offered:
        raise DomainError(MessageCode.TASK_NOT_OFFERED)
    await notif_svc.enqueue(
        session,
        org_id=org_id,
        actor_id=actor_id,
        user_id=task.owner_id,
        channel=_HANDOFF_CHANNEL,
        kind="task_declined",
        title=f"Task declined: {task.title}"[:300],
        body=f"'{task.title}' was declined by a member.",
        dedupe_key=f"task_decline:{task.id}:{actor_id}",
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task.id,
        action="decline",
    )
    return task
