"""AI assistants router. Per-user identities an external AI client
(Claude Desktop, Cursor, ...) authenticates with against Mycelium's MCP
surface. Pattern parity with bitvision_phoenix's /ai-assistants.

Thin adapter over ``mycelium_core.services.ai_assistants`` — owner-gated
on every mutation by the service. Per-user scoping is also enforced
in the service (you only see / edit / rotate / delete your own
assistants even within the same workspace).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import (
    AiAssistantCreatedOut,
    AiAssistantCreateIn,
    AiAssistantOut,
    AiAssistantPatchIn,
    ConnectorInfoOut,
    ScopeCatalogEntry,
)
from mycelium_core.mcp_scopes import SCOPE_CATALOG
from mycelium_core.models.agent_token import AgentToken, WorkspaceBinding
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.services import ai_assistants as svc

router = APIRouter(prefix="/ai-assistants", tags=["ai-assistants"])


# --- Connector info -----------------------------------------------------
# The MCP URL the operator pastes into Claude / Cursor. Same-origin as
# the SPA so there is no CORS surface to manage; the API rewrites
# /mcp/* requests to the MCP transport.

_INSTRUCTIONS_MD = """\
1. In Claude (or Cursor / any MCP client), add a **custom MCP server**
   using the streamable-http transport.
2. Set the URL to the value shown above.
3. Set the bearer token to the **client secret** the create dialog
   will reveal (shown only once - copy it now or rotate).
4. Save. The client will call Mycelium's MCP tools on your behalf.
"""


@router.get("/connector-info", response_model=ConnectorInfoOut)
async def connector_info(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> ConnectorInfoOut:
    # Same-origin: SPA at /, API at /api, MCP at /mcp. The frontend
    # prefixes window.location.origin so the operator gets the full
    # public URL (e.g. https://mycelium.xeno.garden/mcp).
    return ConnectorInfoOut(mcp_url="/mcp", instructions_md=_INSTRUCTIONS_MD)


@router.get("/scope-catalog", response_model=list[ScopeCatalogEntry])
async def scope_catalog(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[ScopeCatalogEntry]:
    return [
        ScopeCatalogEntry(key=s.key, category=s.category, label=s.label, description=s.description)
        for s in SCOPE_CATALOG
    ]


async def _latest_token(
    ctx: TenantCtx, assistant_id: uuid.UUID
) -> tuple[str | None, WorkspaceBinding]:
    """The live credential behind this assistant: the first chars of its
    secret (so the UI can tell one rotation from another) and what it
    reaches. Both come from the same row, in one query, because they are
    two facts about one credential and reading them apart is how they
    come to disagree."""
    row = (
        await ctx.session.execute(
            select(AgentToken.prefix, AgentToken.workspace_binding)
            .where(
                AgentToken.assistant_id == assistant_id,
                AgentToken.revoked_at.is_(None),
            )
            .order_by(AgentToken.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None, WorkspaceBinding.workspace
    return row[0], row[1]


def _out(
    a: AiAssistant,
    token_prefix: str | None,
    workspace_binding: WorkspaceBinding = WorkspaceBinding.workspace,
) -> AiAssistantOut:
    return AiAssistantOut(
        id=a.id,
        label=a.label,
        provider=a.provider,
        model_id=a.model_id,
        notes=a.notes,
        scope=a.scope_list(),
        is_active=a.is_active,
        runtime=a.runtime,
        version=a.version,
        created_at=a.created_at,
        updated_at=a.updated_at,
        token_prefix=token_prefix,
        workspace_binding=workspace_binding,
    )


@router.get("", response_model=list[AiAssistantOut])
async def list_assistants(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[AiAssistantOut]:
    rows = await svc.list_assistants(ctx.session, org_id=ctx.org_id, user_id=ctx.user_id)
    out: list[AiAssistantOut] = []
    for a in rows:
        prefix, binding = await _latest_token(ctx, a.id)
        out.append(_out(a, prefix, binding))
    return out


@router.post("", response_model=AiAssistantCreatedOut)
async def create_assistant(
    body: AiAssistantCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AiAssistantCreatedOut:
    res = await svc.create_assistant(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        label=body.label,
        scope=body.scope,
        provider=body.provider,
        model_id=body.model_id,
        notes=body.notes,
        runtime=body.runtime,
        workspace_binding=body.workspace_binding,
    )
    return AiAssistantCreatedOut(
        assistant=_out(res.assistant, res.token_prefix, body.workspace_binding),
        raw_secret=res.raw_secret,
    )


@router.get("/{assistant_id}", response_model=AiAssistantOut)
async def get_assistant(
    assistant_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AiAssistantOut:
    row = await svc.get_assistant(
        ctx.session,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        assistant_id=assistant_id,
    )
    prefix, binding = await _latest_token(ctx, row.id)
    return _out(row, prefix, binding)


@router.patch("/{assistant_id}", response_model=AiAssistantOut)
async def patch_assistant(
    assistant_id: uuid.UUID,
    body: AiAssistantPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AiAssistantOut:
    await svc.update_assistant(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        assistant_id=assistant_id,
        expected_version=body.expected_version,
        label=body.label,
        scope=body.scope,
        provider=body.provider,
        model_id=body.model_id,
        notes=body.notes,
        is_active=body.is_active,
        runtime=body.runtime,
    )
    row = await svc.get_assistant(
        ctx.session,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        assistant_id=assistant_id,
    )
    prefix, binding = await _latest_token(ctx, row.id)
    return _out(row, prefix, binding)


@router.delete("/{assistant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assistant(
    assistant_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.delete_assistant(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        assistant_id=assistant_id,
    )


@router.post("/{assistant_id}/rotate", response_model=AiAssistantCreatedOut)
async def rotate_secret(
    assistant_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AiAssistantCreatedOut:
    res = await svc.rotate_secret(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        assistant_id=assistant_id,
    )
    return AiAssistantCreatedOut(
        assistant=_out(res.assistant, res.token_prefix),
        raw_secret=res.raw_secret,
    )
