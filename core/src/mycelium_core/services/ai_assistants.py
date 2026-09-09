"""AI assistant CRUD (per-user). The user creates an assistant in the
SPA's /settings/ai-assistants page, gets a one-time URL+secret, and
pastes them into Claude / Cursor / any MCP client.

The bearer credential lives in ``agent_tokens`` (see migration 0056);
this service composes ``mint`` / ``revoke`` from there so an assistant
rotation is a deliberate new-mint + revoke-old pair (auditable in the
ledger, not an in-place secret swap).

Owner-gated on every write — same threshold as agent_tokens mint —
plus an explicit ``ensure_owner`` per row (the assistant lives in the
caller's workspace but the user_id binding is their own user, never
another's).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.concurrency import optimistic_update
from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.mcp_scopes import DEFAULT_SCOPES, SELF_SERVICE_SCOPES, VALID_SCOPE_KEYS
from mycelium_core.models.agent_token import AgentToken, WorkspaceBinding
from mycelium_core.models.ai_assistant import AiAssistant, AssistantRuntime
from mycelium_core.models.membership import Role
from mycelium_core.services import actors as actors_svc
from mycelium_core.services import agent_tokens, audit, identities
from mycelium_core.services.rbac import require_role


@dataclass(frozen=True, slots=True)
class AssistantWithSecret:
    """Returned exactly once at create / rotate time. ``raw_secret``
    travels back to the operator's clipboard and never re-appears."""

    assistant: AiAssistant
    raw_secret: str
    token_prefix: str


def _validate_scope(scope: Sequence[str]) -> list[str]:
    """Coerce a scope list into a clean, de-duplicated subset of the
    catalog. Each key must exist; an unknown key raises with the
    offending value so the SPA can surface it. Empty list = deny-all
    (the assistant exists but can call no tools)."""
    out: list[str] = []
    seen: set[str] = set()
    for key in scope:
        s = str(key).strip()
        if not s:
            continue
        if s not in VALID_SCOPE_KEYS:
            raise DomainError(MessageCode.AI_ASSISTANT_INVALID_SCOPE, key=s)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _is_self_service(scope: Sequence[str]) -> bool:
    """Whether a credential with this scope may be minted, managed and
    revoked by any member for themselves, rather than by the workspace
    owner.

    Minting a long-lived bearer secret is owner-gated, and that is the
    right threshold for an assistant whose scope reaches most of the
    workspace. It is the wrong one for the browser panel: the product
    tells every reader to install it, the panel can do a fixed narrow
    subset of what its holder can already do, and the settings page has
    said "installing this is not an administrative act" while the server
    refused anyone but the owner. One of the two had to become true.

    The test is on the CAPABILITY, so it cannot be talked around: the
    provider string is chosen by the caller and decides nothing, and a
    request for one key outside the set is an assistant again, owner-gated
    as before.
    """
    return set(scope) <= SELF_SERVICE_SCOPES


async def _require_mint_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    scope: Sequence[str],
) -> None:
    """The threshold for creating or holding a credential with this scope."""
    await require_role(
        session,
        org_id,
        actor_id,
        Role.member if _is_self_service(scope) else Role.owner,
    )


async def _binding_of(session: AsyncSession, *, assistant_id: uuid.UUID) -> WorkspaceBinding:
    """The tenancy of the credential that currently stands for this
    assistant. Read from the live token rather than remembered on the
    assistant row, because the token is what a request authenticates
    with and there must be one answer, not two that can disagree."""
    row = (
        await session.execute(
            select(AgentToken.workspace_binding)
            .where(
                AgentToken.assistant_id == assistant_id,
                AgentToken.revoked_at.is_(None),
            )
            .order_by(AgentToken.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row if row is not None else WorkspaceBinding.workspace


async def create_assistant(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    label: str,
    scope: Sequence[str] | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    notes: str | None = None,
    runtime: AssistantRuntime = AssistantRuntime.external,
    workspace_binding: WorkspaceBinding = WorkspaceBinding.workspace,
) -> AssistantWithSecret:
    """Create an assistant + its first agent_token in one atomic flush.
    ``raw_secret`` returned exactly once; the operator pastes it into
    Claude / Cursor and the DB only holds its hash.

    Owner-gated, unless the requested scope is one a member may grant
    themselves (see ``_is_self_service``). The scope is therefore
    validated BEFORE the gate: what is being asked for decides who may
    ask for it."""
    eff_scope = _validate_scope(scope if scope is not None else list(DEFAULT_SCOPES))
    if workspace_binding is WorkspaceBinding.account and not _is_self_service(eff_scope):
        # A credential that reaches every workspace its holder belongs to
        # is only offered for the narrow, fixed set. Wider than that and
        # the reach of the secret stops being proportionate to what it
        # can do with it -- and this is refused rather than quietly
        # narrowed, because a caller that asked for account reach and got
        # workspace reach would look connected and then fail one
        # workspace later.
        extra = sorted(set(eff_scope) - SELF_SERVICE_SCOPES)
        raise DomainError(
            MessageCode.AI_ASSISTANT_BINDING_TOO_WIDE,
            key=", ".join(extra),
        )
    await _require_mint_role(session, org_id, actor_id, eff_scope)
    row = AiAssistant(
        org_id=org_id,
        user_id=actor_id,
        label=label,
        provider=provider,
        model_id=model_id,
        notes=notes,
        scope=eff_scope,
        is_active=True,
        runtime=runtime,
    )
    session.add(row)
    await session.flush()
    # Mint the actor handle and materialise the identity row. The
    # ai_assistant insert trigger (migration 0085) only fires once and
    # short-circuits on the empty handle the row carries at first flush;
    # we mint + ensure_for_ai_assistant explicitly so the assistant is
    # both selectable (picker reads ai_assistants.handle) and
    # assignable (update_task resolves through identities.handle).
    await actors_svc.mint_assistant_handle(session, org_id=org_id, assistant_id=row.id, seed=label)
    await identities.ensure_for_ai_assistant(session, org_id=org_id, assistant_id=row.id)
    mint = await agent_tokens.mint(
        session,
        org_id=org_id,
        actor_id=actor_id,
        name=label,
        assistant_id=row.id,
        workspace_binding=workspace_binding,
        # Already checked, above, against what this credential may do.
        # Passing it again here would let the two thresholds disagree.
        minimum_role=Role.member if _is_self_service(eff_scope) else Role.owner,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="ai_assistant",
        entity_id=row.id,
        action="create",
        diff={"label": label, "scope": eff_scope},
    )
    return AssistantWithSecret(assistant=row, raw_secret=mint.raw, token_prefix=mint.token.prefix)


async def list_assistants(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[AiAssistant]:
    """Per-user listing (RLS already scopes to the workspace; here we
    additionally filter to the actor's own assistants — the UI never
    shows another user's secrets even within the workspace)."""
    result = await session.execute(
        select(AiAssistant)
        .where(AiAssistant.user_id == user_id)
        .order_by(AiAssistant.created_at.desc())
    )
    return list(result.scalars().all())


async def get_assistant(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    assistant_id: uuid.UUID,
) -> AiAssistant:
    row = (
        await session.execute(
            select(AiAssistant).where(
                AiAssistant.id == assistant_id,
                AiAssistant.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.AI_ASSISTANT_NOT_FOUND)
    return row


async def update_assistant(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    assistant_id: uuid.UUID,
    expected_version: int,
    label: str | None = None,
    scope: Sequence[str] | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
    runtime: AssistantRuntime | None = None,
) -> int:
    """Patch an assistant. Optimistic concurrency. The bound token row
    stays unchanged — for a secret rotation use ``rotate_secret``.

    Owner-gated, unless BOTH what the assistant can do today and what it
    is being asked to become are self-service. Checking only the current
    row would make a widening patch the way around the mint threshold."""
    current = await get_assistant(
        session, org_id=org_id, user_id=actor_id, assistant_id=assistant_id
    )
    await _require_mint_role(session, org_id, actor_id, current.scope_list())
    if scope is not None:
        await _require_mint_role(session, org_id, actor_id, _validate_scope(scope))
    values: dict[str, Any] = {}
    if label is not None:
        values["label"] = label
    if scope is not None:
        values["scope"] = _validate_scope(scope)
    if provider is not None:
        values["provider"] = provider
    if model_id is not None:
        values["model_id"] = model_id
    if notes is not None:
        values["notes"] = notes
    if is_active is not None:
        values["is_active"] = is_active
    if runtime is not None:
        values["runtime"] = runtime
    if not values:
        return expected_version
    new_version = await optimistic_update(
        session,
        AiAssistant,
        pk=assistant_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="ai_assistant",
        entity_id=assistant_id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


async def delete_assistant(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    assistant_id: uuid.UUID,
) -> None:
    """Hard-delete. Cascades to its bound agent_tokens (FK ON DELETE
    CASCADE in migration 0059), so the secret is invalidated atomically
    with the row.

    Same threshold as minting, deliberately: a person who may create a
    credential must be able to destroy it. The opposite pairing -- mint
    without revoke -- is how a lost browser stays connected."""
    row = await get_assistant(session, org_id=org_id, user_id=actor_id, assistant_id=assistant_id)
    await _require_mint_role(session, org_id, actor_id, row.scope_list())
    await session.delete(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="ai_assistant",
        entity_id=assistant_id,
        action="delete",
    )


async def rotate_secret(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    assistant_id: uuid.UUID,
) -> AssistantWithSecret:
    """Mint a new agent_token for this assistant and revoke every
    pre-existing one (cleanest audit trail: each rotation is a new
    row + a revoked_at on the old). Returns the fresh raw secret —
    shown exactly once to the operator.

    Same threshold as minting, and the new token inherits the tenancy of
    the one it replaces: a rotation is a new secret for the same
    credential, never a change to what that credential reaches."""
    row = await get_assistant(session, org_id=org_id, user_id=actor_id, assistant_id=assistant_id)
    await _require_mint_role(session, org_id, actor_id, row.scope_list())
    binding = await _binding_of(session, assistant_id=row.id)
    # Mint the new one first so a store failure on the old revoke
    # doesn't leave the assistant credential-less.
    mint = await agent_tokens.mint(
        session,
        org_id=org_id,
        actor_id=actor_id,
        name=row.label,
        assistant_id=row.id,
        workspace_binding=binding,
        minimum_role=Role.member if _is_self_service(row.scope_list()) else Role.owner,
    )
    # Revoke any previous (non-revoked) token for this assistant.
    prev = (
        (
            await session.execute(
                select(AgentToken).where(
                    AgentToken.assistant_id == row.id,
                    AgentToken.id != mint.token.id,
                    AgentToken.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(tz=UTC)
    for old in prev:
        old.revoked_at = now
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="ai_assistant",
        entity_id=row.id,
        action="rotate_secret",
        diff={"revoked": [str(o.id) for o in prev]},
    )
    return AssistantWithSecret(assistant=row, raw_secret=mint.raw, token_prefix=mint.token.prefix)


__all__ = [
    "AssistantWithSecret",
    "create_assistant",
    "delete_assistant",
    "get_assistant",
    "list_assistants",
    "rotate_secret",
    "update_assistant",
]
