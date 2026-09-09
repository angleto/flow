"""Password hashing (argon2) and JWT tokens.

No insecure fallback: the JWT secret is mandatory (config.py).

Two token kinds share the verifier surface (v1.1 onwards):

- **JWT** (default): minted by ``/auth/login`` / ``/auth/signup`` etc.
  Short-lived (``jwt_ttl_seconds``). ``decode_token`` returns its
  claims dict and is the only function the SPA bearer path needs.
- **Agent token**: a long-lived bearer credential (``mycelium_at_...``)
  for MCP / external automation. Stored as ``sha256`` only; verified
  by a DB lookup. The async helper ``decode_token_async`` accepts
  *either* kind and returns a claims dict shaped like the JWT one
  (``sub``, ``org_id``, ``scope``) so downstream call sites are
  agnostic to credential type. The sync ``decode_token`` keeps its
  JWT-only contract -- changing a sync function to async silently
  would be a footgun.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from mycelium_core.config import get_settings
from mycelium_core.errors import AuthError
from mycelium_core.i18n import MessageCode

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return str(_ph.hash(password))


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _ph.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def create_access_token(*, user_id: str, extra: dict[str, Any] | None = None) -> str:
    s = get_settings()
    now = dt.datetime.now(tz=dt.UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=s.jwt_ttl_seconds)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_alg)


def decode_token(token: str) -> dict[str, Any]:
    """JWT-only decode. Use this where you know the credential is a
    session JWT (SPA bearer path, post-login flows). For surfaces that
    must also accept an agent token, use :func:`decode_token_async`."""
    s = get_settings()
    try:
        decoded: dict[str, Any] = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_alg])
    except jwt.PyJWTError as exc:
        raise AuthError(MessageCode.AUTH_TOKEN_INVALID) from exc
    return decoded


async def decode_token_async(
    raw: str,
    *,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Decode either a session JWT or an agent token (``mycelium_at_...``).

    Branch on the cheap discriminator prefix BEFORE attempting JWT
    decode, so a valid agent token never pays the cost of a failed
    signature check and we do not swallow a real JWT error as
    "probably an agent token".

    Returns a claims dict shaped like the JWT one:

    - ``sub``: user_id (str)
    - ``org_id``: workspace id the token is bound to (str), present
      only for agent tokens (JWTs do not carry an org)
    - ``scope``: capability bucket (str), present only for agent tokens
    - ``typ``: ``"agent"`` for agent tokens, absent for JWTs
    - ``tid``: the agent-token row id (str), present only for agent
      tokens; lets a direct-HTTP caller resolve the token's binding
      without re-authenticating
    - ``assistant_id``: the AI-assistant the token is bound to (str) or
      ``None`` for a bare token; present only for agent tokens. The API
      uses it to attribute an agent's direct-HTTP write (the token-free
      streaming path) to the same identity badge as its MCP-tool writes
    - ``workspace_binding``: ``workspace`` (this credential acts only in
      the workspace it was minted for) or ``account`` (every workspace
      its holder belongs to, decided per request and authorized by their
      membership there); present only for agent tokens

    Raises :class:`mycelium_core.errors.AuthError` on a bad / revoked /
    expired credential, same contract as :func:`decode_token`.
    """
    # Local import to avoid a top-level cycle: the agent-token service
    # imports from ``mycelium_core.models`` which imports from many places.
    from mycelium_core.services.agent_tokens import authenticate, is_agent_token

    if is_agent_token(raw):
        result = await authenticate(raw, session=session)
        if result is None:
            raise AuthError(MessageCode.AGENT_TOKEN_INVALID)
        return {
            "sub": str(result.user_id),
            "org_id": str(result.org_id),
            "scope": result.scope,
            "typ": "agent",
            "tid": str(result.token_id),
            "assistant_id": (str(result.assistant_id) if result.assistant_id else None),
            # The bound assistant's granted scope list, or None for a bare
            # token (= no per-route restriction). Carried so the REST surface
            # can enforce the SAME boundary the MCP gateway does: without it
            # a scoped assistant simply stops speaking MCP and regains full
            # access here (task c19f2f63, enabler B).
            "assistant_scope": result.assistant_scope,
            # Re-read on every request, like the revocation and expiry
            # checks beside it: a credential's tenancy is part of
            # authenticating it, not something a client was told once.
            "workspace_binding": result.workspace_binding.value,
        }
    return decode_token(raw)
