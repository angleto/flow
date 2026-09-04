"""Agent execution runtime (docs/adr/0025, P3).

On-demand execution of ONE already-dispatched ``llm_agent`` task:
spawn -> work -> artifact -> complete. This is NOT an autonomous loop
and NOT closed-loop auto-dispatch (that is P5); a human (or an
owner-gated API/MCP call) triggers ``start_run`` for a task the P2
scheduler already assigned to an executor.

Governance is by construction, not by convention:

- **Owner-gated.** Running an agent spends credits, so ``start_run`` /
  ``cancel_run`` require the effective role ``owner`` (mirrors
  ``billing.grant_credits`` admin-gating; the sudo-clamped GUC role is
  enforced via ``rbac.require_role``).
- **Bounded.** The work loop stops at ``MAX_STEPS`` and at the assigned
  executor's ``credit_budget`` (status=blocked,
  blocked_reason="budget_exhausted"). Every model call is metered
  through ``billing.meter_if_billable`` with an idempotent
  ``operation_id`` (missing rate card => free, never an error).
- **Killable.** ``cancel_run`` sets a cooperative flag; the loop
  re-reads a fresh ``AgentRun`` each step and stops (status=cancelled).
- **Hard tool allowlist.** The agent may only invoke a small explicit
  set of safe in-process ``mycelium_core`` service calls (read the task,
  read its linked notes, recall memory, append a work note, write a
  memory blob on the seeded ``agent`` channel, set the task state). A
  request for anything else stops the run (status=blocked,
  blocked_reason="tool_not_allowed") -- an HITL stub (a human would
  approve; the full approval UI is P5). There is NO destructive /
  admin / billing / executor / user op, NO arbitrary code/shell, NO
  network.
- **Confined to the human.** Every tool call runs as the actor under
  the same RLS tenant context + ``rbac.require_role`` choke point as a
  human request, so the agent can never exceed the triggering human's
  effective permissions.

Determinism: with a scripted deterministic ``LLMProvider`` (the test
``FakeLLM``, injected via ``set_llm_override`` like the embedder/STT
fakes) a run is fully reproducible -- identical status, steps,
credits_spent and artifact across repeated runs.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core import db as db_ctx
from mycelium_core.ai_providers import LLMProvider, LocalLLM, get_llm
from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_run import AgentRun, AgentRunStatus
from mycelium_core.models.billing import CostBasis
from mycelium_core.models.executor import Executor
from mycelium_core.models.identity import Identity
from mycelium_core.models.membership import Role
from mycelium_core.models.note import Note
from mycelium_core.models.schedule import Schedule
from mycelium_core.models.task import ExecKind, Task
from mycelium_core.services import audit, billing
from mycelium_core.services import coordination as coordination_svc
from mycelium_core.services import identities as identities_svc
from mycelium_core.services import memory as memory_svc
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import task_search as task_search_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.note_effective import effective_note_clause
from mycelium_core.services.rbac import require_role

# Hard step cap: an agent run can never take more than this many model
# calls regardless of the script (bounded by construction).
MAX_STEPS = 12

# The seeded canonical memory channel an agent writes/recalls on.
_AGENT_CHANNEL = "agent"

# Token-overlap based credit unit normalization is irrelevant here: the
# meter uses the LLMResult token counts directly. This module only
# accumulates the debited credits onto the run row.

# The governance core: the EXACT set of tool names the agent may
# invoke. Anything else -> status=blocked, blocked_reason
# "tool_not_allowed" (the HITL stub). Read-only + append-only +
# state-transition only; nothing destructive/admin/billing/executor/
# user, no code/shell, no network.
TOOL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "read_task",  # re-read the task being worked
        "read_task_notes",  # read notes linked to the task
        "recall_memory",  # memory.retrieve on the agent channel
        "search",  # task_search.search_unified (task 4858e818) -- cross-channel
        "append_work_note",  # notes.create_note_for_task (Proposal-A)
        "write_memory",  # memory.write_blob on the agent channel
        "set_task_state",  # tasks.set_state (workflow-validated)
        "finish",  # terminal: produce the artifact and succeed
    }
)


@dataclass(frozen=True)
class _Action:
    """A parsed step decision from the provider output."""

    tool: str
    args: dict[str, object]


def _truncate(text: str, n: int = 500) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _parse_action(text: str) -> _Action:
    """Interpret an ``LLMResult.text`` as the agent's next decision.

    The contract is a small JSON object ``{"tool": <name>, "args":
    {...}}``. Anything that does not parse to that shape is treated as
    a plain final answer (tool ``finish`` with the raw text as the
    output) -- a real model that "just answered" still terminates
    safely instead of being interpreted as an unknown tool."""
    raw = text.strip()
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return _Action(tool="finish", args={"output": raw})
    if not isinstance(obj, dict) or "tool" not in obj:
        return _Action(tool="finish", args={"output": raw})
    tool = obj.get("tool")
    if not isinstance(tool, str):
        return _Action(tool="finish", args={"output": raw})
    args = obj.get("args")
    return _Action(tool=tool, args=args if isinstance(args, dict) else {})


async def _assigned_executor(session: AsyncSession, *, task_id: uuid.UUID) -> Executor | None:
    """The executor the P2 scheduler dispatched this task to, iff the
    Schedule row exists, has an assigned executor and is dispatchable
    (not flagged unassignable)."""
    sched = (
        await session.execute(select(Schedule).where(Schedule.task_id == task_id))
    ).scalar_one_or_none()
    if sched is None or sched.unassignable or sched.assigned_executor_id is None:
        return None
    return (
        await session.execute(select(Executor).where(Executor.id == sched.assigned_executor_id))
    ).scalar_one_or_none()


async def _build_context(
    session: AsyncSession, *, org_id: uuid.UUID, task: Task
) -> list[tuple[str, str]]:
    """Small, deterministic context: the task title/description, the
    titles/bodies of the notes already linked to it, and (docs/adr/0025
    P4 -- the LLM-recipient handoff delivery path) the PENDING incoming
    handoffs for the task: each predecessor's title + the handoff
    message + the artifact note title/body. Kept compact and
    order-stable (notes by created_at,id; handoffs by created_at,id) so
    a scripted provider yields a reproducible run."""
    lines = [f"Task: {task.title}"]
    if task.description:
        lines.append(f"Description: {task.description}")
    # Incoming coordination handoffs (P4): the same artifact+message
    # primitive a human would receive as a notification, injected here
    # for an llm_agent recipient. Deterministic order (created_at, id).
    incoming = await coordination_svc.incoming_for_context(session, task_id=task.id)
    # Phase 6 final: note body comes from note_part(ord=0)+ joined,
    # not the dropped ``transcript`` column.
    from mycelium_core.services.notes import get_body as _get_body

    for ho, pred, note in incoming:
        lines.append(f"Handoff from [{pred.title}]: {ho.message}")
        if note is not None:
            body = (await _get_body(session, note_id=note.id)).strip()[:500]
            lines.append(f"Handoff artifact[{note.title or 'untitled'}]: {body}")
    # docs/adr/0029 P3: notes linked to the task come through the
    # typed link (any kind) instead of the legacy ``Note.task_id``.
    linked_ids = await note_links_svc.notes_for_task(session, org_id=org_id, task_id=task.id)
    linked = (
        list(
            (
                await session.execute(
                    select(Note)
                    .where(Note.id.in_(linked_ids), effective_note_clause())
                    .order_by(Note.created_at, Note.id)
                )
            )
            .scalars()
            .all()
        )
        if linked_ids
        else []
    )
    for n in linked:
        body = (await _get_body(session, note_id=n.id)).strip()
        snippet = body[:500]
        lines.append(f"Note[{n.title or 'untitled'}]: {snippet}")
    return [("user", "\n".join(lines))]


async def _run_tool(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    run: AgentRun,
    task: Task,
    action: _Action,
) -> str | None:
    """Execute one allowlisted tool as the actor (RLS + require_role
    inside each service call). Returns a short observation string for
    the next prompt, or ``None`` for ``finish`` (handled by the caller).

    Pre-condition: ``action.tool`` is in :data:`TOOL_ALLOWLIST` (the
    caller blocks anything else BEFORE side effects)."""
    tool = action.tool
    args = action.args
    if tool == "finish":
        return None
    if tool == "read_task":
        return f"task:{task.title}|state:{task.state_id}"
    if tool == "read_task_notes":
        linked_ids = await note_links_svc.notes_for_task(session, org_id=org_id, task_id=task.id)
        linked = (
            list(
                (
                    await session.execute(
                        select(Note)
                        .where(Note.id.in_(linked_ids), effective_note_clause())
                        .order_by(Note.created_at, Note.id)
                    )
                )
                .scalars()
                .all()
            )
            if linked_ids
            else []
        )
        return f"notes:{len(linked)}"
    if tool == "recall_memory":
        query = str(args.get("query") or task.title)
        raw_limit = args.get("limit")
        limit = int(raw_limit) if isinstance(raw_limit, int | str) else 5
        hits = await memory_svc.retrieve(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=None,
            query=query,
            operation_id=f"agentrun:{run.id}:{run.steps}:recall",
            limit=limit,
            channel_key=_AGENT_CHANNEL,
        )
        return f"recalled:{len(hits)}"
    if tool == "search":
        # Unified search across tasks + ALL memory channels (cross-
        # channel, in contrast to ``recall_memory`` which is scoped to
        # the agent's own channel). Lets the agent pull context from
        # the user's notes / other tasks for the work it's doing.
        # rerank=False (cost cap for internal agents), limit capped at
        # 10 (large result sets burn context for marginal gain).
        query = str(args.get("query") or task.title)
        raw_kinds = args.get("kinds")
        if isinstance(raw_kinds, list) and raw_kinds:
            kinds = [str(k) for k in raw_kinds if str(k) in ("task", "blob", "note")]
        else:
            kinds = ["task", "blob", "note"]
        if not kinds:
            return "search:err:kinds"
        raw_limit = args.get("limit")
        try:
            limit = int(raw_limit) if isinstance(raw_limit, int | str) else 10
        except (ValueError, TypeError):
            limit = 10
        limit = max(1, min(10, limit))
        search_hits = await task_search_svc.search_unified(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=None,
            query=query,
            kinds=kinds,
            tag_ids=[],
            channel_keys=[],
            limit=limit,
            include_archived=False,
            include_deleted=False,
            operation_id=f"agentrun:{run.id}:{run.steps}:search",
        )
        return f"search:{len(search_hits)}"
    if tool == "append_work_note":
        note = await notes_svc.create_note_for_task(
            session,
            org_id=org_id,
            actor_id=actor_id,
            task_id=task.id,
            title=str(args["title"]) if args.get("title") else None,
            text=str(args.get("text") or ""),
        )
        # Record the artifact note id (the last appended note is the
        # work artifact; a later explicit ``finish`` output supersedes
        # the text but the linked note stays the artifact).
        run.artifact_note_id = note.id
        await session.flush()
        return f"note:{note.id}"
    if tool == "write_memory":
        blob = await memory_svc.write_blob(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=None,
            text_body=str(args.get("text") or ""),
            operation_id=f"agentrun:{run.id}:{run.steps}:mem",
            namespace="agent",
            sources=[("task", str(task.id))],
            channel_key=_AGENT_CHANNEL,
        )
        return f"mem:{blob.id}"
    if tool == "set_task_state":
        state_id = uuid.UUID(str(args["state_id"]))
        await tasks_svc.set_state(
            session,
            org_id=org_id,
            actor_id=actor_id,
            task_id=task.id,
            expected_version=task.version,
            state_id=state_id,
        )
        # Refresh the in-memory task so a later tool sees the new
        # version/state (set_state bumped version optimistically).
        await session.refresh(task)
        return f"state:{state_id}"
    # Unreachable: the caller validates against TOOL_ALLOWLIST first.
    raise DomainError(MessageCode.DOMAIN_ERROR)


def _model_id(executor: Executor) -> str:
    return executor.model_id or "agent"


async def _finalize_artifact(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    run: AgentRun,
    task: Task,
    output: str,
) -> None:
    """Ensure the run has a Proposal-A work-note artifact. If the agent
    already appended one (``artifact_note_id`` set) keep it; otherwise
    create the final answer as the artifact note linked to the task. A
    memory blob on the ``agent`` channel mirrors the artifact so it is
    recallable by later runs."""
    if run.artifact_note_id is None:
        note = await notes_svc.create_note_for_task(
            session,
            org_id=org_id,
            actor_id=actor_id,
            task_id=task.id,
            title=f"Agent result: {task.title}"[:120],
            text=output,
        )
        run.artifact_note_id = note.id
        await session.flush()
    if output.strip():
        await memory_svc.write_blob(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=None,
            text_body=output,
            operation_id=f"agentrun:{run.id}:final:mem",
            namespace="agent",
            sources=[("task", str(task.id))],
            channel_key=_AGENT_CHANNEL,
        )


async def _audit_run(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, run: AgentRun
) -> None:
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="agent_run",
        entity_id=run.id,
        action="run",
        diff={
            "status": run.status.value,
            "steps": str(run.steps),
            "credits": str(run.credits_spent),
        },
    )


async def start_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    provider: LLMProvider | None = None,
) -> AgentRun:
    """Spawn and drive ONE agent run for an already-dispatched
    ``llm_agent`` task, returning the FINAL ``AgentRun``.

    Owner-gated (running an agent spends credits). Validates the task
    is an in-org ``llm_agent`` task with a dispatchable Schedule
    assignment, rejects a concurrent active run, then runs the bounded
    governed loop and persists the terminal state."""
    await require_role(session, org_id, actor_id, Role.owner)
    task = (
        await session.execute(select(Task).where(Task.id == task_id, Task.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(MessageCode.AGENT_RUN_NOT_FOUND)
    # docs/adr/0028: a task is dispatchable to an agent run iff its
    # assignee identity is an ai_assistant this system can run, OR
    # (unassigned) the ``executor_kind`` hint is ``llm_agent`` (a llm
    # task that has not been bound to a specific assistant yet).
    #
    # Checked here and not only in the dispatch loop, because two of the
    # three callers never go through it: MCP ``agent_run_start`` and
    # POST /tasks/{task_id}/run reach this function directly. And the
    # executor guard below does not stand in for it -- ``_assigned_executor``
    # reads back whatever the scheduler assigned, so it finds a row.
    if task.assignee_id is not None:
        identity = (
            await session.execute(select(Identity).where(Identity.id == task.assignee_id))
        ).scalar_one_or_none()
        is_llm = await identities_svc.is_internal_agent_identity(session, ident=identity)
        # Live re-check on top of the scheduler snapshot, the same
        # defense-in-depth the dispatch loop applies to WIP: the schedule
        # row that flags ``assignee_inactive`` is only as fresh as the
        # last recompute, and POST /agent-runs reads whatever it left.
        # Its own code, not the generic not_dispatchable, because "it must
        # be an llm_agent task with an assigned, dispatchable executor"
        # tells an owner nothing about WHICH assistant is dead.
        if is_llm and identity is not None:
            await identities_svc.require_assistant_runnable(session, ident=identity)
    else:
        is_llm = task.executor_kind is ExecKind.llm_agent
    if not is_llm:
        raise DomainError(MessageCode.AGENT_RUN_NOT_DISPATCHABLE)
    executor = await _assigned_executor(session, task_id=task_id)
    if executor is None:
        raise DomainError(MessageCode.AGENT_RUN_NOT_DISPATCHABLE)

    active = (
        await session.execute(
            select(AgentRun).where(
                AgentRun.task_id == task_id,
                AgentRun.status.in_([AgentRunStatus.queued, AgentRunStatus.running]),
            )
        )
    ).scalar_one_or_none()
    if active is not None:
        raise DomainError(MessageCode.AGENT_RUN_ALREADY_ACTIVE)

    # Resolve the provider HERE, above ``session.add(run)``, because a
    # run row is what makes the damage permanent. ``_drive`` never
    # raises for a provider error: it writes ``failed`` and returns, so
    # ``_dispatch_one`` sees a normal return and marks the request
    # ``dispatched``, and the run row it leaves behind excludes the task
    # from every later tick. Refusing before the insert leaves nothing
    # behind and gives the caller a reason instead of a failed run.
    #
    # ``LocalLLM`` is the reference stub: its ``complete`` raises
    # unconditionally, so this is a certain first-step failure and not a
    # guess. Resolution itself cannot fail -- ``get_llm`` falls back to
    # the stub and ``llm_resolver`` degrades to it by written contract --
    # so a guard written against a resolution error would be dead code.
    # A live probe would answer the same question by spending a real
    # completion on every dispatch.
    #
    # The resolved provider is then handed to ``_drive``, so what was
    # checked and what runs cannot be two different objects.
    resolved = provider or get_llm()
    if isinstance(resolved, LocalLLM):
        raise DomainError(MessageCode.AGENT_RUN_NO_PROVIDER)

    now = dt.datetime.now(tz=dt.UTC)
    run = AgentRun(
        org_id=org_id,
        task_id=task_id,
        executor_id=executor.id,
        status=AgentRunStatus.running,
        steps=0,
        credits_spent=Decimal(0),
        started_at=now,
    )
    session.add(run)
    await session.flush()

    # From this point on, every audit row produced inside ``_drive``,
    # the post-drive bookkeeping, and the run-level audit must attribute
    # the change to the agent run as actor, not to the human caller
    # who triggered the dispatch. ``with_actor`` shifts the GUC for
    # the rest of this transaction and restores it on exit.
    async with db_ctx.with_actor(session, actor_kind="agent_run", actor_subject_id=str(run.id)):
        await _drive(
            session,
            org_id=org_id,
            actor_id=actor_id,
            run=run,
            task=task,
            executor=executor,
            provider=resolved,
        )
        # The run has begun and its context was built from the task's
        # PENDING incoming handoffs (the P4 LLM-recipient delivery
        # path): mark them consumed so a later run does not re-inject
        # them (idempotent -- a second start finds none). Non-fatal: a
        # bookkeeping failure here is logged (audit boundary, like
        # coordination.on_task_completed) and must not undo a
        # finished run.
        try:
            await coordination_svc.mark_incoming_consumed(
                session, org_id=org_id, actor_id=actor_id, task_id=task_id
            )
        except Exception as exc:  # coordination boundary
            await audit.log(
                session,
                org_id=org_id,
                actor_id=actor_id,
                entity="task_handoff",
                entity_id=task_id,
                action="consume_failed",
                diff={"error": str(exc)[:200]},
            )
        await _audit_run(session, org_id=org_id, actor_id=actor_id, run=run)
    return run


async def _drive(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    run: AgentRun,
    task: Task,
    executor: Executor,
    provider: LLMProvider,
) -> None:
    """The bounded, governed work loop. Mutates ``run`` to a terminal
    state (succeeded|failed|cancelled|blocked). Never raises for a
    provider error (captured as status=failed) or a guardrail stop."""
    budget: Decimal | None = executor.credit_budget
    history: list[tuple[str, str]] = await _build_context(session, org_id=org_id, task=task)
    system = (
        "You are a Mycelium work agent executing a single assigned task. "
        'Respond with a JSON object {"tool": name, "args": {...}}. '
        f'Allowed tools: {sorted(TOOL_ALLOWLIST)}. Use "finish" with '
        '{"output": "..."} when the work is complete.'
    )
    last_output = ""

    for _ in range(MAX_STEPS):
        # Re-load a fresh row so a concurrent cancel_run is observed
        # (cooperative kill switch).
        await session.refresh(run)
        if run.cancel_requested:
            run.status = AgentRunStatus.cancelled
            run.ended_at = dt.datetime.now(tz=dt.UTC)
            await session.flush()
            return

        # Budget guard BEFORE the next paid call: stop cleanly if the
        # assigned executor's credit budget is already exhausted.
        if budget is not None and run.credits_spent >= budget:
            run.status = AgentRunStatus.blocked
            run.blocked_reason = "budget_exhausted"
            run.ended_at = dt.datetime.now(tz=dt.UTC)
            await session.flush()
            return

        try:
            result = await provider.complete(system=system, messages=history)
        except Exception as exc:  # pragma: no cover - real-provider failure
            run.status = AgentRunStatus.failed
            run.error = _truncate(str(exc))
            run.ended_at = dt.datetime.now(tz=dt.UTC)
            await session.flush()
            return

        # Meter the model call (idempotent per step). Missing rate card
        # => free (meter_if_billable), still recorded if configured.
        rec = await billing.meter_if_billable(
            session,
            org_id=org_id,
            actor_id=actor_id,
            operation_id=f"agentrun:{run.id}:{run.steps}",
            op="agent_step",
            model_id=_model_id(executor),
            units_in=Decimal(result.tokens_in),
            units_out=Decimal(result.tokens_out),
            basis=CostBasis.local,
        )
        if rec is not None:
            run.credits_spent = run.credits_spent + rec.credits
        run.steps = run.steps + 1
        await session.flush()

        action = _parse_action(result.text)

        if action.tool not in TOOL_ALLOWLIST:
            # HITL stub: a tool outside the hard allowlist would need
            # human approval (full approval UI is P5). Stop with NO
            # side effect.
            run.status = AgentRunStatus.blocked
            run.blocked_reason = "tool_not_allowed"
            run.ended_at = dt.datetime.now(tz=dt.UTC)
            await session.flush()
            return

        if action.tool == "finish":
            last_output = str(action.args.get("output") or last_output)
            break

        try:
            obs = await _run_tool(
                session,
                org_id=org_id,
                actor_id=actor_id,
                run=run,
                task=task,
                action=action,
            )
        except DomainError as exc:
            # An allowlisted tool that failed its own validation (bad
            # args, illegal workflow transition, ...). Not a crash:
            # treat as a guarded stop so the run is inspectable.
            run.status = AgentRunStatus.blocked
            run.blocked_reason = "tool_not_allowed"
            run.error = _truncate(f"{action.tool}: {exc}")
            run.ended_at = dt.datetime.now(tz=dt.UTC)
            await session.flush()
            return
        history = [*history, ("assistant", result.text), ("user", obs or "")]
    else:
        # Step cap reached without an explicit finish: still produce the
        # artifact from whatever the last output was (bounded success).
        last_output = last_output or f"Agent reached the step limit on: {task.title}"

    await _finalize_artifact(
        session,
        org_id=org_id,
        actor_id=actor_id,
        run=run,
        task=task,
        output=last_output,
    )
    run.status = AgentRunStatus.succeeded
    run.ended_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()


async def cancel_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    run_id: uuid.UUID,
) -> AgentRun:
    """Request cancellation of a run (owner-gated). Sets the cooperative
    ``cancel_requested`` flag the loop observes. Idempotent on an
    already-cancelling run; a terminal run -> AGENT_RUN_TERMINAL."""
    await require_role(session, org_id, actor_id, Role.owner)
    run = (
        await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError(MessageCode.AGENT_RUN_NOT_FOUND)
    if run.status in (
        AgentRunStatus.succeeded,
        AgentRunStatus.failed,
        AgentRunStatus.cancelled,
        AgentRunStatus.blocked,
    ):
        raise DomainError(MessageCode.AGENT_RUN_TERMINAL)
    run.cancel_requested = True
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="agent_run",
        entity_id=run.id,
        action="cancel",
    )
    return run


async def get_run(session: AsyncSession, *, org_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun:
    """Read one run (member-level, RLS-scoped). Foreign/unknown id ->
    AGENT_RUN_NOT_FOUND (cross-org isolation preserved)."""
    run = (
        await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError(MessageCode.AGENT_RUN_NOT_FOUND)
    return run


async def list_runs(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
) -> Sequence[AgentRun]:
    """List runs in the workspace (RLS-scoped), newest first, optionally
    filtered to one task."""
    stmt = select(AgentRun)
    if task_id is not None:
        stmt = stmt.where(AgentRun.task_id == task_id)
    stmt = stmt.order_by(AgentRun.created_at.desc(), AgentRun.id)
    return list((await session.execute(stmt)).scalars().all())
