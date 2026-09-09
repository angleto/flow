"""FastAPI dependencies: user extraction from JWT and tenant session.

The dependency opens a ``tenant_session`` (which sets the RLS GUCs) and
verifies the user's membership in the org: isolation is enforced by the
DB, here we only authenticate/authorize.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_api import route_scopes
from mycelium_core.config import get_settings
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import AuthError, ForbiddenError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_token import WorkspaceBinding
from mycelium_core.models.attachment import Attachment
from mycelium_core.models.membership import Role
from mycelium_core.models.user import User
from mycelium_core.security import decode_token_async
from mycelium_core.services import agent_tokens as agent_tokens_svc
from mycelium_core.services import capability_tokens, issuer_api_keys
from mycelium_core.services.auth import assert_token_not_revoked
from mycelium_core.services.rbac import _RANK, get_role

# auto_error=False: the scheme is published to OpenAPI (so Swagger shows
# "Authorize"), but absence/malformed credentials are reported via our
# i18n AuthError (401, code auth.missing_bearer), not FastAPI's default
# 403 {"detail": "Not authenticated"}. Keeps the error contract stable.
_bearer_scheme = HTTPBearer(auto_error=False, bearerFormat="JWT")


def _bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError(MessageCode.AUTH_MISSING_BEARER)
    return credentials.credentials


async def current_claims(
    request: Request,
    token: Annotated[str, Depends(_bearer_token)],
) -> dict[str, Any]:
    """Decoded bearer claims. Accepts both session JWTs (SPA / login
    flow) and agent tokens (``mycelium_at_...``) used by the CLI / MCP.

    For JWTs the dict carries ``sub``, ``jti``, ``iat``, ``exp``.
    For agent tokens ``sub``, ``org_id``, ``scope``, ``typ='agent'``.
    Either flavour is enough for ``current_user_id`` (reads ``sub``)
    plus the JWT revocation check (skipped gracefully when no ``jti``
    is present, since agent tokens carry their own revocation state
    enforced inside ``decode_token_async``)."""
    # ``enforce_route_scope`` runs first (app-level dependency) and has
    # already decoded an agent token to read its scope. Reuse that result so
    # a request authenticates once, not twice.
    cached = getattr(request.state, "claims", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    claims = await decode_token_async(token)
    request.state.claims = claims
    return claims


async def enforce_route_scope(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> None:
    """Confine a scoped assistant to the routes its scope covers (task
    c19f2f63, enabler B).

    Registered as an APPLICATION-level dependency, so it runs before any
    route's own dependencies and covers every route uniformly -- including
    the capability-or-bearer ones, which never see ``current_claims``.
    Without it the MCP tool gate is bypassable by simply not speaking MCP:
    the same ``mycelium_at_`` token authenticates here too.

    No-op for everything that is not a *scoped* assistant token, so human
    sessions, bare agent tokens and capability tokens are unaffected."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        return
    token = credentials.credentials
    if capability_tokens.is_capability_token(token):
        # A capability token carries its own action/resource authorization,
        # validated where it is redeemed. It is not an assistant credential.
        return
    if not agent_tokens_svc.is_agent_token(token):
        return  # human session JWT
    try:
        claims = await decode_token_async(token)
    except AuthError:
        # Leave the verdict to the route's own auth dependency: this gate
        # must not turn a bad bearer on a public route into a new 401.
        return
    request.state.claims = claims
    scope = claims.get("assistant_scope")
    if scope is None:
        return  # bare token (no bound assistant) -> no per-route restriction
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path is None:
        return  # unmatched -> 404 handling; nothing to gate
    if not route_scopes.scope_permits(request.method, path, scope):
        raise ForbiddenError(MessageCode.AGENT_SCOPE_DENIED)


def require_agent_scope(claims: dict[str, Any], scope_key: str | frozenset[str]) -> None:
    """Enforce a scope requirement inside a handler (task c19f2f63, review #5).

    The app-level gate keys off (method, path) and runs before the body is
    parsed, so a route whose required scope depends on the request body (a kind
    multiplexer, e.g. the note<->task link routes) can only be gated to an
    any-of baseline there. The handler, which HAS the body, calls this to
    enforce the exact key the chosen operation needs. No-op for a human session
    or a bare token (``assistant_scope`` is None = full access); a bound
    assistant lacking the requirement gets the same 403 the app-level gate
    raises.

    A frozenset means ANY ONE of those keys is enough, matching the value
    shape ``route_scopes`` uses. The 403 has to name ONE key to ask for,
    and it must be the narrow one: a transitional set exists so an old
    wide grant keeps working, so telling the caller to go and get the wide
    key would push grants in exactly the direction the split was made to
    reverse. The lowest sorted member is used because a set has no order,
    and that it IS the narrow key for every declared set is asserted by a
    test rather than assumed here."""
    scope = claims.get("assistant_scope")
    if scope is None:
        return
    required = frozenset({scope_key}) if isinstance(scope_key, str) else scope_key
    if not (required & set(scope)):
        raise ForbiddenError(MessageCode.AGENT_SCOPE_DENIED, scope=sorted(required)[0])


async def current_claims_optional(
    token: Annotated[str, Depends(_bearer_token)],
) -> dict[str, Any]:
    """Like :func:`current_claims` but tolerates a capability token
    (``mycelium_cap_``): returns ``{}`` instead of trying to decode it as a JWT
    / agent token (which would 401). Used by routes that accept BOTH a
    normal bearer and a ``mycelium_cap_`` token and only need the bearer's
    claims for identity attribution -- on the capability branch the write
    is attributed to the token's user (no assistant badge), matching the
    note-part capability path."""
    if capability_tokens.is_capability_token(token):
        return {}
    return await decode_token_async(token)


async def current_user_id(
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> uuid.UUID:
    sub = claims.get("sub")
    if not isinstance(sub, str):
        raise AuthError(MessageCode.AUTH_TOKEN_NO_SUB)
    # Stateless JWT + revocation list: every authenticated request
    # checks the jti against revoked_tokens (ADR-0024). Malformed jti
    # is treated as "no jti" (legacy tokens), not a 500.
    jti = claims.get("jti")
    if isinstance(jti, str):
        try:
            jti_uuid = uuid.UUID(jti)
        except ValueError:
            jti_uuid = None
        if jti_uuid is not None:
            async with admin_session() as session:
                await assert_token_not_revoked(session, jti=jti_uuid)
    return uuid.UUID(sub)


async def current_user(
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> User:
    """The authenticated User row. ``users`` is global (not RLS-scoped:
    login resolves the email before any org context), so this uses an
    admin session. A deactivated account is rejected here, so a soft
    lock (is_active=False) takes effect on the next request."""
    async with admin_session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError(MessageCode.AUTH_TOKEN_INVALID)
    return user


async def _active_user(user_id: uuid.UUID) -> User:
    """Look up the active User by id (global table, admin session), same
    rejection contract as ``current_user``. Used by the part-body-stream
    dep for the capability-token branch, which has the user id from the
    token rather than from a decoded JWT subject."""
    async with admin_session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError(MessageCode.AUTH_TOKEN_INVALID)
    return user


async def _resolve_user(token: str) -> tuple[User, dict[str, Any]]:
    """Decode a JWT / agent-token bearer, enforce jti revocation, and
    return the active User WITH its claims. Mirrors ``current_user_id`` +
    ``current_user`` for the part-body-stream dep, which cannot reuse
    those FastAPI deps because it must first branch on a capability-token
    bearer.

    The claims come back with the user because the caller needs them to
    confine an agent token to its workspace, and decoding a second time
    to get them would mean a second SECURITY DEFINER lookup per request
    on this path."""
    claims = await decode_token_async(token)
    sub = claims.get("sub")
    if not isinstance(sub, str):
        raise AuthError(MessageCode.AUTH_TOKEN_NO_SUB)
    jti = claims.get("jti")
    if isinstance(jti, str):
        try:
            jti_uuid: uuid.UUID | None = uuid.UUID(jti)
        except ValueError:
            jti_uuid = None
        if jti_uuid is not None:
            async with admin_session() as session:
                await assert_token_not_revoked(session, jti=jti_uuid)
    return await _active_user(uuid.UUID(sub)), claims


_TRUTHY = {"1", "true", "yes", "on"}


def admin_mode_active(
    user: User,
    x_admin_mode: str | None,
) -> bool:
    """Sudo-style elevation: an admin runs as a normal user by default
    and only acts as admin when the SPA explicitly sends X-Admin-Mode.
    The header is honoured *only* if the account actually has the
    capability, so a forged header can never escalate a non-admin
    (same trust model as costa_associati's X-Active-Role)."""
    return user.is_admin and (x_admin_mode is not None and x_admin_mode.strip().lower() in _TRUTHY)


def effective_role(
    *,
    membership: Role | None,
    user: User,
    x_workspace_role: str | None,
    x_admin_mode: str | None,
) -> Role:
    """The role the request actually runs with.

    Two-step, same trust model as ``admin_mode_active`` (a header is
    honoured only up to the entitlement; a forged header can never
    escalate):

    - *ceiling* = the most this caller is entitled to. A global admin
      in admin-mode acts as ``owner`` of ANY workspace (even with no
      membership: that is the sudo escape hatch). Otherwise it is the
      caller's actual membership role; a non-admin without membership
      has no ceiling and is rejected by the caller.
    - *requested* = ``X-Workspace-Role`` parsed to a valid Role;
      absent/invalid defaults to ``member`` (least privilege: "by
      default I act as a plain user").

    The effective role is the requested role clamped DOWN to the
    ceiling: a member asking for ``owner`` stays a member."""
    if admin_mode_active(user, x_admin_mode):
        ceiling = Role.owner
    elif membership is None:
        raise ForbiddenError(MessageCode.RBAC_NO_MEMBERSHIP)
    else:
        ceiling = membership
    try:
        requested = Role(x_workspace_role) if x_workspace_role else Role.member
    except ValueError:
        requested = Role.member
    return requested if _RANK[requested] <= _RANK[ceiling] else ceiling


async def require_admin(
    user: Annotated[User, Depends(current_user)],
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> User:
    """Gate for the admin surface (global user administration). Needs
    both the capability (is_admin) and an *active* elevation; without
    the elevation an admin is treated exactly like a normal user."""
    if not admin_mode_active(user, x_admin_mode):
        raise ForbiddenError(MessageCode.ADMIN_REQUIRED)
    return user


@dataclass(frozen=True, slots=True)
class TenantCtx:
    session: AsyncSession
    user_id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID | None
    role: Role
    # Set only when the request authenticated with a capability token
    # (the part-body-stream dep); the endpoint consumes it on success.
    capability_token_id: uuid.UUID | None = None


@asynccontextmanager
async def _tenant_scope(
    user: User,
    org_id: uuid.UUID,
    project_id: uuid.UUID | None,
    *,
    x_workspace_role: str | None,
    x_admin_mode: str | None,
) -> AsyncIterator[TenantCtx]:
    """RLS-scoped tenant session shared by ``tenant_ctx`` and the
    part-body-stream dep: open the session as the human/api caller,
    resolve the sudo-clamped effective role, publish it for the
    service-layer RBAC choke point, and yield the ctx. Behaviour is
    identical to the original inline ``tenant_ctx`` body."""
    async with tenant_session(
        str(org_id),
        str(user.id),
        str(project_id) if project_id else None,
        actor_kind="human_api",
    ) as session:
        try:
            membership: Role | None = await get_role(session, org_id, user.id)
        except NotFoundError:
            membership = None
        # X-Workspace-Role is a *downgrade* lever (the SPA's per-tab
        # "act as" switch): the effective role is the requested role
        # clamped to the caller's entitlement. A global admin in
        # admin-mode is entitled to owner on any workspace; a non-admin
        # with no membership is rejected here (behaviour preserved).
        role = effective_role(
            membership=membership,
            user=user,
            x_workspace_role=x_workspace_role,
            x_admin_mode=x_admin_mode,
        )
        # Publish the sudo-clamped role for the service-layer RBAC
        # choke point (rbac.require_role), same transaction-local GUC
        # mechanism as the RLS tenant context. Without this the
        # service re-derives from stored membership and the "act as"
        # downgrade would not bind (privilege-confinement bug).
        await session.execute(
            text("SELECT set_config('app.current_role', :r, true)"),
            {"r": role.value},
        )
        yield TenantCtx(
            session=session,
            user_id=user.id,
            org_id=org_id,
            project_id=project_id,
            role=role,
        )


def _confine_agent_token(claims: dict[str, Any], org_id: uuid.UUID) -> None:
    """An agent token may only act in the workspace it was minted for.

    The token has carried ``org_id`` since it was introduced, and every
    client already believes the binding: the CLI stores a workspace
    alongside each credential, refuses to switch, and tells the user to
    re-mint ("agent tokens are workspace-scoped; there is no side-effect
    switch"). The SERVER did not check it. So the header decided the
    tenant, and a credential minted for one workspace worked in every
    other workspace its user happened to belong to -- which makes the
    per-workspace model a convention held up by clients rather than a
    boundary.

    The MCP surface never had the gap: there the tenant comes from the
    principal, not from a header.

    A human session JWT is untouched: it has no ``org_id`` claim, because
    a person legitimately moves between their workspaces on one login.
    That is the whole difference between a session and a credential.

    An ``account``-bound credential is the deliberate exception, and it
    is not a hole in the rule above: it moves between workspaces the way
    a session does, and it is authorized the way a session is. The
    workspace still arrives per request; ``_tenant_scope`` still resolves
    the HOLDER's membership there and refuses a workspace they do not
    belong to; the effective role is still clamped to that membership,
    so the credential is a guest where its holder is a guest. What it
    does NOT do is carry authority of its own into a workspace nobody
    granted it."""
    if claims.get("typ") != "agent":
        return
    if claims.get("workspace_binding") == WorkspaceBinding.account.value:
        return
    bound = claims.get("org_id")
    if not bound:
        # A pre-binding token. Refusing would log people out of a
        # credential that was valid when it was minted; the mint path has
        # always set this, so this branch is defensive rather than
        # expected.
        return
    try:
        bound_id = uuid.UUID(str(bound))
    except ValueError:
        raise ForbiddenError(MessageCode.AGENT_WORKSPACE_MISMATCH) from None
    if bound_id != org_id:
        # Forbidden, not "absent": the caller may well be a member of the
        # workspace they asked for, and hiding that behind a 404 would
        # tell them the workspace does not exist. What is wrong is the
        # CREDENTIAL, and the fix is to mint one for that workspace.
        raise ForbiddenError(MessageCode.AGENT_WORKSPACE_MISMATCH)


async def tenant_ctx(
    user: Annotated[User, Depends(current_user)],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
    x_workspace_id: Annotated[str, Header()],
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    # Header is X-Workspace-Id (user-facing). Internally the tenant is
    # still org_id (RLS unchanged, ADR-0015); the rename lives here in
    # the adapter, not in core.
    org_id = uuid.UUID(x_workspace_id)
    _confine_agent_token(claims, org_id)
    project_id = uuid.UUID(x_project_id) if x_project_id else None
    async with _tenant_scope(
        user,
        org_id,
        project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as ctx:
        yield ctx


@dataclass(frozen=True, slots=True)
class IssuerKeyCtx:
    """Request context for the public Invoice API, authenticated by a
    per-issuer-profile API key (task 19b7e874). The principal is the KEY, not a
    user: authorization is a pure function of ``permissions`` + a pinned
    ``member`` role, so it is unaffected by the minting user's live role or
    deletion (H1). Every by-id route must scope to ``issuer_profile_id`` (H2)."""

    session: AsyncSession
    org_id: uuid.UUID
    issuer_profile_id: uuid.UUID
    permissions: frozenset[str]
    key_id: uuid.UUID


def _resolve_issuer_client_ip(request: Request) -> str | None:
    """Best-effort TRUSTWORTHY source address for the issuer-key IP allowlist.

    Deliberately NOT ``request.client.host``: production runs uvicorn with
    ``--proxy-headers --forwarded-allow-ips '*'``, which overwrites
    ``scope['client']`` with the LEFTMOST X-Forwarded-For token -- fully
    client-forgeable. Instead this reads the RAW ``X-Forwarded-For`` header
    (which uvicorn leaves intact) and resolves the client as the rightmost
    entry that is NOT one of the configured trusted proxies: an attacker
    cannot insert a hop to the right of the real remote address nginx appends
    (``$proxy_add_x_forwarded_for``), so the result is not forgeable to an
    arbitrary allowlisted value -- provided (a) ``issuer_key_trusted_proxies``
    lists the real infra hops and (b) the pod is reachable only via the proxy
    (NetworkPolicy), else a direct-to-pod caller controls the whole chain.

    Returns None (-> the caller fails the request CLOSED for a restricted key)
    when there is a forwarding chain but no configured trust anchor, when the
    header is malformed, or when every hop is a trusted proxy (the edge did
    not preserve the client). A request with no forwarding header at all
    (direct connection / the in-process test transport) uses the real peer.
    """
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return request.client.host if request.client else None
    trusted = get_settings().issuer_key_trusted_proxies
    if not trusted:
        return None
    try:
        nets = [ipaddress.ip_network(c, strict=False) for c in trusted]
    except ValueError:
        return None
    for hop in reversed([h.strip() for h in xff.split(",")]):
        if not hop:
            continue
        try:
            addr = ipaddress.ip_address(hop)
        except ValueError:
            return None  # a malformed hop poisons the chain -> fail closed
        if not any(addr in net for net in nets):
            return hop
    return None


async def issuer_key_ctx(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AsyncIterator[IssuerKeyCtx]:
    """Public Invoice API dependency. Resolves ``Authorization: Bearer
    mycelium_ik_...`` to an org+issuer-bound principal (no X-Workspace-Id: the
    key carries its tenant), opens an RLS tenant session as an
    ``issuer_api_key`` actor with the role PINNED to ``member`` (H1), and yields
    the ctx. A missing/malformed header is a distinct 401 (auth.missing_bearer);
    an unknown/revoked/expired key AND a source outside the key's allowlist are
    the same COLLAPSED 401 (auth.token_invalid) so the surface is neither a
    key-existence nor an allowlist oracle. The source address is resolved by
    ``_resolve_issuer_client_ip`` (trusted-proxy aware, fail-closed), NOT from
    the client-forgeable ``request.client``."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError(MessageCode.AUTH_MISSING_BEARER)
    raw = authorization[7:].strip()
    client_ip = _resolve_issuer_client_ip(request)
    princ = await issuer_api_keys.authenticate(raw, client_ip=client_ip)
    if princ is None:
        raise AuthError(MessageCode.AUTH_TOKEN_INVALID)
    async with tenant_session(
        str(princ.org_id),
        str(princ.key_id),
        actor_kind="issuer_api_key",
        actor_subject_id=str(princ.key_id),
    ) as session:
        # Pin the effective role so the reused service layer authorizes on
        # (key permissions + member), never on a human's stored membership.
        await session.execute(
            text("SELECT set_config('app.current_role', :r, true)"),
            {"r": Role.member.value},
        )
        yield IssuerKeyCtx(
            session=session,
            org_id=princ.org_id,
            issuer_profile_id=princ.issuer_profile_id,
            permissions=frozenset(princ.permissions),
            key_id=princ.key_id,
        )


def require_perm(ctx: IssuerKeyCtx, permission: str) -> None:
    """Exact-match per-endpoint permission gate, fail-closed on an unknown
    string. ``invoice:credit_note`` / ``invoice:compose`` never implicitly grant
    ``invoice:send`` or client-write."""
    if permission not in ctx.permissions:
        raise ForbiddenError(MessageCode.ISSUER_API_KEY_PERMISSION_DENIED, permission=permission)


@asynccontextmanager
async def _capability_or_bearer(
    token: str,
    *,
    expected_actions: tuple[str, ...],
    expected_resource_kinds: tuple[str, ...],
    resource_id: uuid.UUID | None,
    single_use: bool,
    x_workspace_id: str | None,
    x_project_id: str | None,
    x_workspace_role: str | None,
    x_admin_mode: str | None,
) -> AsyncIterator[tuple[TenantCtx, capability_tokens.AuthenticatedCapability | None]]:
    """Shared capability-or-bearer tenant context for every block route.

    On the ``mycelium_cap_`` branch: authenticate, validate
    ``action in expected_actions`` AND ``resource_kind in
    expected_resource_kinds`` AND (when ``resource_id`` is given)
    ``princ.resource_id == resource_id``, then open an ``mcp_token`` tenant
    session fixed to ``Role.member`` and yield ``(ctx, princ)``. Pass
    ``resource_id=None`` for a PARENT-scoped token whose exact target the
    caller re-checks against ``princ`` inside the session (e.g. an
    attachment row's parent FK). ``single_use=True`` sets
    ``capability_token_id`` so the endpoint consumes the token after the
    write commits. On the normal-bearer branch yield ``(ctx, None)`` with
    the same contract as ``tenant_ctx`` (``X-Workspace-Id`` required).

    The capability path is confined to the routes that depend on this:
    ``decode_token_async`` does not know ``mycelium_cap_``, so such a token is
    rejected everywhere else."""
    if capability_tokens.is_capability_token(token):
        princ = await capability_tokens.authenticate(token)
        if princ is None:
            raise AuthError(MessageCode.CAPABILITY_TOKEN_INVALID)
        if (
            princ.action not in expected_actions
            or princ.resource_kind not in expected_resource_kinds
            or (resource_id is not None and princ.resource_id != resource_id)
        ):
            raise ForbiddenError(MessageCode.CAPABILITY_TOKEN_SCOPE)
        user = await _active_user(princ.user_id)
        async with tenant_session(
            str(princ.org_id),
            str(user.id),
            actor_kind="mcp_token",
            actor_subject_id=str(princ.token_id),
        ) as session:
            await session.execute(
                text("SELECT set_config('app.current_role', :r, true)"),
                {"r": Role.member.value},
            )
            yield (
                TenantCtx(
                    session=session,
                    user_id=user.id,
                    org_id=princ.org_id,
                    project_id=None,
                    role=Role.member,
                    capability_token_id=princ.token_id if single_use else None,
                ),
                princ,
            )
        return
    # Normal bearer: same contract as tenant_ctx (X-Workspace-Id required).
    if not x_workspace_id:
        raise AuthError(MessageCode.AUTH_WORKSPACE_REQUIRED)
    user, claims = await _resolve_user(token)
    org_id = uuid.UUID(x_workspace_id)
    # The same confinement tenant_ctx applies. This path exists because
    # it must branch on a capability bearer first, and a rule enforced on
    # one of two tenant doors is not enforced.
    _confine_agent_token(claims, org_id)
    project_id = uuid.UUID(x_project_id) if x_project_id else None
    async with _tenant_scope(
        user,
        org_id,
        project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as ctx:
        yield ctx, None


async def _assert_attachment_parent(
    session: AsyncSession,
    attachment_id: uuid.UUID,
    princ: capability_tokens.AuthenticatedCapability,
) -> None:
    """Confine a parent-scoped attachment capability: the attachment must
    exist in the token's org (RLS) AND hang off the exact note/task the
    token is scoped to. Selects only the parent ids, never the ``data``
    blob."""
    row = (
        await session.execute(
            select(Attachment.note_id, Attachment.task_id).where(Attachment.id == attachment_id)
        )
    ).first()
    if row is None:
        raise NotFoundError(MessageCode.ATTACHMENT_NOT_FOUND)
    parent_ok = (
        princ.resource_kind == capability_tokens.RESOURCE_NOTE and row.note_id == princ.resource_id
    ) or (
        princ.resource_kind == capability_tokens.RESOURCE_TASK and row.task_id == princ.resource_id
    )
    if not parent_ok:
        raise ForbiddenError(MessageCode.CAPABILITY_TOKEN_SCOPE)


async def part_body_write_ctx(
    part_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Note-part body stream: bearer OR a single-use ``note_part_body:write``
    capability scoped to this ``part_id`` (consumed on success)."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_NOTE_PART_BODY_WRITE,),
        expected_resource_kinds=(capability_tokens.RESOURCE_NOTE_PART,),
        resource_id=part_id,
        single_use=True,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def attachment_read_ctx(
    attachment_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Attachment binary download: bearer OR a parent-scoped, multi-use
    ``attachment:read`` capability (one mint downloads every attachment of
    the note/task until it expires; NOT consumed, a download is
    idempotent). The token authorises only attachments whose parent FK
    matches its ``resource_id``."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_ATTACHMENT_READ,),
        expected_resource_kinds=(
            capability_tokens.RESOURCE_NOTE,
            capability_tokens.RESOURCE_TASK,
        ),
        resource_id=None,  # parent-scoped: re-check the attachment's parent below
        single_use=False,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, princ):
        if princ is not None:
            await _assert_attachment_parent(ctx.session, attachment_id, princ)
        yield ctx


# Exact-resource-scoped block deps. Each accepts a normal bearer OR a
# mycelium_cap_ token whose action+resource match the route. read = multi-use
# (idempotent, NOT consumed); write/patch = single-use (consumed after the
# write commits). The attachment-write deps key on the parent note/task in
# the path -- itself the scoped resource -- so they need no extra row read.


async def note_attachment_write_ctx(
    note_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Note attachment upload: bearer OR a single-use ``attachment:write``
    capability scoped to this note."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_ATTACHMENT_WRITE,),
        expected_resource_kinds=(capability_tokens.RESOURCE_NOTE,),
        resource_id=note_id,
        single_use=True,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def task_attachment_write_ctx(
    task_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Task attachment upload: bearer OR a single-use ``attachment:write``
    capability scoped to this task."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_ATTACHMENT_WRITE,),
        expected_resource_kinds=(capability_tokens.RESOURCE_TASK,),
        resource_id=task_id,
        single_use=True,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def part_body_read_ctx(
    part_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Note-part body raw download: bearer OR a multi-use
    ``note_part_body:read`` capability scoped to this part."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_NOTE_PART_BODY_READ,),
        expected_resource_kinds=(capability_tokens.RESOURCE_NOTE_PART,),
        resource_id=part_id,
        single_use=False,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def part_body_patch_ctx(
    part_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Note-part body patch: bearer OR a single-use ``note_part_body:patch``
    capability scoped to this part (consumed on success)."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_NOTE_PART_BODY_PATCH,),
        expected_resource_kinds=(capability_tokens.RESOURCE_NOTE_PART,),
        resource_id=part_id,
        single_use=True,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def task_description_read_ctx(
    task_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Task description raw download: bearer OR a multi-use
    ``task_description:read`` capability scoped to this task."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_TASK_DESCRIPTION_READ,),
        expected_resource_kinds=(capability_tokens.RESOURCE_TASK,),
        resource_id=task_id,
        single_use=False,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def task_description_write_ctx(
    task_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Task description full-replace stream: bearer OR a single-use
    ``task_description:write`` capability scoped to this task."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_TASK_DESCRIPTION_WRITE,),
        expected_resource_kinds=(capability_tokens.RESOURCE_TASK,),
        resource_id=task_id,
        single_use=True,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def task_description_patch_ctx(
    task_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Task description patch: bearer OR a single-use
    ``task_description:patch`` capability scoped to this task (consumed on
    success)."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_TASK_DESCRIPTION_PATCH,),
        expected_resource_kinds=(capability_tokens.RESOURCE_TASK,),
        resource_id=task_id,
        single_use=True,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def annotation_body_read_ctx(
    annotation_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Comment/annotation body raw download: bearer OR a multi-use
    ``annotation_body:read`` capability scoped to this annotation."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_ANNOTATION_BODY_READ,),
        expected_resource_kinds=(capability_tokens.RESOURCE_ANNOTATION,),
        resource_id=annotation_id,
        single_use=False,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def annotation_body_write_ctx(
    annotation_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Comment/annotation body stream edit: bearer OR a single-use
    ``annotation_body:write`` capability scoped to this annotation."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_ANNOTATION_BODY_WRITE,),
        expected_resource_kinds=(capability_tokens.RESOURCE_ANNOTATION,),
        resource_id=annotation_id,
        single_use=True,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def annotation_body_patch_ctx(
    annotation_id: uuid.UUID,
    token: Annotated[str, Depends(_bearer_token)],
    x_workspace_id: Annotated[str | None, Header()] = None,
    x_project_id: Annotated[str | None, Header()] = None,
    x_workspace_role: Annotated[str | None, Header()] = None,
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> AsyncIterator[TenantCtx]:
    """Comment/annotation body patch: bearer OR a single-use
    ``annotation_body:patch`` capability scoped to this annotation (consumed
    on success)."""
    async with _capability_or_bearer(
        token,
        expected_actions=(capability_tokens.ACTION_ANNOTATION_BODY_PATCH,),
        expected_resource_kinds=(capability_tokens.RESOURCE_ANNOTATION,),
        resource_id=annotation_id,
        single_use=True,
        x_workspace_id=x_workspace_id,
        x_project_id=x_project_id,
        x_workspace_role=x_workspace_role,
        x_admin_mode=x_admin_mode,
    ) as (ctx, _princ):
        yield ctx


async def tenant_admin_ctx(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    user: Annotated[User, Depends(current_user)],
    x_admin_mode: Annotated[str | None, Header()] = None,
) -> TenantCtx:
    """Platform-admin surface *inside* a tenant: an RLS-scoped tenant
    session (so the write still lands in the caller's workspace, ADR
    isolation preserved) gated by the SAME sudo rule as the global
    admin surface (``require_admin``): the account must have the
    capability (``is_admin``) AND an active elevation (``X-Admin-Mode``).
    A workspace owner who is not a platform admin -- even with the
    header -- is rejected with the channel-specific code. ``current_user``
    is shared (cached) with ``tenant_ctx`` so the identity is consistent.
    """
    if not admin_mode_active(user, x_admin_mode):
        raise ForbiddenError(MessageCode.CHANNEL_ADMIN_ONLY)
    return ctx
