"""Task service: CRUD, tags/assignees, comments, workflow state
transitions (FR-6). RBAC, optimistic concurrency, i18n, audit
(docs/adr/0002, docs/adr/0017).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from mycelium_core.concurrency import optimistic_update
from mycelium_core.config import get_settings
from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.annotation import Annotation
from mycelium_core.models.classification_job import ClassificationJob
from mycelium_core.models.identity import Identity, IdentityKind
from mycelium_core.models.index_scope import IndexScope
from mycelium_core.models.membership import Role
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.models.task import ExecKind, Necessity, Task
from mycelium_core.models.task_collaborator import TaskCollaborator
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.models.user import User
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import annotations as _annotations
from mycelium_core.services import audit, lifecycle, tag_assignment, text_patch
from mycelium_core.services import entity_revisions as _revisions
from mycelium_core.services import identities as identities_svc
from mycelium_core.services import task_search as _task_search
from mycelium_core.services import workflow as wf
from mycelium_core.services.rbac import require_role
from mycelium_core.timewindow import end_of_day, resolve_tz


async def _promote_due(
    session: AsyncSession,
    value: dt.date | dt.datetime | None,
    *,
    owner_id: uuid.UUID,
) -> dt.datetime | None:
    """Resolve a due-date input to a stored timestamptz. A date-only due
    (a plain ``date`` == "due that day, no specific time") is promoted to
    end-of-day in the OWNER's configured timezone -- the single place
    that decides the time-of-day for "no time specified", so the SPA, the
    MCP and the HTTP API all agree and a date-only deadline expires at
    the end of the owner's calendar day. A real ``datetime`` is an
    explicit instant and is stored unchanged."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    name = (
        await session.execute(select(User.timezone).where(User.id == owner_id))
    ).scalar_one_or_none()
    return end_of_day(value, resolve_tz(name))


def derive_priority(importance: int, urgency: int) -> int:
    """Eisenhower: importance and urgency are 1..5 where 1 is the most
    pressing (1 = Critical / Now, 5 = Trivial / Whenever). priority =
    importance*urgency, so it runs 1 (most prioritary: Critical+Now)
    .. 25 (least: Trivial+Whenever). 1 is highest and the default task
    / scheduler ordering is ascending (ADR-0004), so the smallest
    number is always done first. importance/urgency are persisted so
    the matrix round-trips."""
    return max(1, min(25, importance * urgency))


_UPDATABLE = frozenset(
    {
        "title",
        "description",
        # ``priority`` is intentionally absent: since migration 0102
        # every task carries Eisenhower axes (Low/Low by default), and
        # the service derives ``priority`` from importance x urgency.
        # Letting callers patch ``priority`` directly would re-introduce
        # the duplication this enforces against.
        "importance",
        "urgency",
        "start_date",
        "due_date",
        "estimate_effort_h",
        "assignee_id",
        "owner_id",
        "required_capabilities",
        "parent_task_id",
        "monetary_cost",
        "location",
        "necessity",
        "budget_id",
        "billable",
        # The write path for the indexing opt-out has to be this one:
        # ``update_task`` marks the task dirty afterwards, and the
        # ``after_update`` mapper listener does not fire for the Core
        # UPDATE that ``optimistic_update`` issues.
        "index_scope",
        # Appointment unification (migration 0094, ADR-0008 addendum).
        # ``start_at`` + ``duration_minutes`` are paired (CHECK
        # constraint); ``recurrence`` is independent.
        "start_at",
        "duration_minutes",
        "recurrence",
    }
)


def _validate_event_pairing(start_at: Any, duration_minutes: Any) -> None:
    """Enforce the ``(start_at, duration_minutes)`` pairing before the
    DB CHECK does (so the API returns 422 with a clean message rather
    than a generic IntegrityError). Either both set or both NULL."""
    if (start_at is None) != (duration_minutes is None):
        raise DomainError(MessageCode.DOMAIN_ERROR)


async def _tag_kind(session: AsyncSession, tag_id: uuid.UUID) -> TagKind:
    """The tag's kind, or TAG_NOT_FOUND. RLS hides another workspace's
    tags, so a foreign id simply reads as missing -- the same existence
    check the tag endpoints have always made, folded into the lookup
    the structural dispatch (attach_tag / detach_tag) needs anyway."""
    kind = (await session.execute(select(Tag.kind).where(Tag.id == tag_id))).scalar_one_or_none()
    if kind is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    return kind


async def get_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    include_deleted: bool = False,
) -> Task:
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None or (task.deleted_at is not None and not include_deleted):
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    return task


async def _log_task_revision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    version_from: int,
    version_to: int,
    changed_fields: list[str],
    channel: str,
    edit_session_id: str | None,
    restored_from: uuid.UUID | None = None,
) -> None:
    """Persist a recovery-history entry for a task mutation.

    Reads the task back so the snapshot reflects the post-update
    state (the Core UPDATE in ``optimistic_update`` bypasses the ORM
    mapper, so any in-memory copy is stale). ``include_deleted=True``
    is required for the soft-delete path: the row exists, just with
    ``deleted_at`` set.

    ``restored_from`` chains a restore revision back to the source
    revision so the timeline shows ``restored from #abcd1234`` next
    to the new sealed row.
    """
    fresh = await get_task(session, org_id=org_id, task_id=task_id, include_deleted=True)
    snapshot = await _revisions.snapshot_task(session, fresh)
    await _revisions.append(
        session,
        org_id=org_id,
        entity_kind=_revisions.ENTITY_KIND_TASK,
        entity_id=task_id,
        actor_id=actor_id,
        snapshot=snapshot,
        changed_fields=changed_fields,
        channel=channel,
        version_from=version_from,
        version_to=version_to,
        edit_session_id=edit_session_id,
        restored_from=restored_from,
    )


async def create_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    title: str,
    description: str | None = None,
    # Eisenhower axes drive ``priority``. Low/Low (4/4) is the default
    # since migration 0102 --- ``priority`` is never settable by the
    # caller, the service derives it through ``derive_priority``.
    importance: int = 4,
    urgency: int = 4,
    start_date: dt.date | None = None,
    # A date-only due (a plain ``date``) is promoted to end-of-day in the
    # owner's configured timezone by ``_promote_due`` below; a real
    # ``datetime`` is an explicit instant stored as-is. Migration 0005:
    # due_date is a TIMESTAMPTZ.
    due_date: dt.date | dt.datetime | None = None,
    billable: bool | None = None,
    parent_task_id: uuid.UUID | None = None,
    # docs/adr/0028: pass either ``assignee_id`` (uuid into identities)
    # or ``assignee_handle`` (we resolve it to the identity in this
    # org). The two are mutually exclusive; if both are passed,
    # ``assignee_id`` wins. ``executor_kind`` is still accepted as a
    # convenience for callers that previously routed by kind without
    # picking a specific assignee (it sets the human/agent intent
    # before any identity exists); resolution happens via Identity.
    assignee_id: uuid.UUID | None = None,
    assignee_handle: str | None = None,
    executor_kind: ExecKind = ExecKind.human,
    owner_id: uuid.UUID | None = None,
    estimate_effort_h: Decimal | None = None,
    required_capabilities: Sequence[str] | None = None,
    monetary_cost: Decimal | None = None,
    location: str | None = None,
    necessity: Necessity = Necessity.should,
    index_scope: IndexScope = IndexScope.org,
    budget_id: uuid.UUID | None = None,
    tag_ids: Sequence[uuid.UUID] = (),
    assignee_ids: Sequence[uuid.UUID] = (),
    # docs/adr/0028 + migration 0091: identity (user | ai_assistant)
    # that actually created the task. When None we default to the
    # ``user`` identity of ``actor_id``; the MCP layer overrides this
    # with the ai_assistant identity when the principal is an agent
    # token, so AI-created tasks are identifiable.
    created_by_identity_id: uuid.UUID | None = None,
    # migration 0093: the agent_token behind the call when the
    # principal is an mcp_token. Set unconditionally by the MCP layer
    # for the HTTP transport (regardless of whether the token has an
    # ``assistant_id`` bound), so a bare token still surfaces AI
    # authorship through ``agent_tokens.name`` in the serializer.
    created_by_token_id: uuid.UUID | None = None,
    # Appointment unification (migration 0094, ADR-0008 addendum).
    # ``start_at`` + ``duration_minutes`` together promote the task to
    # a calendar appointment subject to no-overlap on assignee.
    start_at: dt.datetime | None = None,
    duration_minutes: int | None = None,
    recurrence: dict[str, Any] | None = None,
    # Recovery history: the channel this create came in through, plus
    # an optional editing-session id when the SPA wants the first
    # snapshot to be attributable to the same session as the upcoming
    # edits. Default ``"api"`` for callers that don't know.
    channel: str = "api",
    edit_session_id: str | None = None,
) -> Task:
    _validate_event_pairing(start_at, duration_minutes)
    await require_role(session, org_id, actor_id, Role.member)
    if parent_task_id is not None:
        await get_task(session, org_id=org_id, task_id=parent_task_id)
    # Structural tags are resolved by the choke-point (docs/adr/0003,
    # services/tag_assignment): it picks THE project out of the
    # requested bag and derives the client from it, so the task carries
    # the client tag explicitly (every per-client filter / report /
    # focus stays a flat ``WHERE tag_id = <client>`` instead of joining
    # task_tags -> project_profile) without this service deciding what
    # "the" project is. ``entity="task"`` never resolves to a NULL
    # project -- no orphan tasks: it falls back to the default
    # "General" project under "Personal" -- so the workflow lookup
    # below always has a project to key on. Resolving BEFORE the insert
    # also means an unknown tag id is rejected without leaving a task
    # row behind.
    structural = await tag_assignment.resolve_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        requested=tag_ids,
    )
    workflow = await wf.resolve_effective_workflow(session, org_id, structural.project_tag_id)
    initial = await wf.get_initial_state(session, workflow.id)
    priority = derive_priority(importance, urgency)
    # docs/adr/0028 Stage C: resolve assignee through identities.
    # Either an explicit ``assignee_id`` or a handle to look up.
    if assignee_id is None and assignee_handle:
        identity = await identities_svc.lookup_by_handle(
            session, org_id=org_id, handle=assignee_handle
        )
        if identity is None:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        assignee_id = identity.id
    elif assignee_id is not None:
        # Resolve to an identity id, accepting either an identity id or a
        # member's user id (task 2d3abdc3), and validate org membership.
        assignee_id = await identities_svc.resolve_assignee(
            session, org_id=org_id, assignee_id=assignee_id
        )
    # ``owner_id`` defaults to the creator. Same rule as the
    # migration backfill: every task has an explicit human owner.
    #
    # Validated when stated: it is a bare FK to ``users``, so without
    # this an owner from another workspace is accepted and every later
    # patch reads that stranger's timezone. Not validated when it falls
    # back to ``actor_id``, which require_role already established as a
    # member two lines up.
    effective_owner = owner_id or actor_id
    if owner_id is not None:
        await identities_svc.require_owner_user(session, org_id=org_id, user_id=owner_id)
    # A date-only due is anchored to end-of-day in the owner's timezone.
    due_date = await _promote_due(session, due_date, owner_id=effective_owner)
    # Default ``created_by_identity_id`` to the user identity of the
    # actor. Callers (notably the MCP server) can override this with
    # the ai_assistant identity when the request came in through an
    # agent token.
    if created_by_identity_id is None:
        user_ident = (
            await session.execute(
                select(Identity.id).where(
                    Identity.org_id == org_id,
                    Identity.user_id == actor_id,
                    Identity.kind == IdentityKind.user,
                )
            )
        ).scalar_one_or_none()
        created_by_identity_id = user_ident
    # Default assignee = creator. When the caller leaves both
    # ``assignee_id`` and ``assignee_handle`` unset, the task is
    # auto-assigned to whoever created it: the user identity for
    # human-driven calls, the ai_assistant identity for MCP/agent
    # calls (the MCP layer resolves ``created_by_identity_id`` to the
    # assistant). Callers that want an unassigned task (e.g. to feed
    # the autonomous-dispatch queue from ADR-0025) must pass an
    # explicit value distinct from the creator.
    if assignee_id is None and created_by_identity_id is not None:
        assignee_id = created_by_identity_id
        # Align ``executor_kind`` with the resolved identity so the
        # persisted routing hint stays coherent with the assignee. An
        # assistant that runs OUTSIDE this system gets ``human``: the
        # hint answers "which pool routes this if nobody is assigned",
        # and nothing here can start such an assistant.
        #
        # This line closes nothing on its own. ``assignee_id`` is set
        # two lines above and is never NULL on this path, so the hint
        # is not read for these tasks at all (ADR-0028: when an
        # assignee is set the kind comes from the joined identity).
        # The trap is closed at the three sites that read the identity.
        creator = (
            await session.execute(select(Identity).where(Identity.id == created_by_identity_id))
        ).scalar_one_or_none()
        creator_kind = creator.kind if creator is not None else None
        if creator_kind == IdentityKind.ai_assistant:
            if await identities_svc.is_internal_agent_identity(session, ident=creator):
                executor_kind = ExecKind.llm_agent
            else:
                executor_kind = ExecKind.human
        elif creator_kind == IdentityKind.user:
            executor_kind = ExecKind.human
    # docs/adr/0028: ``executor_kind`` is the routing hint used only
    # when ``assignee_id`` is NULL; when an assignee is set the kind
    # is derived from the joined identity. Persisted regardless so
    # the unassigned-fallback path keeps working.
    task = Task(
        org_id=org_id,
        title=title,
        description=description,
        priority=priority,
        importance=importance,
        urgency=urgency,
        start_date=start_date,
        due_date=due_date,
        billable=billable,
        state_id=initial.id,
        parent_task_id=parent_task_id,
        owner_id=effective_owner,
        assignee_id=assignee_id,
        executor_kind=executor_kind,
        estimate_effort_h=estimate_effort_h,
        required_capabilities=list(required_capabilities or []),
        monetary_cost=monetary_cost,
        location=location,
        necessity=necessity,
        index_scope=index_scope,
        budget_id=budget_id,
        created_by_identity_id=created_by_identity_id,
        created_by_token_id=created_by_token_id,
        start_at=start_at,
        duration_minutes=duration_minutes,
        recurrence=recurrence,
    )
    session.add(task)
    try:
        # Savepoint so the outer transaction survives an overlap
        # rejection (the test / caller can keep using the session).
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        # Detach the half-inserted task if the savepoint rollback did
        # not already evict it. Some SQLAlchemy versions leave the
        # Python instance attached and that trips later flushes.
        if task in session:
            session.expunge(task)
        # The GiST EXCLUDE on task_participants (migration 0096) rejects
        # an appointment that would put any identity in two overlapping
        # windows. Migration 0094 originally lived on tasks
        # (no_overlap_event_tasks_per_assignee); both names are matched
        # so a manual rollback to 0095 still surfaces a clean 409.
        if "no_overlap_event_tasks_per_assignee" in str(
            exc.orig
        ) or "no_overlap_task_participants" in str(exc.orig):
            raise ConflictError(MessageCode.EVENT_OVERLAP) from exc
        raise
    # The client/project pair is written by the choke-point (which also
    # logs the attach audit); the free-form facets go in one by one so a
    # tag repeated in ``tag_ids`` stays idempotent instead of raising an
    # unhandled IntegrityError on the junction primary key.
    await tag_assignment.set_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task.id,
        structural=structural,
        on_create=True,
    )
    for tag_id in structural.generic_ids:
        await tag_assignment.attach_generic(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="task",
            entity_id=task.id,
            tag_id=tag_id,
            on_create=True,
        )
    for user_id in assignee_ids:
        session.add(TaskCollaborator(org_id=org_id, task_id=task.id, user_id=user_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task.id,
        action="create",
    )
    # Recovery-history baseline: a sealed revision at the moment of
    # creation. Lets the timeline show the task's starting point and
    # gives a non-empty restore target for the very first edit.
    await _log_task_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task.id,
        version_from=task.version,
        version_to=task.version,
        changed_fields=["_create"],
        channel=channel,
        edit_session_id=edit_session_id,
    )
    # On-create auto-classify (ADR-0042 D5): same-transaction enqueue, drained
    # by the garden worker. Gated off by default; for TASKS it also requires
    # the unified-task-graph flag (classify_node accepts a task only then).
    _cfg = get_settings()
    if _cfg.garden_autoclassify_on_creation_enabled and _cfg.garden_unified_task_graph_enabled:
        session.add(ClassificationJob(org_id=org_id, node_kind="task", node_id=task.id))
    return task


# order_by whitelist (task 39e98a30): a strict name->ORM-column map so the
# sort key can never be raw-string-interpolated. ``necessity`` sorts by
# MoSCoW rank (must < should < could), not the alphabetical enum value.
_TASK_ORDER: dict[str, Any] = {
    "priority": Task.priority,
    "due_date": Task.due_date,
    "start_date": Task.start_date,
    "created_at": Task.created_at,
    "updated_at": Task.updated_at,
    "necessity": case(
        (Task.necessity == Necessity.must, 0),
        (Task.necessity == Necessity.should, 1),
        else_=2,
    ),
}


def _apply_task_filters(
    stmt: Select[Any],
    *,
    ids: Sequence[uuid.UUID] | None,
    state_id: uuid.UUID | None,
    tag_id: uuid.UUID | None,
    assignee_id: uuid.UUID | None,
    assignee_kind: IdentityKind | None,
    assignee_handles: Sequence[str] | None,
    owner_handles: Sequence[str] | None,
    parent_task_id: uuid.UUID | None,
    include_archived: bool,
    include_deleted: bool,
    open_only: bool,
    q: str | None,
    due_from: dt.datetime | None,
    due_to: dt.datetime | None,
    start_from: dt.date | None,
    start_to: dt.date | None,
    updated_since: dt.datetime | None,
) -> Select[Any]:
    """Apply the shared WHERE/JOIN predicates that ``list_tasks`` and
    ``count_tasks`` agree on: one filter vocabulary, two projections (the
    row set vs its count). All joins are anchored on ``Task`` so the same
    clauses compose onto ``select(Task)`` or ``select(count(distinct(id)))``.
    """
    if not include_deleted:
        stmt = stmt.where(Task.deleted_at.is_(None))
    if not include_archived:
        stmt = stmt.where(Task.is_archived.is_(False))
    if open_only:
        # 'open' == the task's workflow state is non-terminal. Resolved via
        # WorkflowState.is_terminal (RLS-scoped to the org), so the caller
        # never has to look up a non-terminal state uuid first. ``state_id``
        # is NOT NULL, so the IN-subquery is exact.
        stmt = stmt.where(
            Task.state_id.in_(select(WorkflowState.id).where(WorkflowState.is_terminal.is_(False)))
        )
    if ids is not None:
        # An explicit set of rows, for a caller that already knows which
        # ones it wants: hydrating N search hits costs one query instead
        # of N. An EMPTY sequence means "these zero rows" and returns
        # nothing -- distinct from None, which means "no id filter". A
        # caller that folds the two together turns a filtered-to-nothing
        # page into the whole workspace.
        stmt = stmt.where(Task.id.in_(list(ids)))
    if state_id is not None:
        stmt = stmt.where(Task.state_id == state_id)
    if parent_task_id is not None:
        stmt = stmt.where(Task.parent_task_id == parent_task_id)
    if tag_id is not None:
        stmt = stmt.join(TaskTag, TaskTag.task_id == Task.id).where(TaskTag.tag_id == tag_id)
    if assignee_id is not None:
        stmt = stmt.join(TaskCollaborator, TaskCollaborator.task_id == Task.id).where(
            TaskCollaborator.user_id == assignee_id
        )
    # docs/adr/0028: identity-axis filters on Task.assignee_id (FK to
    # identities) and Task.owner_id (FK to users). ``assignee_kind``
    # narrows the assignee polymorphism (human vs llm_agent); the
    # ``*_handles`` lists are multi-select. NULL assignee never matches
    # an identity filter (unassigned tasks are excluded from those
    # facets by design).
    if assignee_kind is not None or assignee_handles:
        stmt = stmt.join(Identity, Identity.id == Task.assignee_id)
        if assignee_kind is not None:
            stmt = stmt.where(Identity.kind == assignee_kind)
        if assignee_handles:
            stmt = stmt.where(Identity.handle.in_(list(assignee_handles)))
    if owner_handles:
        stmt = stmt.join(User, User.id == Task.owner_id).where(User.handle.in_(list(owner_handles)))
    if q is not None:
        # Free-text filter mirroring notes.list_notes: each whitespace term
        # must match the task title, its description, any checklist item
        # text, or a tag name (terms ANDed, fields ORed, case-insensitive).
        # Applied server-side over the whole RLS-scoped set BEFORE any
        # caller-side row cap, so it is not limited to the newest rows the
        # way a client-side filter would be. Sub-SELECTs (not joins) keep it
        # composable with the tag_id/assignee joins above without row fan-out.
        from mycelium_core.models.task_checklist_item import TaskChecklistItem

        for term in (w for w in q.split() if w.strip()):
            like = f"%{term}%"
            checklist_tasks = select(TaskChecklistItem.task_id).where(
                TaskChecklistItem.text.ilike(like)
            )
            tag_tasks = (
                select(TaskTag.task_id)
                .join(Tag, Tag.id == TaskTag.tag_id)
                .where(Tag.name.ilike(like))
            )
            stmt = stmt.where(
                or_(
                    Task.title.ilike(like),
                    Task.description.ilike(like),
                    Task.id.in_(checklist_tasks),
                    Task.id.in_(tag_tasks),
                )
            )
    # Date-window predicates (task 39e98a30). ``due_*`` are absolute instants
    # on the timestamptz ``due_date`` (the adapter expands an owner/caller-tz
    # day to bounds); ``start_*`` compare the plain Date ``start_date``. All
    # half-open ([from, to)) so adjacent windows don't double-count.
    if due_from is not None:
        stmt = stmt.where(Task.due_date >= due_from)
    if due_to is not None:
        stmt = stmt.where(Task.due_date < due_to)
    if start_from is not None:
        stmt = stmt.where(Task.start_date >= start_from)
    if start_to is not None:
        stmt = stmt.where(Task.start_date < start_to)
    if updated_since is not None:
        stmt = stmt.where(Task.updated_at >= updated_since)
    return stmt


async def list_tasks(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    ids: Sequence[uuid.UUID] | None = None,
    state_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    assignee_kind: IdentityKind | None = None,
    assignee_handles: Sequence[str] | None = None,
    owner_handles: Sequence[str] | None = None,
    parent_task_id: uuid.UUID | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
    open_only: bool = False,
    with_description: bool = True,
    q: str | None = None,
    due_from: dt.datetime | None = None,
    due_to: dt.datetime | None = None,
    start_from: dt.date | None = None,
    start_to: dt.date | None = None,
    updated_since: dt.datetime | None = None,
    order_by: str | None = None,
    order_desc: bool = False,
    limit: int | None = None,
    after: tuple[int, dt.datetime, uuid.UUID] | None = None,
) -> list[Task]:
    stmt: Select[Any] = select(Task)
    # The list view does not need the (potentially large) description: the
    # SPA free-text search is server-side and the body is edited on the
    # detail page. Defer it so listing hundreds of tasks doesn't transfer
    # every body. Callers that read ``description`` keep the default.
    if not with_description:
        stmt = stmt.options(defer(Task.description))
    stmt = _apply_task_filters(
        stmt,
        ids=ids,
        state_id=state_id,
        tag_id=tag_id,
        assignee_id=assignee_id,
        assignee_kind=assignee_kind,
        assignee_handles=assignee_handles,
        owner_handles=owner_handles,
        parent_task_id=parent_task_id,
        include_archived=include_archived,
        include_deleted=include_deleted,
        open_only=open_only,
        q=q,
        due_from=due_from,
        due_to=due_to,
        start_from=start_from,
        start_to=start_to,
        updated_since=updated_since,
    )
    # Order: a whitelisted key (NULLs always last so unset dates never lead),
    # created_at as a stable tiebreak; else the default priority-first order.
    order_col = _TASK_ORDER.get(order_by) if order_by else None
    if order_col is not None:
        # Custom order: the whitelisted column (NULLs last), with id as a
        # unique final tiebreak for a TOTAL order. Keyset cursoring (``after``)
        # is offered only for the default order below -- the MCP layer never
        # emits a cursor for a custom order -- so it is ignored here.
        primary = (order_col.desc() if order_desc else order_col.asc()).nulls_last()
        stmt = stmt.order_by(primary, Task.created_at.desc(), Task.id.asc())
    else:
        # Default total order: priority asc (1 = top), created_at desc, id asc.
        # All three columns are NOT NULL, so the keyset predicate is exact (no
        # NULL-ordering hazard) -- pagination has no dupes/gaps (task c20c6351).
        if after is not None:
            ap, ac, ai = after
            stmt = stmt.where(
                or_(
                    Task.priority > ap,
                    and_(Task.priority == ap, Task.created_at < ac),
                    and_(Task.priority == ap, Task.created_at == ac, Task.id > ai),
                )
            )
        stmt = stmt.order_by(Task.priority.asc(), Task.created_at.desc(), Task.id.asc())
    # Push the row cap into SQL (task c20c6351): the list view used to
    # materialize the whole RLS-scoped table and slice in Python.
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().unique().all())


async def count_tasks(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    ids: Sequence[uuid.UUID] | None = None,
    state_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    assignee_kind: IdentityKind | None = None,
    assignee_handles: Sequence[str] | None = None,
    owner_handles: Sequence[str] | None = None,
    parent_task_id: uuid.UUID | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
    open_only: bool = False,
    q: str | None = None,
    due_from: dt.datetime | None = None,
    due_to: dt.datetime | None = None,
    start_from: dt.date | None = None,
    start_to: dt.date | None = None,
    updated_since: dt.datetime | None = None,
) -> int:
    """Count tasks matching the same filter vocabulary as ``list_tasks`` with
    one ``COUNT`` query (O(index)), so 'how many open tasks' never fetches the
    whole RLS-scoped table to ``len()`` it. ``distinct(Task.id)`` keeps the
    tag/assignee joins from inflating the count."""
    stmt: Select[Any] = select(func.count(func.distinct(Task.id))).select_from(Task)
    stmt = _apply_task_filters(
        stmt,
        ids=ids,
        state_id=state_id,
        tag_id=tag_id,
        assignee_id=assignee_id,
        assignee_kind=assignee_kind,
        assignee_handles=assignee_handles,
        owner_handles=owner_handles,
        parent_task_id=parent_task_id,
        include_archived=include_archived,
        include_deleted=include_deleted,
        open_only=open_only,
        q=q,
        due_from=due_from,
        due_to=due_to,
        start_from=start_from,
        start_to=start_to,
        updated_since=updated_since,
    )
    return int((await session.execute(stmt)).scalar_one())


async def tags_by_task(
    session: AsyncSession, *, task_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Tag]]:
    """Batched task -> tags (for showing tag chips in the task list
    without an N+1)."""
    out: dict[uuid.UUID, list[Tag]] = {}
    if not task_ids:
        return out
    rows = await session.execute(
        select(TaskTag.task_id, Tag)
        .join(Tag, Tag.id == TaskTag.tag_id)
        .where(TaskTag.task_id.in_(task_ids))
    )
    for tid, tag in rows.all():
        out.setdefault(tid, []).append(tag)
    return out


async def list_collaborators(
    session: AsyncSession, *, org_id: uuid.UUID, task_id: uuid.UUID
) -> list[dict[str, str]]:
    """A task's collaborators (ADR-0028: people involved beyond the
    assignee) as ``{user_id, handle}`` rows, sorted by handle. Read-back
    helper for MCP get_task (task 2d3abdc3): the M:N table stores user
    ids, so resolve to handles to confirm what was set."""
    rows = (
        await session.execute(
            select(TaskCollaborator.user_id, User.handle)
            .join(User, User.id == TaskCollaborator.user_id)
            .where(
                TaskCollaborator.org_id == org_id,
                TaskCollaborator.task_id == task_id,
            )
            .order_by(User.handle)
        )
    ).all()
    return [{"user_id": str(uid), "handle": handle} for uid, handle in rows]


async def collaborator_counts(
    session: AsyncSession, *, org_id: uuid.UUID, task_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Collaborator count per task id (one GROUP BY, no N+1). Feeds the
    lean list_tasks row (task 2d3abdc3) without per-row handle lookups."""
    if not task_ids:
        return {}
    rows = (
        await session.execute(
            select(TaskCollaborator.task_id, func.count())
            .where(
                TaskCollaborator.org_id == org_id,
                TaskCollaborator.task_id.in_(list(task_ids)),
            )
            .group_by(TaskCollaborator.task_id)
        )
    ).all()
    return {tid: int(n) for tid, n in rows}


async def apply_patch_to_description(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    patch: str,
    base_sha256: str,
    channel: str = "api",
    edit_session_id: str | None = None,
) -> int:
    """Apply a strict unified diff to a task's ``description`` and persist
    via :func:`update_task`. Symmetric to
    :func:`mycelium_core.services.note_parts.apply_patch_to_part`: sha256 base
    gate (409 PATCH_STALE on drift) plus the version gate in
    ``update_task``'s ``optimistic_update``, all in one transaction."""
    task = await get_task(session, org_id=org_id, task_id=task_id)
    new_body = text_patch.apply_patch_text(
        task.description or "",
        patch,
        expected_sha256=base_sha256,
        max_result_bytes=get_settings().note_body_max_bytes,
    )
    return await update_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"description": new_body},
        channel=channel,
        edit_session_id=edit_session_id,
    )


async def update_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
    # Recovery history (channel-aware coalescing). When ``channel='web'``
    # and ``edit_session_id`` is set, consecutive PATCH calls that share
    # the session id coalesce into a single open revision; otherwise a
    # sealed revision is appended per call.
    channel: str = "api",
    edit_session_id: str | None = None,
    restored_from: uuid.UUID | None = None,
) -> int:
    # ``assignee_handle`` is a convenience input that we resolve to
    # ``assignee_id`` below; it is not a column itself but we tolerate
    # it here so callers don't have to do the lookup themselves.
    unknown = set(values) - _UPDATABLE - {"assignee_handle"}
    if unknown:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    current = await get_task(session, org_id=org_id, task_id=task_id)
    if values.get("owner_id") is not None:
        await identities_svc.require_owner_user(session, org_id=org_id, user_id=values["owner_id"])
    elif "owner_id" in values:
        # Explicit null. A task always has a human accountable for it
        # (the column is NOT NULL); clearing it is not a legal edit.
        raise DomainError(MessageCode.TASK_OWNER_NOT_MEMBER)
    if "due_date" in values:
        # Same date-only -> end-of-day-in-owner's-tz promotion as create
        # (the owner is the task's existing owner).
        values["due_date"] = await _promote_due(
            session, values["due_date"], owner_id=current.owner_id
        )
    if "importance" in values or "urgency" in values:
        # importance/urgency are NOT NULL since migration 0102, so the
        # service can re-derive ``priority`` unconditionally from the
        # patched axes (or the row's current value when one is left
        # untouched). ``priority`` is never patched directly --- see
        # _UPDATABLE.
        imp = values.get("importance", current.importance)
        urg = values.get("urgency", current.urgency)
        values["priority"] = derive_priority(imp, urg)
    # docs/adr/0028 Stage C: a caller can either pass ``assignee_id``
    # directly (uuid into identities) or ``assignee_handle`` (a
    # convenience we resolve here). Both are merged onto the single
    # authoritative ``assignee_id`` column.
    if "assignee_handle" in values:
        handle = values.pop("assignee_handle")
        if handle:
            identity = await identities_svc.lookup_by_handle(session, org_id=org_id, handle=handle)
            if identity is None:
                raise NotFoundError(
                    MessageCode.IDENTITY_NOT_FOUND,
                    passed=handle,
                    expected="handle, @handle, or member login email",
                    valid_handles=await identities_svc.list_handles(session, org_id=org_id),
                )
            values["assignee_id"] = identity.id
        else:
            values["assignee_id"] = None
    if "assignee_id" in values and values["assignee_id"] is not None:
        # Resolve to an identity id, accepting either an identity id or a
        # member's user id (task 2d3abdc3); validates org membership and
        # raises an informative not-found otherwise.
        values["assignee_id"] = await identities_svc.resolve_assignee(
            session, org_id=org_id, assignee_id=values["assignee_id"]
        )
    # Pre-validate the event pairing using current + patched values, so
    # callers can patch one of the two as long as the other is already
    # set on the row. (Both NULL after patch = revert to plain task;
    # both non-NULL after patch = appointment; mixed = 422.)
    if "start_at" in values or "duration_minutes" in values:
        eff_start = values.get("start_at", current.start_at)
        eff_dur = values.get("duration_minutes", current.duration_minutes)
        _validate_event_pairing(eff_start, eff_dur)
    try:
        async with session.begin_nested():
            new_version = await optimistic_update(
                session,
                Task,
                pk=task_id,
                expected_version=expected_version,
                values=values,
            )
    except IntegrityError as exc:
        if "no_overlap_event_tasks_per_assignee" in str(
            exc.orig
        ) or "no_overlap_task_participants" in str(exc.orig):
            raise ConflictError(MessageCode.EVENT_OVERLAP) from exc
        raise
    # Core UPDATE bypasses the mapper-level event listeners that the
    # task-search resync would otherwise pick up; mark the task dirty
    # so the pre-commit flush re-indexes the blob.
    _task_search.mark_task_dirty(session, task_id)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    await _log_task_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=list(values.keys()),
        channel=channel,
        edit_session_id=edit_session_id,
        restored_from=restored_from,
    )
    return new_version


async def append_to_description(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    text: str,
    separator: str = "\n\n",
    expected_version: int | None = None,
    dedupe_if_tail_matches: bool = False,
    channel: str = "api",
) -> tuple[int, int]:
    """Append ``text`` to ``task.description`` without reading the body
    first (task 4ac39ecf). Mirror of ``notes.append_to_note_field``;
    returns ``(new_version, appended_chars)``.

    ``expected_version=None`` -> use the just-loaded version (append on
    current state; concurrent writers surface as stale_version).
    ``dedupe_if_tail_matches=True`` -> no-op when the body already ends
    with ``text``. ``BODY_LIMIT_EXCEEDED`` when the resulting body
    would exceed ``settings.note_body_max_bytes`` (the same cap as
    notes; description is the symmetric long-form field).
    """
    from mycelium_core.config import get_settings as _get_settings
    from mycelium_core.services.notes import _collapsed_concat as _concat

    task = await get_task(session, org_id=org_id, task_id=task_id)
    current = task.description or ""
    if dedupe_if_tail_matches and current and current.rstrip().endswith(text.rstrip()):
        return task.version, 0
    new_value = _concat(current, separator, text)
    max_bytes = _get_settings().note_body_max_bytes
    if len(new_value.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    eff_version = expected_version if expected_version is not None else task.version
    values: dict[str, Any] = {"description": new_value}
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=eff_version,
        values=values,
    )
    _task_search.mark_task_dirty(session, task_id)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="description.append",
        diff={"description_appended_chars": str(len(text))},
    )
    await _log_task_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        version_from=eff_version,
        version_to=new_version,
        changed_fields=["description"],
        channel=channel,
        edit_session_id=None,
        restored_from=None,
    )
    return new_version, len(text)


async def prepend_to_description(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    text: str,
    separator: str = "\n\n",
    expected_version: int | None = None,
    dedupe_if_head_matches: bool = False,
    channel: str = "api",
) -> tuple[int, int]:
    """Prepend ``text`` to the FRONT of ``task.description`` without
    reading the body first (task 5662a07f; mirror of
    ``append_to_description``). Returns ``(new_version, prepended_chars)``.

    ``expected_version=None`` -> prepend onto the current version.
    ``dedupe_if_head_matches=True`` -> no-op when the body already starts
    with ``text``. ``BODY_LIMIT_EXCEEDED`` past the body cap.
    """
    from mycelium_core.config import get_settings as _get_settings
    from mycelium_core.services.notes import _collapsed_concat as _concat

    task = await get_task(session, org_id=org_id, task_id=task_id)
    current = task.description or ""
    if dedupe_if_head_matches and current and current.lstrip().startswith(text.lstrip()):
        return task.version, 0
    # Swap the concat order vs append: text goes BEFORE the current body.
    new_value = _concat(text, separator, current)
    max_bytes = _get_settings().note_body_max_bytes
    if len(new_value.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    eff_version = expected_version if expected_version is not None else task.version
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=eff_version,
        values={"description": new_value},
    )
    _task_search.mark_task_dirty(session, task_id)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="description.prepend",
        diff={"description_prepended_chars": str(len(text))},
    )
    await _log_task_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        version_from=eff_version,
        version_to=new_version,
        changed_fields=["description"],
        channel=channel,
        edit_session_id=None,
        restored_from=None,
    )
    return new_version, len(text)


def _coerce_task_restore_values(payload: dict[str, Any]) -> dict[str, Any]:
    """JSON snapshot values come back as strings (Decimal, date,
    datetime, UUID) per ``_json_safe`` in entity_revisions; coerce
    them back to Python types the service layer / SQLAlchemy expect.
    Unknown keys are filtered upstream by ``restorable_payload``.
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            out[key] = None
            continue
        if key in {"importance", "urgency", "duration_minutes"}:
            out[key] = int(value)
        elif key == "start_date":
            out[key] = dt.date.fromisoformat(value) if isinstance(value, str) else value
        elif key in {"due_date", "start_at"}:
            out[key] = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
        elif key in {"estimate_effort_h", "monetary_cost"}:
            out[key] = Decimal(value) if not isinstance(value, Decimal) else value
        elif key in {"parent_task_id", "budget_id"}:
            out[key] = uuid.UUID(value) if isinstance(value, str) else value
        elif key == "necessity":
            out[key] = Necessity(value) if not isinstance(value, Necessity) else value
        elif key == "required_capabilities":
            out[key] = list(value)
        else:
            out[key] = value
    return out


async def replace_in_description(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    find: str,
    replace: str,
    expected_version: int | None = None,
    count: int = 0,
    channel: str = "api",
) -> tuple[int, int]:
    """Anchored find/replace inside ``task.description`` without
    resending it. The task-description twin of
    ``note_parts.replace_in_part`` and ``annotations.replace_in_body``:
    the description was the one markdown document of the three that could
    be appended and prepended to but never amended in place.

    ``count`` <= 0 replaces every occurrence, a positive ``count`` only
    the first N. Returns ``(new_version, replacements)``. A no-op --
    ``find`` empty or absent -- returns ``(current_version, 0)`` WITHOUT
    bumping the version and without asserting ``expected_version``
    (nothing changed, so nothing to race). ``expected_version=None``
    replaces on the current version, matching the blind-write contract of
    the append/prepend twins. ``BODY_LIMIT_EXCEEDED`` past the body cap.
    """
    from mycelium_core.config import get_settings as _get_settings

    await require_role(session, org_id, actor_id, Role.member)
    task = await get_task(session, org_id=org_id, task_id=task_id)
    body = task.description or ""
    occurrences = body.count(find) if find else 0
    if occurrences == 0:
        return task.version, 0
    n = occurrences if count <= 0 else min(count, occurrences)
    new_body = body.replace(find, replace) if count <= 0 else body.replace(find, replace, count)
    max_bytes = _get_settings().note_body_max_bytes
    if len(new_body.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    eff_version = expected_version if expected_version is not None else task.version
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=eff_version,
        values={"description": new_body},
    )
    _task_search.mark_task_dirty(session, task_id)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="description.replace",
        diff={"replacements": str(n)},
    )
    await _log_task_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        version_from=eff_version,
        version_to=new_version,
        changed_fields=["description"],
        channel=channel,
        edit_session_id=None,
        restored_from=None,
    )
    return new_version, n


async def restore_revision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    revision_id: uuid.UUID,
    expected_version: int,
    fields: Sequence[str] | None = None,
) -> int:
    """Revert a task's restorable fields to the snapshot stored in
    ``revision_id``. Produces a NEW sealed revision on the
    ``restore`` channel with ``restored_from = revision_id``; the
    source revision is never mutated.

    ``fields=None`` restores every field allowed by
    ``restorable_payload``; a non-empty ``fields`` narrows the
    subset and rejects non-restorable names (DomainError) so a
    caller can't sneak owner/state through.
    """
    revision = await _revisions.get_revision(
        session,
        revision_id=revision_id,
        entity_kind=_revisions.ENTITY_KIND_TASK,
        entity_id=task_id,
    )
    payload = _revisions.restorable_payload(revision, fields=fields)
    values = _coerce_task_restore_values(payload)
    if not values:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    return await update_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values=values,
        channel="restore",
        edit_session_id=None,
        restored_from=revision_id,
    )


_SCHEDULE_FIELDS = frozenset(
    {
        "schedule_mode",
        "constraint_kind",
        "constraint_date",
        "remaining_effort_h",
        "actual_start",
        "is_milestone",
    }
)


async def set_schedule_fields(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> int:
    """Write-back of scheduler pins/constraints (FR-4, docs/adr/0004).
    The next recompute reads these; manual/constraint survive it."""
    if not values:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    unknown = set(values) - _SCHEDULE_FIELDS
    if unknown:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=expected_version,
        values=values,
    )
    # Scheduler write-back doesn't touch ``title``/``description``/
    # checklist, so the resync either short-circuits on the unchanged
    # content_hash or, on a task at ``index_scope='none'``, repeats an
    # already-idempotent delete; no embed is paid on either branch. The
    # mark is still required so the listener path doesn't go stale on a
    # future combined update.
    _task_search.mark_task_dirty(session, task_id)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="set_schedule",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


async def set_state(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    state_id: uuid.UUID,
) -> int:
    await require_role(session, org_id, actor_id, Role.member)
    task = await get_task(session, org_id=org_id, task_id=task_id)
    workflow = await wf.effective_workflow_for_task(session, org_id, task_id)
    if not await wf.state_in_workflow(session, workflow.id, state_id):
        raise DomainError(MessageCode.TRANSITION_NOT_ALLOWED)
    old_state_id = task.state_id
    if old_state_id != state_id:
        await wf.assert_transition(session, workflow.id, old_state_id, state_id)
    # Terminal detection reuses the SAME notion the scheduler uses
    # (``WorkflowState.is_terminal``), never a hardcoded state name.
    term_rows: dict[uuid.UUID, bool] = {
        sid: is_term
        for sid, is_term in (
            await session.execute(
                select(WorkflowState.id, WorkflowState.is_terminal).where(
                    WorkflowState.id.in_({old_state_id, state_id})
                )
            )
        ).all()
    }
    was_terminal = bool(term_rows.get(old_state_id, False))
    now_terminal = bool(term_rows.get(state_id, False))
    new_version = await optimistic_update(
        session,
        Task,
        pk=task_id,
        expected_version=expected_version,
        values={"state_id": state_id},
    )
    _task_search.mark_task_dirty(session, task_id)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="set_state",
    )
    await _log_task_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=["state_id"],
        channel="system",
        edit_session_id=None,
    )
    # Coordination handoff fan-out (docs/adr/0025, P4): fire ONLY when
    # the transition crosses INTO a terminal state from a non-terminal
    # one (re-entering the same terminal state is a no-op -- idempotent
    # by construction). ADDITIVE + NON-FATAL: a coordination failure is
    # swallowed inside the hook; the state transition above is the
    # source of truth and is never rolled back by it. Imported lazily
    # to avoid a tasks<->notifications<->coordination import cycle.
    if now_terminal and not was_terminal:
        from mycelium_core.services import coordination as _coord
        from mycelium_core.services import recurrence as _rec

        await session.refresh(task)
        await _coord.on_task_completed(session, org_id=org_id, actor_id=actor_id, task=task)
        # Recurrence spawn (migration 0094 ``tasks.recurrence``): if the
        # just-completed task carries a recurrence spec, materialise the
        # next occurrence as a fresh row in the initial state with the
        # window shifted forward. No-op when ``recurrence`` is NULL or
        # the chain has ended (past ``until``).
        await _rec.maybe_spawn_next(session, org_id=org_id, actor_id=actor_id, task=task)
    return new_version


async def _set(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
    action: str,
) -> int:
    # Validate existence (include deleted: restore needs to see the
    # soft-deleted row). The actual flag flip + audit is shared with
    # notes via lifecycle.transition.
    await get_task(session, org_id=org_id, task_id=task_id, include_deleted=True)
    new_version = await lifecycle.transition(
        session,
        model_cls=Task,
        org_id=org_id,
        actor_id=actor_id,
        entity_id=task_id,
        expected_version=expected_version,
        values=values,
        audit_entity="task",
        audit_action=action,
    )
    # archive / soft_delete / restore -- lifecycle.transition issues a
    # Core UPDATE so the mapper listener won't fire. Mark dirty: the
    # resync re-reads the task and, for soft_delete (deleted_at set),
    # the loader returns None which triggers cleanup of pointer + blob.
    _task_search.mark_task_dirty(session, task_id)
    # Discrete lifecycle revisions land on the ``system`` channel:
    # they're not free-text edits and don't coalesce with the SPA's
    # editing session, but the timeline should still show them.
    await _log_task_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=[f"_{action}", *values.keys()],
        channel="system",
        edit_session_id=None,
    )
    return new_version


async def archive_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
    archived: bool = True,
) -> int:
    return await _set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"is_archived": archived},
        action="archive",
    )


async def soft_delete_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
) -> int:
    return await _set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"deleted_at": dt.datetime.now(tz=dt.UTC)},
        action="soft_delete",
    )


async def restore_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    expected_version: int,
) -> int:
    return await _set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"deleted_at": None},
        action="restore",
    )


async def attach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    kind = await _tag_kind(session, tag_id)
    # Dispatch by kind; the choke-point owns the write AND its audit row
    # (docs/adr/0003, services/tag_assignment), so nothing is logged
    # here a second time.
    if kind is TagKind.project:
        # Attaching a project is a MOVE, not an error: the client
        # follows the project, so the pair is swapped atomically instead
        # of this service expanding project -> client by hand.
        await tag_assignment.move_to_project(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="task",
            entity_id=task_id,
            project_tag_id=tag_id,
        )
    elif kind is TagKind.client:
        # A client that contradicts the attached project is refused
        # there (TAG_CLIENT_PROJECT_MISMATCH): the project is the truth.
        await tag_assignment.set_client(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="task",
            entity_id=task_id,
            client_tag_id=tag_id,
        )
    else:
        await tag_assignment.attach_generic(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="task",
            entity_id=task_id,
            tag_id=tag_id,
        )


async def detach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    kind = await _tag_kind(session, tag_id)
    if kind in (TagKind.client, TagKind.project):
        # A task is exactly-one client AND exactly-one project
        # (docs/adr/0003), so there is no legal state to detach INTO:
        # re-pointing the task is a MOVE, i.e. attaching the wanted
        # project (which drags its client along).
        raise DomainError(MessageCode.TAG_STRUCTURAL_REQUIRED)
    # Free-form facet: the choke-point deletes it and logs the audit.
    await tag_assignment.detach_generic(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        tag_id=tag_id,
    )


async def assign(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    try:
        async with session.begin_nested():
            session.add(TaskCollaborator(org_id=org_id, task_id=task_id, user_id=user_id))
            await session.flush()
    except IntegrityError:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="assign",
    )


async def unassign(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(TaskCollaborator).where(
            TaskCollaborator.task_id == task_id,
            TaskCollaborator.user_id == user_id,
        )
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="unassign",
    )


async def add_comment(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    body: str,
    author_identity_id: uuid.UUID | None = None,
) -> Annotation:
    """A task comment is an annotation on the task description
    (``doc_kind='task_description'``, no anchor); a task's chronological
    general comments are its work diary. Delegates to the annotation
    service so tasks and notes share one model. ``author_identity_id``
    lets the MCP layer record an ai_assistant author."""
    return await _annotations.create_comment(
        session,
        org_id=org_id,
        actor_id=actor_id,
        doc_kind="task_description",
        doc_id=task_id,
        body=body,
        author_identity_id=author_identity_id,
    )


async def list_comments(
    session: AsyncSession, *, org_id: uuid.UUID, task_id: uuid.UUID
) -> list[Annotation]:
    return await _annotations.list_for_doc(
        session, org_id=org_id, doc_kind="task_description", doc_id=task_id
    )
