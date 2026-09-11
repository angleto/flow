"""Device authorization grant: the HTTP surface of the flow a browser
extension connects through.

Five routes in two halves, and the split is the whole security story.

The DEVICE half (``authorize``, ``token``) is unauthenticated, because a
device that has nothing is exactly who is calling. Neither route can be
talked into producing anything on its own: ``authorize`` creates a request
that grants nothing until a person answers it, and ``token`` answers
"still waiting" until one has.

The PERSON half (``pending``, ``approve``, ``deny``) is authenticated and
HUMAN_ONLY. Approving a device is minting a credential, so it is fenced
off from scoped credentials for the same reason the assistant routes are:
a credential that could approve a device could mint its way out of its own
scope.

**Waiting is a 4xx.** ``token`` answers an unapproved request with 400 and
a domain code, never 202. Every HTTP client reads a 2xx as "it worked",
and a client that stores an empty answer and believes itself connected is
a real bug rather than a hypothetical one: the sibling project shipped it
and its test caught it. The standard grant uses 400 for the same reason.

**What the responses do not say.** An unknown device code, one already
collected, and one that never existed are one answer. A short code that
matches nothing open is absent rather than described. Neither route
confirms the existence of anything to a caller that does not already hold
the right code.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from mycelium_api.deps import TenantCtx, _resolve_issuer_client_ip, current_user, tenant_ctx
from mycelium_api.schemas import (
    DeviceAnswerIn,
    DeviceAuthorizeIn,
    DeviceAuthorizeOut,
    DevicePendingOut,
    DeviceTokenIn,
    DeviceTokenOut,
)
from mycelium_core.db import admin_session
from mycelium_core.errors import ForbiddenError
from mycelium_core.i18n import MessageCode
from mycelium_core.mcp_scopes import EXTENSION_PROVIDER, EXTENSION_SCOPES
from mycelium_core.models.organization import Organization
from mycelium_core.models.user import User
from mycelium_core.services import device_authorization as svc

router = APIRouter(prefix="/auth/device", tags=["auth"])

# The surfaces allowed to open a request, and what each one is granted.
# A closed set rather than a free-text label: ``client`` decides the scope
# that will be minted, so an unrecognised value must be refused rather
# than defaulted (a default here would be a credential nobody specified).
_CLIENTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # key -> (label on the credential, provider, scope granted)
    "extension": ("Browser extension", EXTENSION_PROVIDER, EXTENSION_SCOPES),
}

# Where a person goes to answer. A path, resolved by the device against
# the deployment it already talks to.
VERIFICATION_PATH = "/settings/extension"

# How many requests one network origin may open in the window. Generous
# for a person who fumbles the ceremony twice, and far below what is
# needed to farm short codes: the space is 30^8 and a code grants nothing
# until somebody approves it, so this bounds table growth and noise more
# than it bounds an attack.
_OPEN_LIMIT = 12
_OPEN_WINDOW_SECONDS = 600


def _client_or_refuse(client: str) -> tuple[str, str, tuple[str, ...]]:
    spec = _CLIENTS.get(client)
    if spec is None:
        # Not NotFound: the caller named something, and what is wrong is
        # the name, not the absence of a thing they were entitled to.
        raise ForbiddenError(MessageCode.AUTH_DEVICE_CODE_INVALID)
    return spec


@router.post("/authorize", response_model=DeviceAuthorizeOut)
async def device_authorize_endpoint(
    body: DeviceAuthorizeIn,
    request: Request,
) -> DeviceAuthorizeOut:
    """Open a request. Unauthenticated: the caller is a device holding
    nothing, and what it receives here grants nothing until a person
    approves it."""
    _client_or_refuse(body.client)
    # The address the SERVER resolved, never one the client declared.
    # Shared with the issuer-key allowlist rather than re-derived, so
    # there is one answer to "who is calling" and not two that can
    # disagree about proxy headers.
    origin_ip = _resolve_issuer_client_ip(request)
    async with admin_session() as session:
        await svc.check_open_rate(
            session,
            origin_ip=origin_ip,
            limit=_OPEN_LIMIT,
            window_seconds=_OPEN_WINDOW_SECONDS,
        )
        opened = await svc.open_request(session, client=body.client, origin_ip=origin_ip)
    return DeviceAuthorizeOut(
        device_code=opened.device_code,
        user_code=opened.user_code,
        verification_path=VERIFICATION_PATH,
        expires_at=opened.expires_at,
        interval=opened.interval,
    )


@router.post("/token", response_model=DeviceTokenOut)
async def device_token_endpoint(body: DeviceTokenIn) -> DeviceTokenOut:
    """Collect. The credential is created by this call and not before.

    The domain codes a device branches on, all of them 4xx:
    ``auth.device_pending`` (keep asking), ``auth.device_denied`` and
    ``auth.device_expired`` (stop, for opposite reasons), and
    ``auth.device_code_invalid`` (no such request, or already collected).
    """
    async with admin_session() as session:
        # Read the client off the row rather than from the request: what
        # is being granted was decided when the request was opened and
        # approved, and a caller must not be able to re-specify it here.
        pending = await svc.describe(session, device_code=body.device_code)
        label, provider, scope = _client_or_refuse(pending.client)
        granted = await svc.redeem(
            session,
            device_code=body.device_code,
            label=label,
            scope=list(scope),
            provider=provider,
        )
        org = await session.get(Organization, granted.org_id)
    return DeviceTokenOut(
        secret=granted.secret,
        assistant_id=granted.assistant_id,
        workspace_id=granted.org_id,
        # Where the panel should OPEN, which is the workspace the person
        # was in when they approved. Not the credential's perimeter: that
        # is every workspace they belong to.
        workspace_name=org.name if org is not None else "",
        scope=granted.scope,
        expires_at=granted.expires_at,
    )


@router.get("/pending", response_model=DevicePendingOut)
async def device_pending_endpoint(
    user_code: Annotated[str, Query(min_length=1, max_length=32)],
    user: Annotated[User, Depends(current_user)],
) -> DevicePendingOut:
    """What a person is being asked to approve.

    Authenticated, and deliberately NOT scoped to the caller: a request
    belongs to nobody until it is answered, and the person reading this
    is the one about to make it theirs. What defends the decision is the
    code comparison, which is why the code is echoed back here.

    The grant comes from the server's own list for that client, so the
    screen cannot disclose one thing while the mint does another."""
    async with admin_session() as session:
        row = await svc.find_open(session, user_code=user_code)
        _, _, scope = _client_or_refuse(row.client)
        return DevicePendingOut(
            user_code=row.user_code,
            client=row.client,
            opened_at=row.created_at,
            expires_at=row.expires_at,
            origin_ip=row.origin_ip,
            scope=list(scope),
        )


@router.post("/approve", status_code=status.HTTP_204_NO_CONTENT)
async def device_approve_endpoint(
    body: DeviceAnswerIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> Response:
    """Answer yes. Mints nothing: it records who approved and where, and
    the device collects afterwards.

    The workspace comes from the request's tenant context, the same one
    every other write here resolves through, never from the body: a
    person approves in the workspace they are looking at, and letting a
    body name one would be letting a page choose the tenancy of a
    credential."""
    await svc.approve(
        ctx.session, user_code=body.user_code, approver_id=ctx.user_id, org_id=ctx.org_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/deny", status_code=status.HTTP_204_NO_CONTENT)
async def device_deny_endpoint(
    body: DeviceAnswerIn,
    user: Annotated[User, Depends(current_user)],
) -> Response:
    """Answer no. The device is told, rather than left on a spinner
    until the request times out."""
    async with admin_session() as session:
        await svc.deny(session, user_code=body.user_code, actor_id=user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
