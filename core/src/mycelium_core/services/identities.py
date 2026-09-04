"""Identity service: lookups + lifecycle (docs/adr/0028).

Identity rows are populated lazily by the layers that create the
underlying subject:

- ``ensure_for_user(org, user)`` is called from signup /
  add-member flows (Stage 1 wires it into auth.signup; future
  invitation flows will call it too).
- ``ensure_for_ai_assistant(org, assistant)`` is called from
  ``services.ai_assistants.create``.

Idempotent by ``(org_id, handle)``: a second call with the same
identifier returns the existing row instead of conflicting.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.ai_assistant import AiAssistant, AssistantRuntime
from mycelium_core.models.identity import Identity, IdentityKind
from mycelium_core.models.membership import Membership
from mycelium_core.models.user import User


async def ensure_for_user(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Identity:
    """Create or fetch the identity row for ``(org x user)``.

    Requires the user to have a non-empty handle (Mycelium signup
    enforces it). Idempotent: subsequent calls with the same
    ``(org, user)`` return the existing row.
    """
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise NotFoundError(MessageCode.USER_NOT_FOUND)
    if not user.handle:
        raise DomainError(MessageCode.IDENTITY_HANDLE_REQUIRED, kind="user")

    existing = (
        await session.execute(
            select(Identity).where(
                Identity.org_id == org_id,
                Identity.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    identity = Identity(
        org_id=org_id,
        kind=IdentityKind.user,
        handle=user.handle,
        user_id=user_id,
    )
    session.add(identity)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        # Lost the race against a concurrent insert (same org, same
        # handle): fetch and return the winner.
        return (
            await session.execute(
                select(Identity).where(
                    Identity.org_id == org_id,
                    Identity.user_id == user_id,
                )
            )
        ).scalar_one()
    return identity


async def ensure_for_ai_assistant(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    assistant_id: uuid.UUID,
) -> Identity:
    """Create or fetch the identity row for an ai_assistant.

    Idempotent. Requires a non-empty handle on the assistant row.
    """
    assistant = (
        await session.execute(
            select(AiAssistant).where(
                AiAssistant.id == assistant_id,
                AiAssistant.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if assistant is None:
        raise NotFoundError(MessageCode.AI_ASSISTANT_NOT_FOUND)
    if not assistant.handle:
        raise DomainError(MessageCode.IDENTITY_HANDLE_REQUIRED, kind="ai_assistant")

    existing = (
        await session.execute(
            select(Identity).where(
                Identity.org_id == org_id,
                Identity.ai_assistant_id == assistant_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    identity = Identity(
        org_id=org_id,
        kind=IdentityKind.ai_assistant,
        handle=assistant.handle,
        ai_assistant_id=assistant_id,
    )
    session.add(identity)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return (
            await session.execute(
                select(Identity).where(
                    Identity.org_id == org_id,
                    Identity.ai_assistant_id == assistant_id,
                )
            )
        ).scalar_one()
    return identity


def is_internal_agent(kind: IdentityKind | None, runtime: AssistantRuntime | None) -> bool:
    """Whether a task addressed to this identity may enter the llm pool.

    The one place that answer is computed. Three sites decide it -- the
    scheduler's recompute, the dispatcher's admission, and ``start_run``
    -- and each of them used to spell it ``kind is ai_assistant``, which
    admits an assistant this system cannot start.

    Fails closed on a missing runtime. A NULL comes from an identity
    whose ``ai_assistant_id`` is unset or whose row is gone, which is
    not an assistant that can be driven.

    The join it costs is deliberate. ``models/identity.py`` says ``kind``
    is denormalised "so the scheduler / dispatch can read it without a
    JOIN", and reading ``runtime`` puts one back. That is the price of
    distinguishing two things ``kind`` conflates; denormalising
    ``runtime`` onto ``identities`` too would be a second copy of the
    same fact, drifting from ``ai_assistants`` the way a status column
    there would have drifted from ``is_active``.
    """
    return kind == IdentityKind.ai_assistant and runtime is AssistantRuntime.internal


async def is_internal_agent_identity(session: AsyncSession, *, ident: Identity | None) -> bool:
    """``is_internal_agent`` for a caller holding one ``Identity``.

    The two bulk sites read ``runtime`` as a column of a join they
    already build; ``start_run`` has a single row in hand and would
    otherwise re-derive the predicate inline, which is how the three
    copies came to disagree in the first place.
    """
    if ident is None or ident.kind != IdentityKind.ai_assistant:
        return False
    if ident.ai_assistant_id is None:
        return False
    runtime = (
        await session.execute(
            select(AiAssistant.runtime).where(AiAssistant.id == ident.ai_assistant_id)
        )
    ).scalar_one_or_none()
    return is_internal_agent(ident.kind, runtime)


async def _subject_is_active(session: AsyncSession, *, ident: Identity) -> bool:
    """Whether the principal an identity POINTS AT can still act.

    ``identities`` is pure addressing (ADR-0028): it deliberately has no
    status column of its own, so the answer is read off the subject
    table every time rather than denormalised into a third copy that
    would drift from ``users`` / ``ai_assistants``.

    Fails closed. A missing row, an identity with neither FK set, or a
    NULL flag all count as inactive: ``flag is True`` and not ``flag``,
    so an unreadable subject can never pass for a live one.
    """
    flag: bool | None
    if ident.user_id is not None:
        flag = (
            await session.execute(select(User.is_active).where(User.id == ident.user_id))
        ).scalar_one_or_none()
    elif ident.ai_assistant_id is not None:
        flag = (
            await session.execute(
                select(AiAssistant.is_active).where(AiAssistant.id == ident.ai_assistant_id)
            )
        ).scalar_one_or_none()
    else:
        flag = None
    return flag is True


async def _is_member(session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Whether a user still holds a membership in this org."""
    return (
        await session.execute(
            select(Membership.user_id).where(
                Membership.org_id == org_id, Membership.user_id == user_id
            )
        )
    ).scalar_one_or_none() is not None


async def require_bindable_identity(
    session: AsyncSession, *, org_id: uuid.UUID, ident: Identity
) -> Identity:
    """Return ``ident``, or refuse BY NAME when its principal can no
    longer be bound to NEW work in this org.

    ONE question -- "can this be assigned?" -- with two failure modes,
    deliberately not two helpers: a second predicate would have to be
    remembered at every future call site, and one of them would be
    forgotten. That is exactly how the bare-handle fast path below
    ended up disagreeing with the email branch above it, which has
    always joined ``memberships``.

    - ``identity.inactive``: the subject is deactivated. A user cannot
      log in; an assistant cannot authenticate at all.
    - ``identity.not_member``: a ``user`` identity whose membership was
      removed. The row SURVIVES removal -- ``remove_org_member`` runs one
      ``DELETE FROM memberships`` and nothing else -- and it must keep
      surviving: five FKs into ``identities`` are ON DELETE SET NULL and
      ``task_participants`` is CASCADE, so dropping the row would
      silently un-assign every task the person held (``assignee_id``
      NULL is indistinguishable from "nobody ever picked it up"), erase
      note-link authorship and task creation attribution, and delete
      their appointment participation. Irreversibly: a remove-then-re-add
      mints a fresh identity uuid and the SET NULLs have already fired.
      An address that stops accepting new mail is still a valid return
      address.

    ``DomainError`` (400), not ``NotFoundError`` (404): the caller named
    something real and asked for something it may not do. Answering "no
    such identity" for a handle sitting right there would be a lie the
    caller cannot act on -- the same rule as the archived-note resolver
    and as ``invoice create --client <archived>``.
    """
    if not await _subject_is_active(session, ident=ident):
        raise DomainError(
            MessageCode.IDENTITY_INACTIVE,
            handle=ident.handle,
            kind=str(ident.kind),
        )
    if ident.kind == IdentityKind.user and ident.user_id is not None:
        if not await _is_member(session, org_id=org_id, user_id=ident.user_id):
            raise DomainError(
                MessageCode.IDENTITY_NOT_MEMBER,
                handle=ident.handle,
                kind=str(ident.kind),
            )
    return ident


async def require_assistant_runnable(session: AsyncSession, *, ident: Identity) -> Identity:
    """Refuse to RUN work already addressed to a deactivated assistant.

    Deliberately narrower than ``require_bindable_identity``: this is not
    a new binding, so membership is irrelevant (an assistant has none)
    and the task's existing assignment is not being questioned. The only
    question is whether the named assistant can still act right now.

    Its own message code so the owner is told which assistant is dead
    rather than the generic "not dispatchable".
    """
    if await _subject_is_active(session, ident=ident):
        return ident
    raise DomainError(MessageCode.AGENT_RUN_ASSIGNEE_INACTIVE, handle=ident.handle)


async def identity_is_bindable(
    session: AsyncSession, *, org_id: uuid.UUID, identity_id: uuid.UUID
) -> bool:
    """Non-raising form of ``require_bindable_identity`` for callers that
    branch instead of refusing (the recurrence spawn). False when the
    identity does not exist in this org."""
    ident = (
        await session.execute(
            select(Identity).where(Identity.id == identity_id, Identity.org_id == org_id)
        )
    ).scalar_one_or_none()
    if ident is None:
        return False
    if not await _subject_is_active(session, ident=ident):
        return False
    if ident.kind == IdentityKind.user and ident.user_id is not None:
        return await _is_member(session, org_id=org_id, user_id=ident.user_id)
    return True


async def lookup_by_handle(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    handle: str,
    include_inactive: bool = False,
) -> Identity | None:
    """Resolve a handle to an Identity row in the current org, or None.

    Accepts three forms (DX: task 901f0f9f -- assigning by what the
    user actually knows, not an opaque id space):

      * a bare handle (``angelo``) -- the canonical stored form;
      * a handle with a leading ``@`` (``@angelo``) -- tolerated, the
        ``@`` is stripped before matching;
      * a login email (``angelo@leto.blue``) -- resolved to the org
        member whose ``users.email`` matches (case-insensitively), then
        to their identity. ``users.email`` is globally unique, but we
        still scope the join to org membership so a non-member's email
        never materialises an identity in this org.

    Empty handle returns None defensively (the caller should not pass an
    empty string).

    Refuses a DEACTIVATED principal. This is the resolver every write
    goes through -- PATCH /tasks with an assignee_handle, MCP
    set_task_assignee, add_participant, assign_annotation -- so it is
    the one place that can stop a dead handle from being bound, and
    filtering the picker alone would have left all of them open. The
    refusal is ``identity.inactive``, never a bare miss.

    ``include_inactive=True`` is for READS that ask about someone
    precisely BECAUSE they are gone ("what is still assigned to the
    person we deactivated"): the assigned-annotations inbox, the
    running-timers lookup. It relaxes the refusal, never the
    self-heal below: an identity row is materialised only for a live
    principal, whichever way the flag is set.

    Self-heals legacy/drifted state: ``list_actors`` sources from the
    user/ai_assistant tables directly, but ``identities`` is the
    resolver. Pre-Stage-A signups (and assistants created before the
    handle-mint fix) can land in a state where the source table has a
    handle but the matching identity row is missing or carries a
    different handle (e.g. backfilled by a migration to a UUID
    sentinel and then renamed). When the lookup misses, we fall back
    to the source tables and ``ensure_*`` the identity so the next
    call hits the fast path.
    """
    handle = handle.strip().lstrip("@")
    if not handle:
        return None
    # Email form: resolve via the org member's login email. An ``@``
    # after the leading-strip means the caller passed ``local@domain``.
    if "@" in handle:
        mrow = (
            await session.execute(
                select(User.id, User.is_active)
                .join(Membership, Membership.user_id == User.id)
                .where(
                    Membership.org_id == org_id,
                    func.lower(User.email) == handle.lower(),
                )
            )
        ).first()
        if mrow is not None:
            user_id, is_active = mrow
            if is_active is not True:
                if not include_inactive:
                    raise DomainError(MessageCode.IDENTITY_INACTIVE, handle=handle, kind="user")
                # Never materialise an identity for a dead principal:
                # answer with the row that already exists, or nothing.
                return (
                    await session.execute(
                        select(Identity).where(
                            Identity.org_id == org_id, Identity.user_id == user_id
                        )
                    )
                ).scalar_one_or_none()
            return await ensure_for_user(session, org_id=org_id, user_id=user_id)
        return None
    row = (
        await session.execute(
            select(Identity).where(
                Identity.org_id == org_id,
                Identity.handle == handle,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        if include_inactive:
            return row
        return await require_bindable_identity(session, org_id=org_id, ident=row)
    # Fallback: the picker's source-of-truth (users / ai_assistants)
    # diverged from identities. Re-materialise -- but only for a live
    # principal: a read that opts in must not leave a new identity row
    # behind for someone who was deactivated.
    urow = (
        await session.execute(
            select(User.id, User.is_active)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == org_id, User.handle == handle)
        )
    ).first()
    if urow is not None:
        user_id, is_active = urow
        if is_active is not True:
            if not include_inactive:
                raise DomainError(MessageCode.IDENTITY_INACTIVE, handle=handle, kind="user")
            return None
        return await ensure_for_user(session, org_id=org_id, user_id=user_id)
    arow = (
        await session.execute(
            select(AiAssistant.id, AiAssistant.is_active).where(
                AiAssistant.org_id == org_id,
                AiAssistant.handle == handle,
            )
        )
    ).first()
    if arow is not None:
        assistant_id, is_active = arow
        if is_active is not True:
            if not include_inactive:
                raise DomainError(MessageCode.IDENTITY_INACTIVE, handle=handle, kind="ai_assistant")
            return None
        return await ensure_for_ai_assistant(session, org_id=org_id, assistant_id=assistant_id)
    return None


async def get_identity(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    identity_id: uuid.UUID,
) -> Identity:
    row = (
        await session.execute(
            select(Identity).where(
                Identity.id == identity_id,
                Identity.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            MessageCode.IDENTITY_NOT_FOUND,
            passed=str(identity_id),
            expected="identity id",
            valid_handles=await list_handles(session, org_id=org_id),
        )
    return row


async def require_owner_user(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    """Validate a task OWNER and return it.

    ``owner_id`` is a plain FK to ``users`` (not into ``identities``:
    only a human is accountable for a task), and it was the one
    structural reference on a task with NO validation at all -- not
    membership, not activity. Any uuid satisfying the FK was accepted,
    including a user of a DIFFERENT workspace, and ``_promote_due`` then
    read that stranger's timezone on every subsequent patch. ``users``
    carries no RLS policy, so nothing downstream caught it either.

    Membership is checked before activity so the message is the one the
    caller can act on: "not a member here" is a different problem from
    "deactivated", and reporting the second for a stranger would send
    someone to the wrong admin.
    """
    if not await _is_member(session, org_id=org_id, user_id=user_id):
        raise DomainError(MessageCode.TASK_OWNER_NOT_MEMBER)
    active = (
        await session.execute(select(User.is_active).where(User.id == user_id))
    ).scalar_one_or_none()
    if active is not True:
        raise DomainError(MessageCode.IDENTITY_INACTIVE, handle=str(user_id), kind="user")
    return user_id


async def list_handles(session: AsyncSession, *, org_id: uuid.UUID, limit: int = 50) -> list[str]:
    """The identity handles addressable in this org (capped, sorted).

    Used to enrich ``identity.not_found`` error params (task 2d3abdc3) so
    a caller who fumbled a handle/id sees what is valid instead of an
    opaque miss. It reaches MCP clients verbatim, which is why
    deactivated principals are excluded: a resolver that would refuse a
    handle must not hand an agent that same handle as "valid".

    The ``or_`` spells out ``require_bindable_identity`` in SQL, and the
    two arms must stay in step: a user handle counts only when the user
    is active AND still a member; an assistant handle only when it is
    active. Exactly one FK is set (CHECK constraint), so the other arm
    is NULL and drops out, and a dangling identity with neither subject
    readable fails closed.
    """
    rows = (
        (
            await session.execute(
                select(Identity.handle)
                .outerjoin(User, User.id == Identity.user_id)
                .outerjoin(AiAssistant, AiAssistant.id == Identity.ai_assistant_id)
                .outerjoin(
                    Membership,
                    and_(
                        Membership.user_id == Identity.user_id,
                        Membership.org_id == org_id,
                    ),
                )
                .where(
                    Identity.org_id == org_id,
                    or_(
                        and_(User.is_active.is_(True), Membership.user_id.is_not(None)),
                        AiAssistant.is_active.is_(True),
                    ),
                )
                .order_by(Identity.handle)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def resolve_assignee(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    assignee_id: uuid.UUID,
) -> uuid.UUID:
    """Resolve an assignee reference to an *identity* id.

    Accepts either an identity id (the canonical ``task.assignee_id``
    space, ADR-0028) or a member's *user* id, mapping the latter 1:1 to
    the member's identity. The two id spaces are easy to confuse at the
    tool boundary (``assign_task`` takes a user id, ``set_task_assignee``
    an identity id), so this unifies them for DX (task 2d3abdc3) without
    widening the stored column. A user id is only accepted for an actual
    member of this org (never materialise a non-member's identity).

    Raises ``NotFoundError`` naming what was passed, what was expected,
    and the valid handles in the org; ``DomainError(identity.inactive)``
    when the reference is real but its principal is deactivated. No
    opt-in here: both production callers are WRITES (create_task,
    update_task), so a parameter nobody could legitimately pass would
    be invented API.
    """
    ident = (
        await session.execute(
            select(Identity).where(
                Identity.id == assignee_id,
                Identity.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if ident is not None:
        return (await require_bindable_identity(session, org_id=org_id, ident=ident)).id
    member = (
        await session.execute(
            select(Membership.user_id, User.is_active)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.org_id == org_id,
                Membership.user_id == assignee_id,
            )
        )
    ).first()
    if member is not None:
        user_id, is_active = member
        if is_active is not True:
            raise DomainError(MessageCode.IDENTITY_INACTIVE, handle=str(assignee_id), kind="user")
        return (await ensure_for_user(session, org_id=org_id, user_id=user_id)).id
    raise NotFoundError(
        MessageCode.IDENTITY_NOT_FOUND,
        passed=str(assignee_id),
        expected="identity id or member user id",
        valid_handles=await list_handles(session, org_id=org_id),
    )


async def handle_for_identity(
    session: AsyncSession, *, org_id: uuid.UUID, identity_id: uuid.UUID
) -> str | None:
    """The handle of an identity in this org (None if absent). Read-back
    helper (task 2d3abdc3): the stored ``assignee_id`` is an opaque id."""
    return (
        await session.execute(
            select(Identity.handle).where(
                Identity.id == identity_id,
                Identity.org_id == org_id,
            )
        )
    ).scalar_one_or_none()


async def handle_for_user(session: AsyncSession, *, user_id: uuid.UUID) -> str | None:
    """The login handle of a user (None if absent). Read-back helper for
    ``task.owner_id`` (a user id), task 2d3abdc3."""
    return (
        await session.execute(select(User.handle).where(User.id == user_id))
    ).scalar_one_or_none()
