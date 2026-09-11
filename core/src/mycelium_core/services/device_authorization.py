"""The device authorization grant, for our own surfaces.

Four calls, and the order between them is the whole design:

``open_request``    a device with no session asks. It gets a short code to
                    SHOW and a long code to KEEP, and nothing else exists
                    yet: no credential, no user, no workspace.
``find_open``       an authenticated person looks a request up by the
                    short code, to see what is asking before deciding.
``approve`` /       that person answers. Approval records WHO and WHERE.
``deny``            It does not mint anything.
``redeem``          the device, still asking, collects. The credential is
                    created HERE, inside the transaction that marks the
                    request spent.

**Why nothing is minted at approval.** The mechanism this replaces minted
a credential and then tried to hand it over; every failed handover left a
live credential that nobody held and nobody knew to revoke, and the page
could only ask the reader to go and revoke it by hand. Moving the mint to
the collection makes that state unrepresentable rather than rare: there is
no window in which an uncollected secret exists, so there is no secret to
orphan and none at rest between the two halves.

**What the person is delegating.** Not a session: a credential scoped to
the fixed, narrow list the caller passes, acting as that person in the
workspaces they belong to. The surface asking for it cannot do anything
its holder could not already do, which is what lets a plain member approve
one for themselves.

What this does NOT protect against, stated because SEC asks for the limit
and not only for the control:

- A person who approves without comparing the code. The short code exists
  to be compared and the screen says so; nothing can compel the reading.
  What it does defeat is a page that opens a request of its own and sends
  somebody to approve it, because the code on that screen will not be the
  code on their device.
- Anything already executing as the person on their own machine. It can
  open a request and approve it, the same way it could use the session
  directly.
- A second factor is INHERITED, never added: approval happens inside a
  session that already satisfied whatever login required, so this flow
  does not need to know whether MFA exists.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core import security_events
from mycelium_core.db import as_tenant
from mycelium_core.errors import AuthError, NotFoundError, QuotaExceededError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_token import WorkspaceBinding
from mycelium_core.models.device_authorization import DeviceAuthorization
from mycelium_core.services import ai_assistants

# No character that can be read as another one: I/1, L, O/0, U/V are all
# out. What is left is 30 symbols, and the code a person compares is 8 of
# them -- about 6.6e11 combinations, which is not a secret and does not
# need to be: the short code collects nothing on its own.
_ALPHABET = "ABCDEFGHJKMNPQRSTWXYZ23456789"
_USER_CODE_LEN = 8
_GROUP = 4

# Long enough that somebody can be interrupted, log in, deal with a second
# factor and come back; short enough that a request nobody answered does
# not stay open all afternoon. The device says how long is left and opens
# a fresh one rather than extending this.
DEFAULT_REQUEST_TTL_SECONDS = 600

# How long the credential a collection produces lives. Deliberately below
# ``agent_tokens.DEFAULT_TTL_DAYS`` (365, the floor for a forgotten
# machine-to-machine secret): this one sits in a browser profile on a
# machine that may be shared, and renewing it is the same two clicks that
# created it.
DEFAULT_CREDENTIAL_TTL_DAYS = 90

# What the device is told to wait between questions. Advisory: the server
# does not enforce it, and a client that asks faster gets the same answer.
POLL_INTERVAL_SECONDS = 3


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _mint_user_code() -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_USER_CODE_LEN))
    # Stored in the shape it is displayed in. One representation, so a
    # lookup never has to guess whether the separator was typed.
    return "-".join(body[i : i + _GROUP] for i in range(0, _USER_CODE_LEN, _GROUP))


@dataclass(frozen=True)
class OpenedRequest:
    """What the device is given. ``device_code`` is returned exactly
    once and only its digest is stored."""

    device_code: str
    user_code: str
    expires_at: datetime.datetime
    interval: int


@dataclass(frozen=True)
class RedeemedCredential:
    """What a collection produced. ``secret`` leaves once, over TLS."""

    secret: str
    assistant_id: uuid.UUID
    org_id: uuid.UUID
    scope: list[str]
    expires_at: datetime.datetime | None


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


async def open_request(
    session: AsyncSession,
    *,
    client: str,
    origin_ip: str | None = None,
    ttl_seconds: int = DEFAULT_REQUEST_TTL_SECONDS,
) -> OpenedRequest:
    """Open a request. Unauthenticated by design: the caller is a device
    that has nothing yet, and everything it receives here is useless
    until a person approves."""
    now = _now()
    device_code = secrets.token_urlsafe(32)
    # The partial unique index makes a collision with another OPEN
    # request a constraint violation rather than a silent overwrite. At
    # 30^8 and a ten-minute window this is a formality, and a formality
    # that fails loudly is the right kind.
    row = DeviceAuthorization(
        device_code_hash=_hash(device_code),
        user_code=_mint_user_code(),
        client=client,
        expires_at=now + datetime.timedelta(seconds=ttl_seconds),
        origin_ip=origin_ip,
    )
    session.add(row)
    await session.flush()
    return OpenedRequest(
        device_code=device_code,
        user_code=row.user_code,
        expires_at=row.expires_at,
        interval=POLL_INTERVAL_SECONDS,
    )


async def find_open(session: AsyncSession, *, user_code: str) -> DeviceAuthorization:
    """The request behind a short code, for the screen that asks a person
    to approve it.

    Case and separator are normalised, because this value is typed or
    read off another screen and neither of those preserves them. Anything
    that is not an open, unexpired request is reported as absent: a
    person deciding whether to approve has no use for the difference
    between "never existed", "already answered" and "timed out", and the
    distinctions are only useful to somebody sweeping the space."""
    normalised = _normalise_user_code(user_code)
    # OPENNESS IS PART OF THE QUERY, not a check after it. A short code
    # is released for re-issue the moment the request holding it is
    # answered (that is what the partial unique index permits), so a
    # finished row and a live one can legitimately share one code, and a
    # lookup that matched on the code alone would find two rows and fail
    # rather than find the live one. Expiry is here too, where the index
    # cannot express it.
    row = (
        await session.execute(
            select(DeviceAuthorization).where(
                DeviceAuthorization.user_code == normalised,
                DeviceAuthorization.approved_at.is_(None),
                DeviceAuthorization.denied_at.is_(None),
                DeviceAuthorization.redeemed_at.is_(None),
                DeviceAuthorization.expires_at > _now(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.AUTH_DEVICE_CODE_INVALID)
    return row


def _normalise_user_code(value: str) -> str:
    stripped = "".join(ch for ch in value.upper() if ch in _ALPHABET)
    if len(stripped) != _USER_CODE_LEN:
        # Not an error worth distinguishing: it cannot match anything.
        return value.strip().upper()
    return "-".join(stripped[i : i + _GROUP] for i in range(0, _USER_CODE_LEN, _GROUP))


async def describe(session: AsyncSession, *, device_code: str) -> DeviceAuthorization:
    """The request behind a long code, without answering it.

    The collection path needs to know WHICH surface opened a request
    before it can say what that surface is granted, and it must read that
    from the row rather than from the call: the client was decided when
    the request was opened and approved, and a caller that could
    re-specify it here would be choosing its own scope.

    Every shape of "not a live request" is one answer, so a caller
    holding a wrong code learns nothing from which error it got."""
    row = (
        await session.execute(
            select(DeviceAuthorization).where(
                DeviceAuthorization.device_code_hash == _hash(device_code)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.AUTH_DEVICE_CODE_INVALID)
    return row


async def check_open_rate(
    session: AsyncSession,
    *,
    origin_ip: str | None,
    limit: int,
    window_seconds: int,
) -> None:
    """Refuse an origin that is opening requests faster than a person
    could answer them.

    Counted from the rows themselves rather than from a separate counter
    table: opening a request IS the thing being limited, each one is
    already recorded here with its origin and its moment, and a second
    store would be a second thing to keep true. The sweep keeps the
    window cheap to count.

    An origin the server could not resolve is NOT limited, and that is a
    deliberate hole with a name: behind a proxy chain with no configured
    trust anchor every caller would otherwise share one bucket, and the
    first noisy client would lock out everybody else. What closes it is
    configuring the trusted proxies, not counting a value that cannot be
    attributed."""
    if origin_ip is None:
        return
    since = _now() - datetime.timedelta(seconds=window_seconds)
    opened = (
        await session.execute(
            select(func.count())
            .select_from(DeviceAuthorization)
            .where(
                DeviceAuthorization.origin_ip == origin_ip,
                DeviceAuthorization.created_at >= since,
            )
        )
    ).scalar_one()
    if opened >= limit:
        security_events.emit(
            "device_authorization.rate_limited",
            origin_ip=origin_ip,
            limit=limit,
        )
        raise QuotaExceededError(MessageCode.RATE_LIMITED)


async def approve(
    session: AsyncSession,
    *,
    user_code: str,
    approver_id: uuid.UUID,
    org_id: uuid.UUID,
) -> DeviceAuthorization:
    """Record the answer. Mints nothing.

    The claim is a conditional UPDATE rather than a read followed by a
    write: two people cannot both be the approver of one request, and the
    device must not be able to collect twice by racing the screen."""
    row = await find_open(session, user_code=user_code)
    claimed = (
        await session.execute(
            update(DeviceAuthorization)
            .where(
                DeviceAuthorization.id == row.id,
                DeviceAuthorization.approved_at.is_(None),
                DeviceAuthorization.denied_at.is_(None),
                DeviceAuthorization.redeemed_at.is_(None),
            )
            .values(approved_at=_now(), answered_by_id=approver_id, approved_org_id=org_id)
            .returning(DeviceAuthorization.id)
        )
    ).scalar_one_or_none()
    if claimed is None:
        raise NotFoundError(MessageCode.AUTH_DEVICE_CODE_INVALID)
    await session.refresh(row)
    return row


async def deny(session: AsyncSession, *, user_code: str, actor_id: uuid.UUID) -> None:
    """Refuse it. The row stays, marked and attributed, so the device is
    told "no" rather than being left to time out -- a person who refuses
    has decided something, and the device should say so instead of
    showing a spinner for ten minutes."""
    row = await find_open(session, user_code=user_code)
    await session.execute(
        update(DeviceAuthorization)
        .where(
            DeviceAuthorization.id == row.id,
            DeviceAuthorization.approved_at.is_(None),
            DeviceAuthorization.denied_at.is_(None),
        )
        .values(denied_at=_now(), answered_by_id=actor_id)
    )


async def redeem(
    session: AsyncSession,
    *,
    device_code: str,
    label: str,
    scope: list[str],
    provider: str,
    credential_ttl_days: int = DEFAULT_CREDENTIAL_TTL_DAYS,
) -> RedeemedCredential:
    """Collect. This is where the credential comes into existence.

    Raises, and the distinction matters to the caller because the device
    reacts differently to each:

    - ``AUTH_DEVICE_PENDING``: nobody has answered yet. The ordinary
      answer for most of a request's life, and NOT a failure. The HTTP
      adapter must render it as a 4xx rather than a 2xx: every HTTP
      client reads a 2xx as "it worked", and a client that stores an
      empty session and believes itself connected is a real bug that the
      sibling project shipped and found the hard way.
    - ``AUTH_DEVICE_DENIED``: answered, no. Stop asking.
    - ``AUTH_DEVICE_EXPIRED``: nobody answered in time. Open another.
    - ``AUTH_DEVICE_CODE_INVALID``: no such request, or it was already
      collected. Also where a replayed device code lands."""
    row = (
        await session.execute(
            select(DeviceAuthorization).where(
                DeviceAuthorization.device_code_hash == _hash(device_code)
            )
        )
    ).scalar_one_or_none()
    if row is None or row.redeemed_at is not None:
        raise NotFoundError(MessageCode.AUTH_DEVICE_CODE_INVALID)
    if row.denied_at is not None:
        raise AuthError(MessageCode.AUTH_DEVICE_DENIED)
    if row.approved_at is None:
        # Expiry is checked AFTER the answer: a request approved a second
        # before it lapsed has been answered, and the person who answered
        # it should not have their decision thrown away by the clock.
        if row.expires_at <= _now():
            raise AuthError(MessageCode.AUTH_DEVICE_EXPIRED)
        raise AuthError(MessageCode.AUTH_DEVICE_PENDING)
    if row.answered_by_id is None or row.approved_org_id is None:
        # Approved with no approver recorded is not a state this module
        # can produce. If it is ever read, something else wrote this row.
        raise NotFoundError(MessageCode.AUTH_DEVICE_CODE_INVALID)

    # Claim it before minting. Both statements are in the caller's
    # transaction, so a failure in the mint rolls the claim back and the
    # device's next question is answered normally rather than with "no
    # such request".
    claimed = (
        await session.execute(
            update(DeviceAuthorization)
            .where(
                DeviceAuthorization.id == row.id,
                DeviceAuthorization.redeemed_at.is_(None),
            )
            .values(redeemed_at=_now())
            .returning(DeviceAuthorization.id)
        )
    ).scalar_one_or_none()
    if claimed is None:
        raise NotFoundError(MessageCode.AUTH_DEVICE_CODE_INVALID)

    # The collecting request is unauthenticated -- a device holding a
    # code and nothing else -- so it carries no tenant, and the insert
    # below is under row-level security. The window is opened as the
    # person who APPROVED, read from the row rather than from anything
    # the caller sent, and it closes before this function returns.
    async with as_tenant(session, org_id=row.approved_org_id, user_id=row.answered_by_id):
        created = await ai_assistants.create_assistant(
            session,
            org_id=row.approved_org_id,
            # The authority is the approver's, recorded when they
            # answered. Never the caller's.
            actor_id=row.answered_by_id,
            label=label,
            scope=scope,
            provider=provider,
            workspace_binding=WorkspaceBinding.account,
            token_ttl_days=credential_ttl_days,
        )
    await session.execute(
        update(DeviceAuthorization)
        .where(DeviceAuthorization.id == row.id)
        .values(assistant_id=created.assistant.id)
    )
    token_expiry = created.token_expires_at
    return RedeemedCredential(
        secret=created.raw_secret,
        assistant_id=created.assistant.id,
        org_id=row.approved_org_id,
        scope=list(created.assistant.scope_list()),
        expires_at=token_expiry,
    )


async def sweep_expired(session: AsyncSession, *, older_than_days: int = 7) -> int:
    """Delete requests that are long finished.

    A row here is a record of something that did or did not happen, and
    it stops being interesting once the window it describes is well past.
    Deleting on expiry instead would take the answered ones with it, and
    a denial is worth being able to see for a few days."""
    cutoff = _now() - datetime.timedelta(days=older_than_days)
    doomed = (
        (
            await session.execute(
                select(DeviceAuthorization.id).where(DeviceAuthorization.expires_at < cutoff)
            )
        )
        .scalars()
        .all()
    )
    if not doomed:
        return 0
    await session.execute(sa_delete(DeviceAuthorization).where(DeviceAuthorization.id.in_(doomed)))
    return len(doomed)


__all__ = [
    "DEFAULT_CREDENTIAL_TTL_DAYS",
    "DEFAULT_REQUEST_TTL_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "OpenedRequest",
    "RedeemedCredential",
    "approve",
    "check_open_rate",
    "deny",
    "describe",
    "find_open",
    "open_request",
    "redeem",
    "sweep_expired",
]
