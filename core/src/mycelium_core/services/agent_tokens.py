"""Agent-token service: mint / list / revoke / authenticate.

See ``models/agent_token.py`` and migration 0056 for the rationale.

Raw token format
----------------
``mycelium_at_<43 url-safe chars>``: a fixed ``mycelium_at_``
discriminator prefix that makes the type identifiable to the verifier
without trying JWT decode first, followed by ``secrets.token_urlsafe(32)``
(256 bits of entropy, URL-safe alphabet, no padding).

The stored ``prefix`` column is the first 20 characters of the raw
value (``mycelium_at_`` + 8 chars of random material) -- enough to give a
UI an unambiguous handle and short enough that it does not weaken the
secret.

Mint / revoke are owner-gated (minting credentials is a sensitive
operation; same gate model as the executor / billing tools).
``authenticate`` is the one verifier-side helper; the API surface does
not expose it directly, but the MCP / SPA bearer paths call it via
``decode_token_async``.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session
from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_token import AgentToken, WorkspaceBinding
from mycelium_core.models.membership import Role
from mycelium_core.services import audit
from mycelium_core.services.rbac import require_role

# Discriminator prefix. The verifier checks this BEFORE attempting JWT
# decode so we can branch on the credential kind without a try/except
# on cryptographic errors. Plain ASCII, never appears in a Mycelium JWT
# (those are ``<base64>.<base64>.<base64>``).
RAW_PREFIX: str = "mycelium_at_"
# Length of the random url-safe portion (in bytes of entropy; the
# resulting string is ~43 chars).
_RAW_ENTROPY_BYTES: int = 32
# How many leading chars of the raw value to persist as the
# (non-secret) ``prefix`` column. 20 = "mycelium_at_" (12) + 8 of randomness.
_PREFIX_CHARS: int = 20
# Default time-to-live for a freshly minted token (1 year, matching
# the design intent of "long-lived but expires by default so a
# forgotten credential is bounded").
DEFAULT_TTL_DAYS: int = 365


def _hash(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _generate_raw() -> str:
    return f"{RAW_PREFIX}{secrets.token_urlsafe(_RAW_ENTROPY_BYTES)}"


@dataclass(frozen=True, slots=True)
class MintResult:
    token: AgentToken
    # The raw value. Returned exactly once. The caller hands it to the
    # operator and forgets it; the DB only ever holds the hash.
    raw: str


@dataclass(frozen=True, slots=True)
class AuthenticatedAgent:
    token_id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    scope: str
    # Populated when the token is bound to an AI assistant (post-0059):
    # ``assistant_id`` identifies the assistant row, ``assistant_scope``
    # is the JSONB list of tool scopes the assistant is allowed to run
    # (the MCP gate uses it to filter @mcp.tool calls). NULL on legacy
    # bare tokens (pre-assistant), which keep their previous all-tools
    # access — the UI funnels new mints through the assistant flow.
    assistant_id: uuid.UUID | None = None
    assistant_scope: list[str] | None = None
    # Which workspaces this credential may act in. Read at
    # authentication time rather than looked up afterwards: a
    # credential's tenancy is part of what authenticating it establishes.
    workspace_binding: WorkspaceBinding = WorkspaceBinding.workspace


async def mint(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    scope: str = "mcp",
    ttl_days: int | None = DEFAULT_TTL_DAYS,
    assistant_id: uuid.UUID | None = None,
    workspace_binding: WorkspaceBinding = WorkspaceBinding.workspace,
    minimum_role: Role = Role.owner,
) -> MintResult:
    """Mint a fresh long-lived bearer token. Owner-gated by default.

    ``ttl_days=None`` disables expiry (a never-expiring credential);
    the default 365 days is a deliberate floor on forgotten secrets.

    ``assistant_id`` binds the token to an ``ai_assistants`` row when
    set (post-migration 0059); NULL keeps the legacy bare-token shape
    for back-compat with pre-assistant integrations.

    ``workspace_binding`` says which workspaces the credential may act
    in; ``org_id`` remains the workspace it was minted in either way.

    ``minimum_role`` is the threshold for MINTING, and the caller passes
    a lower one only where the credential cannot exceed what its holder
    can already do (``ai_assistants.create_assistant``, which derives it
    from the requested scope). It is not a parameter any request
    controls: no route reads it, and the one caller that lowers it
    computes it from the catalogue.
    """
    await require_role(session, org_id, actor_id, minimum_role)
    raw = _generate_raw()
    token_hash = _hash(raw)
    expires_at: datetime.datetime | None = None
    if ttl_days is not None and ttl_days > 0:
        expires_at = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=ttl_days)
    row = AgentToken(
        org_id=org_id,
        user_id=actor_id,
        name=name,
        prefix=raw[:_PREFIX_CHARS],
        token_hash=token_hash,
        scope=scope,
        expires_at=expires_at,
        assistant_id=assistant_id,
        workspace_binding=workspace_binding,
    )
    session.add(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="agent_token",
        entity_id=row.id,
        action="mint",
        diff={
            "name": name,
            "scope": scope,
            "assistant_id": str(assistant_id) if assistant_id else None,
            "workspace_binding": workspace_binding.value,
        },
    )
    return MintResult(token=row, raw=raw)


async def list_tokens(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
) -> list[AgentToken]:
    """RLS-scoped listing of all tokens in the current workspace
    (active + revoked, the UI distinguishes via ``revoked_at``).
    Member-level read so an operator can see what the workspace owner
    has minted -- the raw value is never in the row, so visibility is
    safe."""
    result = await session.execute(
        select(AgentToken).where(AgentToken.org_id == org_id).order_by(AgentToken.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    token_id: uuid.UUID,
) -> None:
    """Owner-gated. Mark a token revoked; idempotent (a re-revoke is a
    no-op that preserves the original ``revoked_at`` timestamp)."""
    await require_role(session, org_id, actor_id, Role.owner)
    result = await session.execute(
        select(AgentToken).where(AgentToken.id == token_id, AgentToken.org_id == org_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.AGENT_TOKEN_NOT_FOUND)
    if row.revoked_at is not None:
        return
    row.revoked_at = datetime.datetime.now(tz=datetime.UTC)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="agent_token",
        entity_id=row.id,
        action="revoke",
    )


def is_agent_token(raw: str) -> bool:
    """True iff the bearer looks like an agent token (discriminator
    prefix). Cheap pre-check so the verifier branches without paying a
    failed JWT decode."""
    return raw.startswith(RAW_PREFIX)


async def authenticate(
    raw: str,
    *,
    session: AsyncSession | None = None,
) -> AuthenticatedAgent | None:
    """Resolve a raw agent token to its principal, or ``None`` if it
    is unknown / revoked / expired.

    Crosses the tenant boundary: at the moment the MCP server / API
    bearer authenticator runs, the caller has not yet selected a
    workspace, so RLS would block a direct lookup. The SECURITY
    DEFINER ``authenticate_agent_token`` function (migration 0056)
    does the lookup, validates expiry / revocation, bumps
    ``last_used_at``, and returns the principal -- all while saving
    and restoring the caller's GUCs around the operation.

    ``session`` may be ``None`` (the helper opens its own
    ``admin_session``) or an already-open session whose GUCs the
    function will save & restore around its work. Either is correct;
    keeping it injectable lets the API bearer dep reuse its session.
    """
    if not is_agent_token(raw):
        return None
    token_hash = _hash(raw)
    if session is not None:
        return await _call_authenticate_fn(session, token_hash)
    async with admin_session() as s:
        return await _call_authenticate_fn(s, token_hash)


async def _call_authenticate_fn(
    session: AsyncSession, token_hash: bytes
) -> AuthenticatedAgent | None:
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT out_token_id, out_user_id, out_org_id, out_scope, "
            "out_assistant_id, out_assistant_scope, out_workspace_binding "
            "FROM authenticate_agent_token(:h)"
        ),
        {"h": token_hash},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    # The SECURITY DEFINER function gates a token bound to an
    # ``is_active=false`` assistant by returning no row, so by the time
    # we get here either the assistant is active or there is no
    # assistant binding (legacy bare token).
    raw_scope = row[5]
    assistant_scope: list[str] | None
    if raw_scope is None:
        # No assistant binding (legacy bare token) -> no per-tool restriction.
        # This is the ONLY value that may mean "full access".
        assistant_scope = None
    elif isinstance(raw_scope, list):
        assistant_scope = [str(s) for s in raw_scope]
    else:
        # A bound assistant whose scope column is malformed (not a JSONB list).
        # Fail CLOSED -- deny-all -- rather than reusing the None sentinel,
        # which would silently upgrade a corrupt row to full access. Mirrors
        # ``AiAssistant.scope_list()``, which already treats a non-list as no
        # scopes.
        assistant_scope = []
    try:
        binding = WorkspaceBinding(str(row[6]))
    except ValueError:
        # An unrecognised value is a row this code does not understand,
        # and the safe reading of a tenancy it cannot parse is the
        # narrow one. Never the wide one, which is why this is not a
        # default on the enum.
        binding = WorkspaceBinding.workspace
    return AuthenticatedAgent(
        token_id=row[0],
        user_id=row[1],
        org_id=row[2],
        scope=row[3],
        assistant_id=row[4],
        assistant_scope=assistant_scope,
        workspace_binding=binding,
    )


__all__ = [
    "DEFAULT_TTL_DAYS",
    "RAW_PREFIX",
    "AuthenticatedAgent",
    "MintResult",
    "authenticate",
    "is_agent_token",
    "list_tokens",
    "mint",
    "revoke",
]
