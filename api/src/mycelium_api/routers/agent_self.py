"""What this credential is, for the client holding it.

The REST twin of the MCP ``whoami`` META tool, and it exists because a
scoped client that speaks only HTTP had no way to ask.

Without it a client hardcodes the scope list it was minted with. That
list drifts the moment anyone edits the assistant in Settings, and the
drift is silent in the worst direction: the client keeps offering a
control the server will refuse, which is the failure mode of advertising
a capability that does not exist. It is also the only way to tell "my
credential was revoked" from "the network is down" without probing some
unrelated endpoint and interpreting the failure, which is guessing.

DELIBERATELY NOT ``/auth/me``. That route reads the raw user-account row
-- email, avatar, the admin flag -- and stays HUMAN_ONLY, because an
assistant has no reason to touch a person's account. This answers a
different question: not "who is the human", but "what is this credential
and what may it do". The account is named only by the identity the
workspace already publishes to its collaborators.

META in ``route_scopes``: callable under any scope, including the empty
one, because a client cannot ask what it may do if asking is itself
gated. It is still authenticated and still tenant-scoped, so it leaks
nothing to a caller without a valid token, and nothing about a workspace
the caller is not a member of.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from mycelium_api.deps import TenantCtx, current_claims, current_user_id, tenant_ctx
from mycelium_core.db import admin_session
from mycelium_core.models.agent_token import AgentToken, WorkspaceBinding
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.identity import Identity
from mycelium_core.models.organization import Organization
from mycelium_core.services.auth import list_user_orgs

router = APIRouter(prefix="/agent", tags=["meta"])


class SelfWorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    # The caller's effective role for THIS request, after any requested
    # downgrade -- not the membership ceiling. A client gating its
    # controls needs what it may do now.
    role: str


class SelfIdentityOut(BaseModel):
    id: uuid.UUID | None = None
    handle: str | None = None
    kind: str | None = None
    label: str | None = None


class SelfTokenOut(BaseModel):
    # Not a secret: the first characters of the raw value, so a person
    # looking at a list of credentials in Settings can tell which row is
    # the one in front of them.
    prefix: str | None = None
    name: str | None = None
    expires_at: datetime.datetime | None = None


class SelfWorkspacesOut(BaseModel):
    """The workspaces this credential may act in.

    ``binding`` says which rule produced the list, because the two are
    different promises and a client that cannot tell them apart would
    cache the wrong one: ``workspace`` is a credential confined to the
    one it was minted for, ``account`` follows its holder into every
    workspace they belong to -- including one they join tomorrow, which
    is why this is asked rather than remembered."""

    binding: str
    workspaces: list[SelfWorkspaceOut]


class SelfOut(BaseModel):
    workspace: SelfWorkspaceOut
    identity: SelfIdentityOut
    token: SelfTokenOut
    # null = no per-route restriction (a human session or a bare agent
    # token). A LIST, possibly empty, = a bound assistant confined to
    # exactly those keys. The distinction matters to a client: null is
    # "ask the server", an empty list is "you may do nothing".
    scope: list[str] | None = None
    # Which workspaces this credential may act in at all. The panel
    # needs it to decide whether to offer a workspace switcher, and a
    # person reading Settings needs it to know what the secret in their
    # browser reaches.
    workspace_binding: str = WorkspaceBinding.workspace.value


@router.get("/workspaces", response_model=SelfWorkspacesOut)
async def get_agent_workspaces(
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> SelfWorkspacesOut:
    """Where this credential may act.

    Pre-tenant, and it must be: a credential that reaches several
    workspaces cannot put one in ``X-Workspace-Id`` before it knows
    which ones exist for it. Deliberately NOT ``GET /workspaces``, which
    stays HUMAN_ONLY and answers a question about the ACCOUNT (every
    workspace, its status, the switcher's data). This answers the
    narrower one -- what may this credential act on -- and for a
    confined credential the answer is one row, not a list of places it
    cannot go.

    The role on each row is the caller's membership there, which is the
    ceiling this credential is clamped to in that workspace. It is not a
    promise that every operation will succeed: the scope list still
    applies, and both are re-read per request."""
    is_agent = claims.get("typ") == "agent"
    binding = (
        WorkspaceBinding(str(claims.get("workspace_binding", WorkspaceBinding.workspace.value)))
        if is_agent
        else WorkspaceBinding.account
    )
    async with admin_session() as session:
        rows = await list_user_orgs(session, user_id=user_id)
    if is_agent and binding is WorkspaceBinding.workspace:
        # Confined: the one it was minted for, and only if the holder is
        # still a member of it. A credential whose holder left the
        # workspace answers an empty list rather than a row nothing will
        # honour.
        bound = str(claims.get("org_id") or "")
        rows = [r for r in rows if str(r.id) == bound]
    return SelfWorkspacesOut(
        binding=binding.value,
        workspaces=[SelfWorkspaceOut(id=r.id, name=r.name, role=str(r.role)) for r in rows],
    )


@router.get("/self", response_model=SelfOut)
async def get_agent_self(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> SelfOut:
    org = (
        await ctx.session.execute(select(Organization.name).where(Organization.id == ctx.org_id))
    ).scalar_one()

    token_id_raw = claims.get("tid")
    identity = SelfIdentityOut()
    token = SelfTokenOut()
    if token_id_raw:
        # A bound assistant carries handle/label even where its Identity
        # row was never minted, so the assistant is the source and the
        # identity is joined for the id and the handle task assignment
        # keys on. A bare token has no assistant row and falls through to
        # the caller's own identity below.
        row = (
            await ctx.session.execute(
                select(
                    AgentToken.prefix,
                    AgentToken.name,
                    AgentToken.expires_at,
                    AiAssistant.handle,
                    AiAssistant.label,
                    Identity.id,
                    Identity.handle,
                )
                .select_from(AgentToken)
                .outerjoin(AiAssistant, AiAssistant.id == AgentToken.assistant_id)
                .outerjoin(
                    Identity,
                    (Identity.ai_assistant_id == AiAssistant.id) & (Identity.org_id == ctx.org_id),
                )
                .where(AgentToken.id == uuid.UUID(str(token_id_raw)))
            )
        ).first()
        if row is not None:
            prefix, name, expires_at, a_handle, label, ident_id, ident_handle = row
            token = SelfTokenOut(prefix=prefix, name=name, expires_at=expires_at)
            if a_handle is not None:
                identity = SelfIdentityOut(
                    id=ident_id,
                    handle=ident_handle or a_handle,
                    kind="ai_assistant",
                    label=label,
                )

    if identity.handle is None:
        # Human session, or a bare token with no assistant: resolve the
        # caller's own identity read-only. No minting -- asking what you
        # are must not create anything.
        urow = (
            await ctx.session.execute(
                select(Identity.id, Identity.handle, Identity.kind).where(
                    Identity.user_id == ctx.user_id, Identity.org_id == ctx.org_id
                )
            )
        ).first()
        if urow is not None:
            identity = SelfIdentityOut(id=urow[0], handle=urow[1], kind=str(urow[2]))

    raw_scope = claims.get("assistant_scope")
    return SelfOut(
        workspace=SelfWorkspaceOut(id=ctx.org_id, name=org, role=str(ctx.role)),
        identity=identity,
        token=token,
        scope=list(raw_scope) if raw_scope is not None else None,
        workspace_binding=str(claims.get("workspace_binding", WorkspaceBinding.workspace.value)),
    )
