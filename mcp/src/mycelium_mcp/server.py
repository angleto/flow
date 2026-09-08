"""MCP server: thin adapter over mycelium_core, co-equal to the REST API
(docs/adr/0001). Same service layer, RBAC and (org) isolation.

Each tool authenticates with a JWT and an org id, opens a tenant
session (RLS GUCs set) and verifies membership, exactly like the REST
``tenant_ctx`` dependency.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core import __version__
from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import embedder_available
from mycelium_core.errors import AuthError, DomainError, ForbiddenError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.markdown_inline import md_link, md_link_label
from mycelium_core.models.agent_run import AgentRun
from mycelium_core.models.billing import CostBasis, RateCard, UsageRecord
from mycelium_core.models.budget import Budget, BudgetPeriod
from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.dependency import DependencyType, TaskDependency
from mycelium_core.models.dispatch_request import DispatchRequest
from mycelium_core.models.email import (
    EmailAccount,
    EmailMessage,
    EmailProvider,
    EmailResponderJob,
)
from mycelium_core.models.executor import Executor, ExecutorKind
from mycelium_core.models.index_scope import IndexScope
from mycelium_core.models.invoice import Invoice, InvoiceLine, InvoiceLineAltriDati, InvoiceState
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.models.note import Note, NoteKind, NoteTurn
from mycelium_core.models.notification import NotificationChannelKind, RecurrenceFreq
from mycelium_core.models.project_profile import ProjectProfile
from mycelium_core.models.schedule import Schedule
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.models.task import (
    ConstraintKind,
    Necessity,
    ScheduleMode,
    SchedulePolicy,
    Task,
)
from mycelium_core.models.task_handoff import TaskHandoff
from mycelium_core.models.time_entry import TimeEntry
from mycelium_core.models.user import User
from mycelium_core.models.workflow import WorkflowDefinition, WorkflowState, WorkflowTransition
from mycelium_core.security import decode_token_async
from mycelium_core.services import advisory as advisory_svc
from mycelium_core.services import agent_runtime as agent_runtime_svc
from mycelium_core.services import annotations as annotations_svc
from mycelium_core.services import attachments as attachments_svc
from mycelium_core.services import billing as billing_svc
from mycelium_core.services import budgets as budgets_svc
from mycelium_core.services import calendar as calendars
from mycelium_core.services import candidates as candidates_svc
from mycelium_core.services import coordination as coordination_svc
from mycelium_core.services import decomposition as decomposition_svc
from mycelium_core.services import dependencies, scheduler, tasks, taxonomy
from mycelium_core.services import dispatch_loop as dispatch_loop_svc
from mycelium_core.services import email as email_svc
from mycelium_core.services import embedding_migration as embedding_svc
from mycelium_core.services import entity_revisions as revisions_svc
from mycelium_core.services import event_bus as event_bus_svc
from mycelium_core.services import executors as executors_svc
from mycelium_core.services import focus_context as focus_context_svc
from mycelium_core.services import garden_classify as garden_classify_svc
from mycelium_core.services import garden_review as garden_review_svc
from mycelium_core.services import invoice as invoice_svc
from mycelium_core.services import invoice_format as invoice_format_svc
from mycelium_core.services import kg as kg_svc
from mycelium_core.services import link_prediction as link_prediction_svc
from mycelium_core.services import lookup as lookup_svc
from mycelium_core.services import memory as memory_svc
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services import note_parts as note_parts_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import notifications as notif_svc
from mycelium_core.services import participants as part_svc
from mycelium_core.services import task_checklist as checklist_svc
from mycelium_core.services import task_relations as task_relations_svc
from mycelium_core.services import task_search as task_search_svc
from mycelium_core.services import time_tracking as time_svc
from mycelium_core.services import workflow as workflow_svc
from mycelium_core.services.rbac import get_role
from mycelium_core.services.taxonomy import ClientInput
from mycelium_core.services.time_tracking import ReportGroup
from mycelium_core.services.workflow import StateEdit, StateSpec
from mycelium_core.timewindow import resolve_tz, split_due
from mycelium_mcp.tool_scopes import DYNAMIC_TOOL_SCOPES, TOOL_SCOPES, required_keys

_INSTRUCTIONS = (
    "You are working through Mycelium, a shared work hub that is ALSO your durable, "
    "portable memory: it persists across machines and MCP clients, so agents coordinate "
    "through it rather than through machine-local files. AT SESSION START call whoami() "
    "-- it returns your identity and granted scope, your open assigned tasks, and a "
    "recall of your durable memory lane, so you resume from Mycelium instead of a local "
    "file. Your DURABLE, shareable memory lives here: write facts/decisions/an index with "
    "memory_write on channel 'agent'; durable PROCEDURES live as protected notes. Your "
    "EPHEMERAL working memory stays with you, the caller (ADR-0049) -- do not store "
    "scratch/session state here. NEVER write secrets, credentials, production/operator "
    "runbooks, deploy commands or personal data into this shared store (it is org+project "
    "shared, without per-actor read isolation): keep those machine-local. Discover tools "
    "with search_tools(query); read platform docs with help(topic). The human steers and "
    "gives direction; agents execute and PROPOSE, never impose."
)

mcp: FastMCP = FastMCP("mycelium", instructions=_INSTRUCTIONS)


# Per-request principal published by the HTTP transport's bearer
# middleware (``server_http.py``). When set, ``_tenant`` reads
# ``(user_id, org_id, token_id)`` from here and skips both the JWT
# decode and the claims/positional org resolution — the bearer was
# already validated at the HTTP boundary via
# ``authenticate_agent_token`` (migration 0059) so the principal is
# trustworthy. The third element is the ``agent_tokens.id`` of the
# token used (always populated under HTTP; the bearer must be a
# ``mycelium_at_…`` agent token). ``None`` for the stdio transport, which
# keeps using the legacy positional-args flow with a plain JWT.
_PRINCIPAL: ContextVar[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None] | None] = ContextVar(
    "_PRINCIPAL", default=None
)

# The bound assistant's granted scope list, published by the HTTP bearer
# middleware alongside ``_PRINCIPAL`` (task c19f2f63, enabler B). ``None`` for a
# bare token / the stdio / human-bearer paths = legacy full access. Kept as a
# separate ContextVar (not folded into ``_PRINCIPAL``) so the many existing
# 3-tuple readers/setters -- tests, eval_scenarios -- are untouched, and so the
# gateway scope gate filters ~250 tools from memory with no DB round-trip.
_PRINCIPAL_SCOPE: ContextVar[list[str] | None] = ContextVar("_PRINCIPAL_SCOPE", default=None)


@asynccontextmanager
async def _tenant(
    token: str = "", org_id: str = ""
) -> AsyncIterator[tuple[AsyncSession, uuid.UUID, uuid.UUID]]:
    """Open a tenant session for the duration of the call.

    Two code paths:
    - HTTP transport: the bearer middleware in ``server_http`` populated
      ``_PRINCIPAL`` with the authenticated ``(user_id, org_id,
      token_id)``; the agent-token nature lets us tag the audit log
      as ``actor_kind='mcp_token'`` with ``actor_subject_id`` = token id.
    - stdio transport (legacy): the caller passes a session JWT (a
      human's bearer used outside the SPA) plus ``org_id`` as
      positional args; tagged ``actor_kind='human_api'``.
    """
    principal = _PRINCIPAL.get()
    if principal is not None:
        user_id, org, token_id = principal
        actor_kind = "mcp_token"
        actor_subject = str(token_id) if token_id is not None else None
    else:
        claims = await decode_token_async(token)
        sub = claims.get("sub")
        if not isinstance(sub, str):
            raise AuthError(MessageCode.AUTH_TOKEN_NO_SUB)
        user_id = uuid.UUID(sub)
        token_org = claims.get("org_id")
        if isinstance(token_org, str) and token_org:
            org = uuid.UUID(token_org)
        else:
            org = uuid.UUID(org_id)
        actor_kind = "human_api"
        actor_subject = None
    async with tenant_session(
        str(org),
        str(user_id),
        actor_kind=actor_kind,
        actor_subject_id=actor_subject,
    ) as session:
        await get_role(session, org, user_id)  # raises if not a member
        yield session, org, user_id


async def _resolve_agent_context(
    session: AsyncSession, org: uuid.UUID
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Return ``(ai_assistant_identity_id, agent_token_id)`` for the
    current MCP call, or ``(None, None)`` for stdio / human-bearer
    requests. The identity is resolved through
    ``agent_tokens.assistant_id -> ai_assistants -> identities`` and
    is therefore ``None`` for bare tokens (assistant_id IS NULL,
    pre-migration 0059). The token id is returned in both cases so
    AI authorship survives a bare token too — ``agent_tokens.name``
    becomes the display label in the serializer (migration 0093)."""
    principal = _PRINCIPAL.get()
    if principal is None:
        return None, None
    _user_id, _org, token_id = principal
    if token_id is None:
        return None, None
    from sqlalchemy import select as _sel

    from mycelium_core.models.agent_token import AgentToken
    from mycelium_core.models.ai_assistant import AiAssistant
    from mycelium_core.models.identity import Identity

    row = await session.execute(
        _sel(Identity.id)
        .join(AiAssistant, AiAssistant.id == Identity.ai_assistant_id)
        .join(AgentToken, AgentToken.assistant_id == AiAssistant.id)
        .where(
            AgentToken.id == token_id,
            Identity.org_id == org,
        )
    )
    identity_id = row.scalar_one_or_none()
    return identity_id, token_id


async def _require_scope(session: AsyncSession, scope_key: str) -> None:
    """Reject an assistant-scoped MCP call that lacks ``scope_key``.

    The per-tool scope gate (the deferred item in ``mcp_scopes.py``), wired here
    for the invoice tools. Least-authority default: it binds ONLY when the call
    is an HTTP agent-token request bound to an ``ai_assistants`` row that carries
    a scope list. A legacy bare token (``assistant_id IS NULL``) and the stdio /
    human-bearer paths keep their previous full access, so nothing existing
    breaks; a scoped assistant is denied any tool outside its ``scope``."""
    principal = _PRINCIPAL.get()
    if principal is None:
        return
    _user_id, _org, token_id = principal
    if token_id is None:
        return
    from sqlalchemy import select as _sel

    from mycelium_core.models.agent_token import AgentToken
    from mycelium_core.models.ai_assistant import AiAssistant

    scope = (
        await session.execute(
            _sel(AiAssistant.scope)
            .join(AgentToken, AgentToken.assistant_id == AiAssistant.id)
            .where(AgentToken.id == token_id)
        )
    ).scalar_one_or_none()
    if scope is None:
        return  # bare token (no bound assistant) -> legacy full access
    if scope_key not in (scope or []):
        raise ForbiddenError(MessageCode.MCP_SCOPE_DENIED, scope=scope_key)


def _scope_permits(tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
    """Whether the current principal may invoke ``tool_name`` (task c19f2f63,
    enabler B). Pure/in-memory: reads the scope list published into
    ``_PRINCIPAL_SCOPE`` by the HTTP bearer middleware.

    - ``_PRINCIPAL_SCOPE`` is None -> a bare token / the stdio / human-bearer
      path: legacy full access, everything allowed.
    - the tool maps to ``None`` in ``TOOL_SCOPES`` -> META (whoami/help/ping):
      always allowed so an agent can bootstrap and discover its own scope.
    - the tool is absent from both maps -> FAIL-CLOSED, denied (a drift-guard
      test keeps them jointly total so this never bites a real tool).
    - otherwise the granted list must contain the tool's required key, or
      ANY ONE of them where the entry is a transitional any-of set.

    ``arguments`` distinguishes two contexts for an argument-dependent tool
    (``DYNAMIC_TOOL_SCOPES``):
    - ``None`` = a LISTING check (search_tools / describe_tools): the tool is
      permitted if the assistant holds ANY scope it could require, so a
      kind-multiplexing tool stays discoverable for the kinds it may use.
    - a dict (the concrete call, possibly empty) = ENFORCEMENT: the required
      scope is resolved from those arguments; an indeterminate kind fails
      closed. Static tools ignore ``arguments`` entirely."""
    scope = _PRINCIPAL_SCOPE.get()
    if scope is None:
        return True
    dyn = DYNAMIC_TOOL_SCOPES.get(tool_name)
    if dyn is not None:
        resolver, possible = dyn
        if arguments is None:
            return bool(possible & set(scope))
        req_dyn = resolver(arguments)
        return req_dyn is not None and req_dyn in scope
    if tool_name not in TOOL_SCOPES:
        return False  # UNMAPPED, and not dynamic either: fail closed
    req = required_keys(tool_name)
    if req is None:
        return True  # META
    # A set means ANY ONE of these keys is enough. A single-key entry
    # normalises to a one-element set, so there is one comparison here
    # rather than a branch per value shape.
    return bool(req & set(scope))


@mcp.tool()
def ping() -> str:
    """Liveness probe; returns the mycelium-core version."""
    return f"mycelium-core {__version__}"


_OVERVIEW = (
    "Mycelium is a multi-tenant personal work hub: tasks with dependency-aware "
    "scheduling, time tracking and billing, notes with a knowledge graph, a "
    "client/project taxonomy, workflows, email and calendar, and Italian "
    "electronic invoicing (FatturaPA / SdI). The MCP surface is co-equal to the "
    "web GUI over one service layer (ADR-0001). Configuration is via MYCELIUM_* "
    "environment variables (see the 'configuration' field). Design and feature "
    "docs are listed under 'doc_topics' -- call help('<topic>') for a document's "
    "full text; use search_tools(query) to find a tool for a task; the REST API "
    "reference is at /apidocs."
)


def _docs_dir() -> Path | None:
    """Locate the maintained ``docs/`` directory: an explicit ``MYCELIUM_DOCS_DIR``
    override, else walk up from this module (repo root in dev, ``/app`` in the
    backend image, whose Dockerfile ships ``docs/``). ``None`` when absent, so
    ``help`` degrades to the derived config reference."""
    import os

    override = os.environ.get("MYCELIUM_DOCS_DIR")
    if override and Path(override).is_dir():
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        cand = parent / "docs"
        if (cand / "functional-requirements.md").is_file():
            return cand
    return None


def _config_reference() -> list[dict[str, Any]]:
    """The environment-variable configuration reference, DERIVED from the
    Settings model so it never drifts from the code. Only names + non-secret
    defaults are exposed: a required secret (jwt_secret, secret_key,
    issuer_key_pepper, the DB URLs) has no default, so no secret value leaks."""
    from mycelium_core.config import Settings

    rows: list[dict[str, Any]] = []
    for name, field in Settings.model_fields.items():
        required = field.is_required()
        default: Any = None
        if not required:
            try:
                default = field.get_default(call_default_factory=True)
            except Exception:
                default = None
        if not isinstance(default, str | int | float | bool | None):
            default = str(default)
        rows.append(
            {
                "env": f"MYCELIUM_{name.upper()}",
                "required": required,
                "default": default,
                "description": field.description,
            }
        )
    return rows


@mcp.tool()
def help(topic: str | None = None) -> dict[str, Any]:
    """Answer questions about Mycelium ITSELF -- its features, configuration and
    documentation. Call with NO ``topic`` for an overview + the full
    environment-variable configuration reference + the list of documentation
    topics. Call with a ``topic`` ('invoicing', 'architecture', 'data-model',
    'functional-requirements', 'mcp-coverage', or any ``docs/`` filename; or
    'configuration') to get that document's full text. To discover the available
    TOOLS use ``search_tools``; the REST API reference is served at ``/apidocs``.
    Read-only, no workspace needed."""
    import difflib

    docs = _docs_dir()
    names = sorted(p.stem for p in docs.glob("*.md")) if docs else []
    # Hide the many numbered ADRs from the top-level index; still fetchable by
    # their exact name (e.g. help('0045-issuer-scoped-api-keys')).
    topics = [n for n in names if not n[:1].isdigit()]

    if topic:
        key = topic.strip().lower().replace(" ", "-")
        if key in ("config", "configuration", "settings", "env"):
            return {"topic": "configuration", "config": _config_reference()}
        if docs:
            lower = {n.lower(): n for n in names}
            match = lower.get(key)
            if match is None:
                close = difflib.get_close_matches(key, list(lower), n=1, cutoff=0.6)
                match = lower[close[0]] if close else None
            if match:
                return {
                    "topic": match,
                    "content": (docs / f"{match}.md").read_text(encoding="utf-8"),
                }
            # No filename match: fall back to a content search so an arbitrary
            # keyword ('invoicing', 'scheduling', ...) still returns the most
            # relevant document plus the other docs that mention it.
            term = topic.strip().lower()
            scored: list[tuple[int, str, str]] = []
            for n in names:
                try:
                    text = (docs / f"{n}.md").read_text(encoding="utf-8")
                except OSError:
                    continue
                hits = text.lower().count(term)
                if hits:
                    scored.append((hits, n, text))
            if scored:
                scored.sort(key=lambda s: (-s[0], s[1]))
                return {
                    "topic": scored[0][1],
                    "content": scored[0][2],
                    "also_matching": [n for _, n, _ in scored[1:6]],
                }
        return {
            "error": f"no document matches '{topic}'",
            "doc_topics": topics,
            "hint": "call help() with no topic for the index, or help('configuration')",
        }

    return {
        "overview": _OVERVIEW,
        "configuration": _config_reference(),
        "doc_topics": topics,
        "pointers": {
            "tools": "use search_tools(query) to find an MCP tool for a task",
            "rest_api": "the REST API reference (OpenAPI) is served at /apidocs",
        },
    }


@mcp.tool()
async def whoami(token: str = "", org_id: str = "") -> dict[str, Any]:
    """Session bootstrap: who am I, what may I do, and my durable memory here.
    Call this FIRST at session start (especially an external LLM / MCP client).
    Read-only. Returns your resolved identity + granted scope (``scope`` null =
    full access, no per-tool restriction); your OPEN assigned tasks; and a recall
    of your durable memory lane (channel 'agent'), so you resume from Mycelium
    instead of a machine-local file. Durable/shareable memory lives here (write it
    with ``memory_write`` on channel 'agent'; durable procedures as protected
    notes); ephemeral working memory stays with you, the caller (ADR-0049). Use
    ``search_tools(query)`` to find tools and ``help(topic)`` for docs."""
    from sqlalchemy import select as _sel

    from mycelium_core.models.agent_token import AgentToken
    from mycelium_core.models.ai_assistant import AiAssistant
    from mycelium_core.models.identity import Identity

    async with _tenant(token, org_id) as (s, org, user):
        principal = _PRINCIPAL.get()
        token_id = principal[2] if principal is not None else None
        handle: str | None = None
        identity_id: uuid.UUID | None = None
        kind: Any = None
        label: str | None = None
        scope: list[str] | None = None
        if token_id is not None:
            # Bound agent token -> the assistant carries handle/label/scope even
            # if its Identity row was never minted; LEFT JOIN Identity for the id
            # (and its handle, which is the one task assignment keys on). A legacy
            # bare token (assistant_id IS NULL) yields no row -> the fallback runs.
            row = (
                await s.execute(
                    _sel(
                        AiAssistant.handle,
                        AiAssistant.label,
                        AiAssistant.scope,
                        Identity.id,
                        Identity.handle,
                    )
                    .select_from(AgentToken)
                    .join(AiAssistant, AiAssistant.id == AgentToken.assistant_id)
                    .outerjoin(
                        Identity,
                        (Identity.ai_assistant_id == AiAssistant.id) & (Identity.org_id == org),
                    )
                    .where(AgentToken.id == token_id)
                )
            ).first()
            if row is not None:
                a_handle, label, raw_scope, identity_id, ident_handle = row
                handle = ident_handle or a_handle
                kind = "ai_assistant"
                scope = list(raw_scope) if raw_scope is not None else None
        if handle is None:
            # Human / stdio / legacy bare token: resolve the caller's own identity
            # read-only (no minting), so whoami stays useful on those paths too.
            urow = (
                await s.execute(
                    _sel(Identity.handle, Identity.id, Identity.kind).where(
                        Identity.user_id == user, Identity.org_id == org
                    )
                )
            ).first()
            if urow is not None:
                handle, identity_id, kind = urow

        # ``whoami`` is META -- callable under ANY scope, so an agent can always
        # discover its own identity. That exemption must not become a data hole:
        # the two enrichments below return the SAME payloads as ``list_tasks``
        # and ``memory_search`` (task rows; full memory-blob text), which a
        # narrowly-scoped assistant may not hold. So gate each enrichment on the
        # scope its equivalent tool requires, and say so when withholding.
        # Identity / granted scope / pointers stay unconditional: without them
        # bootstrap is impossible. (Adversarial audit of enabler B.)
        withheld: list[str] = []

        open_tasks: list[dict[str, Any]] = []
        if handle is not None:
            if _scope_permits("list_tasks"):
                rows = await tasks.list_tasks(
                    s,
                    org_id=org,
                    assignee_handles=[handle],
                    open_only=True,
                    with_description=False,
                    limit=10,
                )
                statemap = await workflow_svc.states_by_ids(s, [t.state_id for t in rows])
                open_tasks = [_task(t, state=statemap.get(t.state_id)) for t in rows]
            else:
                withheld.append("open_tasks: needs the scope that list_tasks requires")

        # Recall the agent's own durable lane (channel 'agent'). Best-effort: a
        # bootstrap must never fail because memory is momentarily unavailable or
        # the channel is not yet seeded.
        memory_recall: list[dict[str, Any]] = []
        if not _scope_permits("memory_search"):
            withheld.append("memory_lane.recall: needs the scope that memory_search requires")
        else:
            try:
                hits, _meta = await memory_svc.retrieve_with_meta(
                    s,
                    org_id=org,
                    actor_id=user,
                    project_id=None,
                    query=f"{handle or 'agent'} memory index protocol operational state",
                    operation_id=f"whoami:{token_id or user}",
                    limit=5,
                    channel_key="agent",
                )
                tagmap = await memory_svc.tags_by_blob(s, blob_ids=[h.blob.id for h in hits])
                memory_recall = [
                    {"blob": _blob(h.blob, tagmap.get(h.blob.id)), "rrf": h.rrf} for h in hits
                ]
            except Exception:
                memory_recall = []

        return {
            "identity": {
                "handle": handle,
                "identity_id": str(identity_id) if identity_id else None,
                "kind": kind.value if hasattr(kind, "value") else kind,
                "assistant_label": label,
                "user_id": str(user),
            },
            "scope": scope,
            "org_id": str(org),
            # Non-empty when this assistant's scope was too narrow for one of
            # the enrichments: an empty list then means "not permitted", not
            # "nothing there".
            "withheld": withheld,
            "open_tasks": open_tasks,
            "memory_lane": {
                "channel": "agent",
                "recall": memory_recall,
                "write_hint": (
                    "memory_write(text, operation_id, channel_key='agent', "
                    "tag_ids=[<your 'agent/<handle>' tag>], namespace='agent')"
                ),
            },
            "pointers": {
                "protocol": (
                    "search for the 'Protocollo agente mycelium' protocol note (how to work here)"
                ),
                "tools": "search_tools(query) to find an MCP tool",
                "docs": "help(topic) for platform documentation",
            },
        }


def _tag(t: Tag) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "kind": t.kind.value,
        "name": t.name,
        "version": t.version,
    }


def _tag_brief(t: Tag) -> dict[str, Any]:
    """Tag chip as carried on a task/note (mirrors the REST TagBrief):
    id/kind/name/color, no version. Lets MCP callers see and reason
    about a task's tags without a follow-up list_tags round-trip."""
    return {
        "id": str(t.id),
        "kind": t.kind.value,
        "name": t.name,
        "color": t.color,
    }


def _project_fields(d: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    """Project a record dict to a subset of fields. None = no projection.

    ``id`` is always kept (callers need the stable handle for follow-up
    calls); unknown field names are silently ignored. Used by list_*
    tools so an LLM picker can ask only for ``[id, title]`` instead of
    the full record.
    """
    if fields is None:
        return d
    keep = set(fields)
    keep.add("id")
    return {k: v for k, v in d.items() if k in keep}


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is ``None`` from a serialized record.

    Used by the read serializers (``_task_full`` / ``_client`` /
    ``_project``) so an unset nullable column costs zero tokens instead
    of an explicit ``"key": null``. Only ``None`` is dropped: ``False``,
    ``0``, ``""`` and ``[]`` are real values and are kept. An absent key
    in these shapes therefore reads as "not set", which matches the
    column semantics; a caller that needs the exhaustive key set (e.g. to
    diff against a write) reads it from the typed REST ``*Out`` schema,
    which is unaffected by this projection."""
    return {k: v for k, v in d.items() if v is not None}


def _task(
    t: Task,
    tags: list[Tag] | None = None,
    *,
    collaborators_count: int = 0,
    state: WorkflowState | None = None,
) -> dict[str, Any]:
    # Lean index shape for ``list_tasks`` / ``create_task`` returns: just
    # the fields an LLM needs to pick a row, plus ``version`` for a
    # follow-up optimistic-concurrency update. Full detail (description,
    # dates, estimates, capabilities, ...) is on ``get_task`` (_task_full);
    # widen a list row with the ``fields`` projection when needed. Task
    # authorship (``created_by_*``) lives on the REST ``TaskOut`` serializer
    # (the SPA's source, used for the AI badge), not here: it is debug-only
    # over MCP and was dropped from the list shape for payload economy.
    #
    # Enriched (task eb874772) with the planning axes an agent needs to
    # triage a row without a follow-up ``get_task``: importance/urgency
    # (Eisenhower), necessity (MoSCoW), start/due dates and parent. Wrapped
    # in ``_compact`` so the sparse fields (dates/parent/assignee/owner)
    # cost zero tokens when unset; the always-present axes
    # (importance/urgency/necessity) stay. ``fields=[...]`` still narrows it.
    return _compact(
        {
            "id": str(t.id),
            "title": t.title,
            "state_id": str(t.state_id),
            # Resolved state (name + its workflow), so an agent reads a task's
            # state directly instead of a bare uuid or inferring the state set
            # from other tasks. workflow_id lets it then call workflow_states /
            # workflow_transitions to see the whole machine.
            "state": state.name if state is not None else None,
            "state_is_terminal": state.is_terminal if state is not None else None,
            "workflow_id": str(state.workflow_id) if state is not None else None,
            "priority": t.priority,
            "importance": t.importance,
            "urgency": t.urgency,
            "necessity": t.necessity.value if t.necessity else None,
            # Only the exception is worth a key on the lean row: the column
            # is NOT NULL and 'org' on nearly every task, and _compact drops
            # None. Absent here means 'org'.
            "index_scope": (t.index_scope.value if t.index_scope != IndexScope.org else None),
            "start_date": t.start_date.isoformat() if t.start_date else None,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "version": t.version,
            "tags": [_tag_brief(g) for g in (tags or [])],
            "assignee_id": str(t.assignee_id) if t.assignee_id else None,
            "owner_id": str(t.owner_id) if t.owner_id else None,
            "collaborators_count": collaborators_count,
        }
    )


async def _tag_of_kind(s: AsyncSession, org: uuid.UUID, tag_id: str, kind: TagKind) -> uuid.UUID:
    """Parse a tag id argument and refuse it when it is not of ``kind``.

    Each single-valued tool below (``move_*_to_project`` /
    ``set_*_client``) stands for ONE structural operation, while the
    service door it delegates to dispatches on the tag's kind: without
    this check a project id handed to ``set_task_client`` would quietly
    perform the OTHER operation and the tool would report a client
    change that never happened. TAG_NOT_FOUND for an unknown or
    foreign-workspace id comes from ``taxonomy.get_tag``."""
    tag = await taxonomy.get_tag(s, org_id=org, tag_id=uuid.UUID(tag_id))
    if tag.kind is not kind:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    return tag.id


@mcp.tool()
async def create_tag(
    token: str, org_id: str, kind: str, name: str, color: str | None = None
) -> dict[str, Any]:
    """Create a free-form tag (kind: generic). A client or a project
    cannot be created here: each carries a typed profile (the invoicing
    card, the project's owning client) that only ``create_client`` /
    ``create_project`` write, so one born through this door would have
    no profile row at all -- see docs/adr/0003. Memory channels are a
    seeded vocabulary (``memory_channel_create``)."""
    async with _tenant(token, org_id) as (s, org, user):
        tag = await taxonomy.create_tag(
            s,
            org_id=org,
            actor_id=user,
            kind=TagKind(kind),
            name=name,
            color=color,
        )
        return _tag(tag)


@mcp.tool()
async def create_client(
    token: str,
    org_id: str,
    name: str,
    legal_name: str,
    country_code: str | None = None,
    vat_number: str | None = None,
) -> dict[str, Any]:
    """Create a client tag with its typed profile."""
    async with _tenant(token, org_id) as (s, org, user):
        tag = await taxonomy.create_client(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            profile=ClientInput(
                legal_name=legal_name,
                country_code=country_code,
                vat_number=vat_number,
            ),
        )
        return _tag(tag)


@mcp.tool()
async def create_project(
    token: str,
    org_id: str,
    name: str,
    client_tag_id: str | None = None,
) -> dict[str, Any]:
    """Create a project under a client. EVERY project has exactly one
    client (docs/adr/0003): omitting ``client_tag_id`` files it under
    the workspace default client ("Personal"), it does not create a
    clientless project. Rate/billable are client-level (set on the
    client); the project carries name/budget/description."""
    async with _tenant(token, org_id) as (s, org, user):
        tag = await taxonomy.create_project(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            client_tag_id=(uuid.UUID(client_tag_id) if client_tag_id else None),
        )
        return _tag(tag)


@mcp.tool()
async def list_tags(token: str, org_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    """List tags, optionally filtered by kind."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await taxonomy.list_tags(s, org_id=org, kind=TagKind(kind) if kind else None)
        return [_tag(t) for t in rows]


def _client(t: Tag, p: ClientProfile) -> dict[str, Any]:
    # Unset invoicing-card fields (tax_code, pec, ...) are dropped
    # rather than emitted as null: a sparsely-filled client carries only
    # its real columns. The exhaustive key set lives on the REST schema.
    return _compact(
        {
            "id": str(t.id),
            "name": t.name,
            "status": t.status,
            "version": t.version,
            "legal_name": p.legal_name,
            "country_code": p.country_code,
            "vat_number": p.vat_number,
            "tax_code": p.tax_code,
            "address": p.address,
            "postal_code": p.postal_code,
            "city": p.city,
            "province": p.province,
            "country": p.country,
            "sdi_code": p.sdi_code,
            "pec": p.pec,
            "description": p.description,
            "default_billable": p.default_billable,
            "hourly_rate": str(p.hourly_rate) if p.hourly_rate is not None else None,
            "currency": p.currency,
        }
    )


def _project(t: Tag, p: ProjectProfile) -> dict[str, Any]:
    return _compact(
        {
            "id": str(t.id),
            "name": t.name,
            "status": t.status,
            "version": t.version,
            "client_tag_id": str(p.client_tag_id) if p.client_tag_id else None,
            "budget": str(p.budget) if p.budget is not None else None,
            "color": t.color,
            "description": p.description,
            "workflow_id": str(p.workflow_id) if p.workflow_id else None,
        }
    )


@mcp.tool()
async def list_clients(
    token: str, org_id: str, include_archived: bool = False
) -> list[dict[str, Any]]:
    """List clients with their invoicing profile.

    Archived clients are excluded by default: an archived client is one
    the workspace stopped working with, and offering it back to an agent
    is the same mistake as offering it in a dropdown. Pass
    ``include_archived`` to resolve a historical reference.
    """
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await taxonomy.list_clients(s, org_id=org, include_archived=include_archived)
        return [_client(t, p) for t, p in rows]


@mcp.tool()
async def list_projects(
    token: str, org_id: str, include_archived: bool = False
) -> list[dict[str, Any]]:
    """List projects with their profile (client link, budget, color).

    Archived projects are excluded by default; ``include_archived``
    brings them back for a historical lookup.
    """
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await taxonomy.list_projects(s, org_id=org, include_archived=include_archived)
        return [_project(t, p) for t, p in rows]


@mcp.tool()
async def get_tag(token: str, org_id: str, tag_id: str) -> dict[str, Any]:
    """Read one tag (generic/client/project)."""
    async with _tenant(token, org_id) as (s, org, _user):
        t = await taxonomy.get_tag(s, org_id=org, tag_id=uuid.UUID(tag_id))
        return _tag(t)


@mcp.tool()
async def update_tag(
    token: str,
    org_id: str,
    tag_id: str,
    expected_version: int,
    name: str | None = None,
    color: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Rename / recolor / set status of a tag (status: active|archived)."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await taxonomy.update_tag(
            s,
            org_id=org,
            actor_id=user,
            tag_id=uuid.UUID(tag_id),
            expected_version=expected_version,
            name=name,
            color=color,
            status=status,
        )
        return {"tag_id": tag_id, "version": version}


@mcp.tool()
async def update_client(
    token: str,
    org_id: str,
    tag_id: str,
    name: str | None = None,
    legal_name: str | None = None,
    country_code: str | None = None,
    vat_number: str | None = None,
    tax_code: str | None = None,
    address: str | None = None,
    postal_code: str | None = None,
    city: str | None = None,
    province: str | None = None,
    country: str | None = None,
    sdi_code: str | None = None,
    pec: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Edit a client's name and invoicing card. Only the given fields
    are changed."""
    # Widened to match the taxonomy.update_client signature (dict is
    # invariant: dict[str, str | None] is not a subtype of dict[str,
    # object]). The MCP tool still only exposes string fields, so the
    # widened type is over-permissive in this caller — that's fine.
    fields: dict[str, object] = {}
    for key, val in (
        ("legal_name", legal_name),
        ("country_code", country_code),
        ("vat_number", vat_number),
        ("tax_code", tax_code),
        ("address", address),
        ("postal_code", postal_code),
        ("city", city),
        ("province", province),
        ("country", country),
        ("sdi_code", sdi_code),
        ("pec", pec),
        ("description", description),
    ):
        if val is not None:
            fields[key] = val
    async with _tenant(token, org_id) as (s, org, user):
        await taxonomy.update_client(
            s,
            org_id=org,
            actor_id=user,
            tag_id=uuid.UUID(tag_id),
            name=name,
            fields=fields,
        )
        return {"tag_id": tag_id, "updated": True}


@mcp.tool()
async def update_project(
    token: str,
    org_id: str,
    tag_id: str,
    name: str | None = None,
    client_tag_id: str | None = None,
    budget: float | None = None,
    color: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Edit a project. Only the given fields are changed; a project can
    never be left clientless, so ``client_tag_id`` omitted means "keep
    the current client" and a null is refused. Passing a DIFFERENT
    client reassigns the project and synchronously re-tags every task
    and note carrying it onto that client, in the same transaction: an
    entity carrying a project always carries THAT project's client
    (docs/adr/0003, taxonomy.reassign_project_client). Admin."""
    fields: dict[str, object] = {}
    if client_tag_id is not None:
        fields["client_tag_id"] = uuid.UUID(client_tag_id)
    if budget is not None:
        fields["budget"] = Decimal(str(budget))
    if color is not None:
        fields["color"] = color
    if description is not None:
        fields["description"] = description
    async with _tenant(token, org_id) as (s, org, user):
        await taxonomy.update_project(
            s,
            org_id=org,
            actor_id=user,
            tag_id=uuid.UUID(tag_id),
            name=name,
            fields=fields,
        )
        return {"tag_id": tag_id, "updated": True}


@mcp.tool()
async def set_tag_scope(
    token: str, org_id: str, tag_id: str, target_ids: list[str]
) -> dict[str, Any]:
    """Replace a tag's scope with the given project/client tag ids
    (empty list = global / visible everywhere). Admin."""
    async with _tenant(token, org_id) as (s, org, user):
        await taxonomy.set_tag_scope(
            s,
            org_id=org,
            actor_id=user,
            tag_id=uuid.UUID(tag_id),
            target_ids=[uuid.UUID(t) for t in target_ids],
        )
        return {"tag_id": tag_id, "targets": len(target_ids)}


@mcp.tool()
async def create_task(
    token: str,
    org_id: str,
    title: str,
    description: str | None = None,
    importance: int = 4,
    urgency: int = 4,
    tag_ids: list[str] | None = None,
    estimate_effort_h: float | None = None,
    required_capabilities: list[str] | None = None,
    monetary_cost: float | None = None,
    location: str | None = None,
    necessity: str | None = None,
    index_scope: str | None = None,
    budget_id: str | None = None,
    assignee_ids: list[str] | None = None,
    assignee_id: str | None = None,
    start_at: str | None = None,
    duration_minutes: int | None = None,
    recurrence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a task. ``importance``/``urgency`` 1..5 Eisenhower
    (1=most pressing, default 4/4); ``priority`` is derived
    (importance*urgency, never settable). ``necessity`` is MoSCoW
    (must|should|could, default should). ``index_scope`` is
    ``org`` (default) or ``none``, which keeps the title and description
    out of the automatic search index -- not out of ``get_task``, and not
    out of the ``q=`` filter of ``list_tasks``. ``required_capabilities``
    declare what an executor needs (empty=any). Pass ``start_at``
    + ``duration_minutes`` to make it an appointment-task subject to
    no-overlap on assignee and explicit participants.

    ``tag_ids`` is one flat bag, but its structural members are
    single-valued: a task ends up with EXACTLY ONE client tag and
    EXACTLY ONE project tag (docs/adr/0003). Pass AT MOST one of each --
    two clients or two projects are rejected. The project decides: the
    client is derived from it, so passing the project alone is the
    normal call and passing a client that is not that project's client
    is rejected too. Pass neither and the task lands in the default
    "General" project under the default "Personal" client; there are no
    projectless tasks. Everything else in the bag (generic tags, memory
    channels) is a free-form facet with no cardinality rule."""
    async with _tenant(token, org_id) as (s, org, user):
        # When the MCP call is authenticated with an agent token
        # (HTTP transport), record the ai_assistant identity (if the
        # token is bound to one) AND the token id itself, so /tasks
        # can render an AI badge whether or not the token has been
        # upgraded to the ai_assistants flow (migrations 0059 / 0091
        # / 0093 in concert).
        creator_identity_id, creator_token_id = await _resolve_agent_context(s, org)
        task = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title=title,
            description=description,
            importance=importance,
            urgency=urgency,
            estimate_effort_h=(
                Decimal(str(estimate_effort_h)) if estimate_effort_h is not None else None
            ),
            required_capabilities=list(required_capabilities or []),
            monetary_cost=(Decimal(str(monetary_cost)) if monetary_cost is not None else None),
            location=location,
            necessity=Necessity(necessity) if necessity is not None else Necessity.should,
            index_scope=IndexScope(index_scope) if index_scope is not None else IndexScope.org,
            budget_id=uuid.UUID(budget_id) if budget_id else None,
            tag_ids=[uuid.UUID(t) for t in (tag_ids or [])],
            assignee_ids=[uuid.UUID(u) for u in (assignee_ids or [])],
            assignee_id=uuid.UUID(assignee_id) if assignee_id else None,
            created_by_identity_id=creator_identity_id,
            created_by_token_id=creator_token_id,
            start_at=dt.datetime.fromisoformat(start_at) if start_at else None,
            duration_minutes=duration_minutes,
            recurrence=recurrence,
            channel="mcp",
        )
        state = (await workflow_svc.states_by_ids(s, [task.state_id])).get(task.state_id)
        return _task(task, state=state)


async def _caller_tz(s: AsyncSession, user_id: uuid.UUID) -> dt.tzinfo:
    """The calling user's IANA timezone -> tzinfo (UTC fallback), used to
    expand a bare ``YYYY-MM-DD`` date filter into a day window (task 39e98a30)."""
    return resolve_tz(await s.scalar(select(User.timezone).where(User.id == user_id)))


def _day_start(d: dt.date, tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(0, 0), tzinfo=tz)


def _to_instant(raw: str, tz: dt.tzinfo) -> dt.datetime:
    """ISO string -> aware datetime: a timed value keeps its instant (a naive
    one is read in ``tz``); a bare date becomes that day's start in ``tz``."""
    v = split_due(raw)
    if isinstance(v, dt.datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=tz)
    return _day_start(v, tz)


def _to_date(raw: str) -> dt.date:
    """ISO string -> date (drops any time component)."""
    v = split_due(raw)
    return v.date() if isinstance(v, dt.datetime) else v


# ── Unified list pagination (tasks b7dde607 / c20c6351) ──────────────────
# One keyset-cursor contract for every paginated list_* tool: the service
# orders by a TOTAL key (the sort column(s) + an id tiebreak, all
# comparable), fetches limit+1 to detect truncation, and the MCP layer
# shapes the {items, next_cursor, truncated} envelope. The cursor is the
# opaque, base64'd value-list of the last returned row's sort key.


def _encode_cursor(values: list[Any]) -> str:
    """Opaque keyset cursor: base64 of the last row's sort-key values
    (datetime -> ISO, uuid -> str). Shared by every paginated list_* tool."""
    enc = [
        v.isoformat() if isinstance(v, dt.datetime) else str(v) if isinstance(v, uuid.UUID) else v
        for v in values
    ]
    return base64.urlsafe_b64encode(json.dumps(enc, separators=(",", ":")).encode()).decode()


def _decode_cursor(token: str) -> list[Any]:
    """Decode an opaque cursor to its raw value list; the caller casts each
    value to its column type. The cursor is round-tripped by the caller, so a
    malformed one is a caller error -> ``ValueError`` (not a leaked traceback)."""
    try:
        out = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(out, list):
        raise ValueError("invalid cursor")
    return out


def _page_envelope(
    rows: list[Any], limit: int, key: Callable[[Any], list[Any]]
) -> tuple[list[Any], str | None, bool]:
    """Shape a paginated list: ``rows`` were fetched as limit+1 (the service
    applied the SQL keyset + LIMIT). Returns ``(items, next_cursor,
    truncated)`` -- ``next_cursor`` is the encoded sort key of the last item
    when there is another page, else None. limit<=0 means 'no pagination'."""
    truncated = limit > 0 and len(rows) > limit
    items = rows[:limit] if limit > 0 else rows
    next_cursor = _encode_cursor(key(items[-1])) if (truncated and items) else None
    return items, next_cursor, truncated


async def _task_date_kwargs(
    s: AsyncSession,
    user_id: uuid.UUID,
    *,
    due_on: str | None,
    due_before: str | None,
    due_after: str | None,
    start_before: str | None,
    start_after: str | None,
    updated_since: str | None,
) -> dict[str, Any]:
    """Expand the string date filters that ``list_tasks`` and ``count_tasks``
    share into the service's absolute due/start/updated bounds in the
    caller's timezone (one place, so the two tools cannot drift)."""
    tz = (
        await _caller_tz(s, user_id)
        if any((due_on, due_before, due_after, start_before, start_after, updated_since))
        else dt.UTC
    )
    due_from = _to_instant(due_after, tz) if due_after else None
    due_to = _to_instant(due_before, tz) if due_before else None
    if due_on:
        d0 = _to_date(due_on)
        due_from = _day_start(d0, tz)
        due_to = _day_start(d0 + dt.timedelta(days=1), tz)
    return {
        "due_from": due_from,
        "due_to": due_to,
        "start_from": _to_date(start_after) if start_after else None,
        "start_to": _to_date(start_before) if start_before else None,
        "updated_since": _to_instant(updated_since, tz) if updated_since else None,
    }


@mcp.tool()
async def list_tasks(
    token: str,
    org_id: str,
    state_id: str | None = None,
    tag_id: str | None = None,
    parent_task_id: str | None = None,
    assignee_kind: str | None = None,
    assignee_handles: list[str] | None = None,
    owner_handles: list[str] | None = None,
    assignee_id: str | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
    open_only: bool = False,
    fields: list[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
    q: str | None = None,
    due_on: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    start_before: str | None = None,
    start_after: str | None = None,
    updated_since: str | None = None,
    order_by: str | None = None,
    order_desc: bool = False,
) -> dict[str, Any]:
    """List tasks: filter by state, tag, parent, assignee, owner, free-text
    ``q``, or a due/start/updated date window; optional ``order_by`` +
    ``order_desc`` sort. ``open_only=True`` returns only tasks in a
    non-terminal workflow state (no need to resolve a state uuid first).
    ``q`` matches a task's title/description/checklist/tag.

    Returns the paginated envelope ``{items, next_cursor, truncated}`` (NOT a
    bare array): ``truncated`` is true when more rows match than ``limit``;
    pass ``next_cursor`` back as ``cursor`` for the next disjoint page (keyset,
    so no dupes/gaps and no re-fetching). Cursor paging uses the default order;
    a ``cursor`` pins it (any ``order_by`` is ignored). With a custom
    ``order_by`` you still get ``items`` + ``truncated`` but ``next_cursor`` is
    null (raise ``limit`` to see more). For just the matching count use
    ``count_tasks`` (same filters); for ranked retrieval ``search(kinds=
    ['task'])`` or ``what_can_i_do_now``; ``get_task`` for one task's detail."""
    from mycelium_core.models.identity import IdentityKind

    # A cursor pins the default keyset order (it was issued under it); ignore
    # any order_by so a paged walk can't silently re-order mid-stream.
    after: tuple[int, dt.datetime, uuid.UUID] | None = None
    if cursor:
        cp, cc, ci = _decode_cursor(cursor)
        after = (int(cp), dt.datetime.fromisoformat(cc), uuid.UUID(ci))
        order_by, order_desc = None, False
    kind: IdentityKind | None = IdentityKind(assignee_kind) if assignee_kind else None
    async with _tenant(token, org_id) as (s, org, user):
        dates = await _task_date_kwargs(
            s,
            user,
            due_on=due_on,
            due_before=due_before,
            due_after=due_after,
            start_before=start_before,
            start_after=start_after,
            updated_since=updated_since,
        )
        # Fetch one extra row to detect truncation; the limit is pushed into
        # SQL by the service (no whole-table materialize).
        rows = await tasks.list_tasks(
            s,
            org_id=org,
            state_id=uuid.UUID(state_id) if state_id else None,
            tag_id=uuid.UUID(tag_id) if tag_id else None,
            parent_task_id=uuid.UUID(parent_task_id) if parent_task_id else None,
            assignee_kind=kind,
            assignee_handles=assignee_handles,
            owner_handles=owner_handles,
            assignee_id=uuid.UUID(assignee_id) if assignee_id else None,
            include_archived=include_archived,
            include_deleted=include_deleted,
            open_only=open_only,
            with_description=False,
            q=q,
            order_by=order_by,
            order_desc=order_desc,
            limit=(limit + 1) if limit > 0 else None,
            after=after,
            **dates,
        )
        # next_cursor only for the default keyset order; a custom order exposes
        # truncation but not a cursor (its key isn't the default triple).
        items, next_cursor, truncated = _page_envelope(
            rows, limit, key=lambda t: [t.priority, t.created_at, t.id]
        )
        if order_by is not None:
            next_cursor = None
        ids = [t.id for t in items]
        tagmap = await tasks.tags_by_task(s, task_ids=ids)
        ccounts = await tasks.collaborator_counts(s, org_id=org, task_ids=ids)
        statemap = await workflow_svc.states_by_ids(s, [t.state_id for t in items])
        return {
            "items": [
                _project_fields(
                    _task(
                        t,
                        tagmap.get(t.id, []),
                        collaborators_count=ccounts.get(t.id, 0),
                        state=statemap.get(t.state_id),
                    ),
                    fields,
                )
                for t in items
            ],
            "next_cursor": next_cursor,
            "truncated": truncated,
        }


@mcp.tool()
async def count_tasks(
    token: str,
    org_id: str,
    state_id: str | None = None,
    tag_id: str | None = None,
    parent_task_id: str | None = None,
    assignee_kind: str | None = None,
    assignee_handles: list[str] | None = None,
    owner_handles: list[str] | None = None,
    assignee_id: str | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
    open_only: bool = False,
    q: str | None = None,
    due_on: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    start_before: str | None = None,
    start_after: str | None = None,
    updated_since: str | None = None,
) -> dict[str, int]:
    """Count tasks matching the SAME filters as ``list_tasks`` with one
    ``COUNT`` query -- so 'how many open tasks' / 'do any tasks tagged X
    exist' is ``count_tasks(open_only=True)`` / ``count_tasks(tag_id=...)``,
    not a full ``list_tasks`` + ``len()``. Returns ``{"total": n}``."""
    from mycelium_core.models.identity import IdentityKind

    kind: IdentityKind | None = IdentityKind(assignee_kind) if assignee_kind else None
    async with _tenant(token, org_id) as (s, org, user):
        dates = await _task_date_kwargs(
            s,
            user,
            due_on=due_on,
            due_before=due_before,
            due_after=due_after,
            start_before=start_before,
            start_after=start_after,
            updated_since=updated_since,
        )
        total = await tasks.count_tasks(
            s,
            org_id=org,
            state_id=uuid.UUID(state_id) if state_id else None,
            tag_id=uuid.UUID(tag_id) if tag_id else None,
            parent_task_id=uuid.UUID(parent_task_id) if parent_task_id else None,
            assignee_kind=kind,
            assignee_handles=assignee_handles,
            owner_handles=owner_handles,
            assignee_id=uuid.UUID(assignee_id) if assignee_id else None,
            include_archived=include_archived,
            include_deleted=include_deleted,
            open_only=open_only,
            q=q,
            **dates,
        )
        return {"total": total}


@mcp.tool()
async def add_comment(token: str, org_id: str, task_id: str, body: str) -> dict[str, Any]:
    """Add a comment to a task (a chronological work-diary entry on the
    task description). Authorship is the calling identity -- an
    ai_assistant when the call uses an agent token.

    Afterwards the entry is fully editable: ``update_comment`` rewrites
    it, ``replace_in_comment`` amends one passage, ``delete_comment``
    removes it and ``restore_comment`` brings it back."""
    async with _tenant(token, org_id) as (s, org, user):
        author_id, _tok = await _resolve_agent_context(s, org)
        c = await tasks.add_comment(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            body=body,
            author_identity_id=author_id,
        )
        # ``version`` lets the caller delete this comment without a
        # ``list_comments`` round-trip (delete_comment needs expected_version).
        return {"id": str(c.id), "task_id": task_id, "version": c.version}


@mcp.tool()
async def get_comment(token: str, org_id: str, comment_id: str) -> dict[str, Any]:
    """Read ONE comment by id: random access into a long work diary
    without pulling every entry into context. The comment twin of
    ``get_note_part``. Returns the full row incl. body, status, author
    and ``version`` (the cursor the edit / delete tools need).

    Reads a soft-deleted comment too, so you can inspect one before
    handing its ``version`` to ``restore_comment``."""
    async with _tenant(token, org_id) as (s, org, _user):
        a = await annotations_svc.get_annotation(
            s,
            org_id=org,
            annotation_id=uuid.UUID(comment_id),
            include_deleted=True,
        )
        return _annotation_dict(a)


@mcp.tool()
async def list_trashed_comments(token: str, org_id: str, task_id: str) -> list[dict[str, Any]]:
    """Comments of a task that were deleted and can still be restored,
    oldest first. The discovery half of ``delete_comment`` /
    ``restore_comment``, and the comment twin of
    ``list_trashed_note_parts``: whoever wants an entry back is rarely
    whoever removed it, and a deleted comment is invisible to
    ``list_comments``."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await annotations_svc.list_for_doc(
            s,
            org_id=org,
            doc_kind="task_description",
            doc_id=uuid.UUID(task_id),
            include_deleted=True,
        )
        return [_annotation_dict(a) for a in rows if a.deleted_at is not None]


@mcp.tool()
async def append_to_comment(
    token: str,
    org_id: str,
    comment_id: str,
    text: str,
    separator: str = "\n\n",
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Append ``text`` to the END of a comment without reading or
    resending its body -- the comment twin of ``append_note_part`` and
    ``append_to_task_description``. The cheap way to add a follow-up to
    your own diary entry instead of posting a second comment.

    ``expected_version`` omitted appends onto the current version (blind
    append, no round trip); pass it to make the write conditional.
    Returns ``{id, version, appended_chars}``. Author or admin only."""
    async with _tenant(token, org_id) as (s, org, user):
        ident, _tok = await _resolve_agent_context(s, org)
        v, n = await annotations_svc.append_to_body(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(comment_id),
            text=text,
            separator=separator,
            expected_version=expected_version,
            actor_identity_id=ident,
        )
        return {"id": comment_id, "version": v, "appended_chars": n}


@mcp.tool()
async def prepend_to_comment(
    token: str,
    org_id: str,
    comment_id: str,
    text: str,
    separator: str = "\n\n",
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Prepend ``text`` to the FRONT of a comment without reading or
    resending its body: the mirror of ``append_to_comment``, for a
    correction or a TL;DR that has to lead. Returns ``{id, version,
    prepended_chars}``. Author or admin only."""
    async with _tenant(token, org_id) as (s, org, user):
        ident, _tok = await _resolve_agent_context(s, org)
        v, n = await annotations_svc.prepend_to_body(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(comment_id),
            text=text,
            separator=separator,
            expected_version=expected_version,
            actor_identity_id=ident,
        )
        return {"id": comment_id, "version": v, "prepended_chars": n}


@mcp.tool()
async def update_comment(
    token: str, org_id: str, comment_id: str, body: str, expected_version: int
) -> dict[str, Any]:
    """EDIT a comment: replace its whole body. Completes the
    ``add_comment`` / ``delete_comment`` pair, which could create and
    remove a comment but never rewrite one.

    ``comment_id`` is the ``id`` from ``add_comment`` / ``list_comments``;
    author or admin only. ``expected_version`` is that comment's
    ``version`` (optimistic-concurrency guard -- fails with
    ``concurrency.stale_version`` if the comment changed since you read
    it, carrying the CURRENT ``current_version``; that is NOT a
    permissions error). Do not blind-retry with the new version: a
    full-body edit would overwrite the other author's change. Re-read,
    merge, then retry -- or use ``replace_in_comment`` to change one
    passage without touching the rest.

    Accepts any comment id, task work-diary or inline note comment."""
    async with _tenant(token, org_id) as (s, org, user):
        ident, _tok = await _resolve_agent_context(s, org)
        v = await annotations_svc.edit(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(comment_id),
            body=body,
            expected_version=expected_version,
            actor_identity_id=ident,
        )
        return {"id": comment_id, "version": v}


@mcp.tool()
async def replace_in_comment(
    token: str,
    org_id: str,
    comment_id: str,
    find: str,
    replace: str,
    expected_version: int,
    count: int = 0,
) -> dict[str, Any]:
    """Anchored edit inside ONE comment: replace occurrences of the
    literal ``find`` with ``replace`` without resending the whole body.
    The comment twin of ``replace_in_note_part``, and the safe way to
    amend a long comment you did not write from scratch.

    ``count=0`` (default) replaces every occurrence; a positive ``count``
    only the first N. ``expected_version`` guards optimistic concurrency
    (same contract as ``update_comment``). Returns ``{id, version,
    replacements}``; a no-op (``find`` absent or empty) returns
    replacements=0 and does not bump the version. Author or admin
    only."""
    async with _tenant(token, org_id) as (s, org, user):
        ident, _tok = await _resolve_agent_context(s, org)
        v, n = await annotations_svc.replace_in_body(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(comment_id),
            find=find,
            replace=replace,
            expected_version=expected_version,
            count=count,
            actor_identity_id=ident,
        )
        return {"id": comment_id, "version": v, "replacements": n}


@mcp.tool()
async def restore_comment(
    token: str, org_id: str, comment_id: str, expected_version: int
) -> dict[str, Any]:
    """Undo a ``delete_comment``: the comment (or withdrawn suggestion)
    is back in the diary. Author or admin only, the same gate as the
    delete it reverses; ``expected_version`` is the deleted comment's
    ``version``.

    This is what makes the delete honestly soft -- until it existed the
    row was retained but unreachable on every surface."""
    async with _tenant(token, org_id) as (s, org, user):
        ident, _tok = await _resolve_agent_context(s, org)
        v = await annotations_svc.restore(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(comment_id),
            expected_version=expected_version,
            actor_identity_id=ident,
        )
        return {"id": comment_id, "version": v, "deleted": False}


@mcp.tool()
async def list_comment_revisions(
    token: str,
    org_id: str,
    comment_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Recovery-history timeline for ONE comment, most recent first
    (migration 0090). Comments were the last markdown document in the
    model with no history: ``version`` said an entry had changed and
    nothing said what it used to say.

    Each row carries the snapshot, who edited, when, and
    ``changed_fields`` (``body`` / ``status`` / ``_create`` / ``_delete``
    / ``_restore``). Feed an id to ``restore_comment_revision``."""
    async with _tenant(token, org_id) as (s, org, _user):
        # A comment snapshot quotes the document it hangs on
        # (``anchor_quote`` / ``original_text``), so the timeline answers to
        # the comment's perimeter -- which, for a note anchor, is the note's.
        await annotations_svc.get_annotation(
            s, org_id=org, annotation_id=uuid.UUID(comment_id), include_deleted=True
        )
        rows = await revisions_svc.list_revisions(
            s,
            entity_kind=revisions_svc.ENTITY_KIND_ANNOTATION,
            entity_id=uuid.UUID(comment_id),
            limit=limit,
        )
        return [_revision_payload(r) for r in rows]


@mcp.tool()
async def get_comment_revision(
    token: str,
    org_id: str,
    comment_id: str,
    revision_id: str,
) -> dict[str, Any]:
    """Single comment-revision lookup; 404 if the id does not belong to
    this comment. Read the old body before deciding to restore it."""
    async with _tenant(token, org_id) as (s, org, _user):
        await annotations_svc.get_annotation(
            s, org_id=org, annotation_id=uuid.UUID(comment_id), include_deleted=True
        )
        rev = await revisions_svc.get_revision(
            s,
            revision_id=uuid.UUID(revision_id),
            entity_kind=revisions_svc.ENTITY_KIND_ANNOTATION,
            entity_id=uuid.UUID(comment_id),
        )
        return _revision_payload(rev)


@mcp.tool()
async def restore_comment_revision(
    token: str,
    org_id: str,
    comment_id: str,
    revision_id: str,
    expected_version: int,
) -> dict[str, Any]:
    """Revert a comment's BODY to a past revision. Only the body is
    restorable: a restore reverts the words, never the thread's status,
    its assignee or its anchoring.

    Author or admin, like every other write to a comment. Logged as a
    NEW revision on the ``restore`` channel, so the timeline stays
    monotonic and the restore is itself recoverable."""
    async with _tenant(token, org_id) as (s, org, user):
        ident, _tok = await _resolve_agent_context(s, org)
        v = await annotations_svc.restore_revision(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(comment_id),
            revision_id=uuid.UUID(revision_id),
            expected_version=expected_version,
            actor_identity_id=ident,
        )
        return {"id": comment_id, "version": v}


@mcp.tool()
async def purge_comment(token: str, org_id: str, comment_id: str) -> dict[str, Any]:
    """PURGE a comment: destroy the entry, its card state and its whole
    revision history, for good. The counterpart of ``delete_note_part``
    for the comment family, and why it costs the ``delete:comments``
    danger scope. The ordinary, restorable removal is ``delete_comment``.

    ADMIN only -- not author-or-admin like every other comment write.
    An author managing their own words is right for a reversible delete;
    erasing a signed entry from a shared conversation so that no trace
    remains is a different act, and the person most likely to want it
    is the author. Accepts a live or an already-deleted comment."""
    async with _tenant(token, org_id) as (s, org, user):
        await annotations_svc.purge(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(comment_id),
        )
        return {"id": comment_id, "purged": True}


@mcp.tool()
async def delete_comment(
    token: str, org_id: str, comment_id: str, expected_version: int
) -> dict[str, Any]:
    """Soft-delete a comment -- the inverse of ``add_comment`` (author or
    admin only; the entry is retained and ``restore_comment`` brings it
    back). ``comment_id``
    is the ``id`` returned by ``add_comment`` / ``list_comments``;
    ``expected_version`` is that comment's ``version`` (optimistic-concurrency
    guard -- fails with ``concurrency.stale_version`` if the comment changed
    since you read it; the error carries the CURRENT ``current_version`` so you
    can re-read and retry, it is NOT a permissions error). Accepts any comment
    id, whether a task work-diary comment or an inline note comment."""
    async with _tenant(token, org_id) as (s, org, user):
        ident, _tok = await _resolve_agent_context(s, org)
        v = await annotations_svc.soft_delete(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(comment_id),
            expected_version=expected_version,
            actor_identity_id=ident,
        )
        return {"id": comment_id, "version": v, "deleted": True}


@mcp.tool()
async def add_dependency(
    token: str,
    org_id: str,
    predecessor_id: str,
    successor_id: str,
    type: str,
    lag_working_minutes: int = 0,
) -> dict[str, Any]:
    """Add a typed task dependency (FS/SS/FF/SF). Cycles are rejected."""
    async with _tenant(token, org_id) as (s, org, user):
        d = await dependencies.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=uuid.UUID(predecessor_id),
            successor_id=uuid.UUID(successor_id),
            type=DependencyType(type),
            lag_working_minutes=lag_working_minutes,
        )
        return {"id": str(d.id), "type": d.type.value}


@mcp.tool()
async def graph(token: str, org_id: str, project_tag_id: str | None = None) -> dict[str, Any]:
    """Return the dependency DAG (nodes + edges) for a scope."""
    async with _tenant(token, org_id) as (s, org, _user):
        return await dependencies.graph(
            s,
            org_id=org,
            project_tag_id=(uuid.UUID(project_tag_id) if project_tag_id else None),
        )


@mcp.tool()
async def set_task_state(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    state_id: str,
) -> dict[str, Any]:
    """Transition a task to a workflow state (validated)."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            state_id=uuid.UUID(state_id),
        )
        return {"task_id": task_id, "version": version}


def _task_full(
    t: Task,
    tags: list[Tag] | None = None,
    *,
    assignee_handle: str | None = None,
    owner_handle: str | None = None,
    collaborators: list[dict[str, str]] | None = None,
    state: WorkflowState | None = None,
) -> dict[str, Any]:
    # Full attribute set for editing one task. Unset nullable columns
    # (dates, estimate, cost, location, budget, parent, deleted_at) are
    # dropped via _compact: a typical task leaves most of these empty, so
    # emitting them as null is pure token overhead. Booleans/empties stay.
    return _compact(
        {
            "id": str(t.id),
            "title": t.title,
            "description": t.description,
            "state_id": str(t.state_id),
            # Resolved state name + its workflow (see _task): read the state
            # directly, and navigate to the machine via workflow_states.
            "state": state.name if state is not None else None,
            "state_is_terminal": state.is_terminal if state is not None else None,
            "workflow_id": str(state.workflow_id) if state is not None else None,
            "priority": t.priority,
            "importance": t.importance,
            "urgency": t.urgency,
            "start_date": t.start_date.isoformat() if t.start_date else None,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "billable": t.billable,
            "parent_task_id": (str(t.parent_task_id) if t.parent_task_id else None),
            "estimate_effort_h": (
                str(t.estimate_effort_h) if t.estimate_effort_h is not None else None
            ),
            "required_capabilities": list(t.required_capabilities or []),
            "monetary_cost": (str(t.monetary_cost) if t.monetary_cost is not None else None),
            "location": t.location,
            "necessity": t.necessity.value,
            # Unconditional here, unlike the lean ``_task``: this is the shape
            # an agent reads back to confirm a write.
            "index_scope": t.index_scope.value,
            "budget_id": str(t.budget_id) if t.budget_id else None,
            "is_archived": t.is_archived,
            "offered": t.offered,
            # Read-back of accountability/assignment (tasks 901f0f9f +
            # 2d3abdc3): the write tools take a handle/id, but the stored
            # values are opaque ids (``assignee_id`` -> identities,
            # ``owner_id`` -> users). Emit the ids AND the resolved
            # handles + the collaborator set so a caller can confirm what
            # it set without a second lookup. Handles/collaborators are
            # resolved by the get_task tool (it has the session).
            "assignee_id": str(t.assignee_id) if t.assignee_id else None,
            "assignee_handle": assignee_handle,
            "owner_id": str(t.owner_id) if t.owner_id else None,
            "owner_handle": owner_handle,
            "collaborators": collaborators if collaborators else None,
            "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
            "version": t.version,
            "tags": [_tag_brief(g) for g in (tags or [])],
        }
    )


@mcp.tool()
async def get_task(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Read one task with its full attribute set (for editing). Includes
    the assignment read-back (task 2d3abdc3): the resolved
    ``assignee_handle`` / ``owner_handle`` next to the stored ids, plus
    the ``collaborators`` set (people involved beyond the assignee). Also
    resolves the workflow ``state`` NAME (with ``state_is_terminal`` and the
    ``workflow_id``) alongside the raw ``state_id``, so you read the state
    directly and can call ``workflow_states`` / ``workflow_transitions`` for
    the rest of the machine -- no inferring states from other tasks."""
    from mycelium_core.services import identities as identities_svc

    async with _tenant(token, org_id) as (s, org, _user):
        t = await tasks.get_task(s, org_id=org, task_id=uuid.UUID(task_id))
        tagmap = await tasks.tags_by_task(s, task_ids=[t.id])
        assignee_handle = (
            await identities_svc.handle_for_identity(s, org_id=org, identity_id=t.assignee_id)
            if t.assignee_id
            else None
        )
        owner_handle = (
            await identities_svc.handle_for_user(s, user_id=t.owner_id) if t.owner_id else None
        )
        collaborators = await tasks.list_collaborators(s, org_id=org, task_id=t.id)
        state = (await workflow_svc.states_by_ids(s, [t.state_id])).get(t.state_id)
        return _task_full(
            t,
            tagmap.get(t.id, []),
            assignee_handle=assignee_handle,
            owner_handle=owner_handle,
            collaborators=collaborators,
            state=state,
        )


@mcp.tool()
async def replace_in_task_description(
    token: str,
    org_id: str,
    task_id: str,
    find: str,
    replace: str,
    expected_version: int | None = None,
    count: int = 0,
) -> dict[str, Any]:
    """Anchored edit inside ``task.description``: replace occurrences of
    the literal ``find`` with ``replace`` without resending the body. The
    task twin of ``replace_in_note_part`` / ``replace_in_comment`` -- the
    description could be appended and prepended to but never amended in
    place, so fixing one line meant rewriting the whole thing.

    ``count=0`` (default) replaces every occurrence; a positive ``count``
    only the first N. ``expected_version`` omitted replaces on the
    current version (same blind-write contract as
    ``append_to_task_description``). Returns ``{task_id, version,
    replacements}``; a no-op (``find`` absent or empty) returns
    replacements=0 and does not bump the version."""
    async with _tenant(token, org_id) as (s, org, user):
        new_version, n = await tasks.replace_in_description(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            find=find,
            replace=replace,
            expected_version=expected_version,
            count=count,
        )
        return {"task_id": task_id, "version": new_version, "replacements": n}


@mcp.tool()
async def append_to_task_description(
    token: str,
    org_id: str,
    task_id: str,
    text: str,
    separator: str = "\n\n",
    expected_version: int | None = None,
    dedupe_if_tail_matches: bool = False,
) -> dict[str, Any]:
    """Append ``text`` to ``task.description`` without first reading the
    body (task 4ac39ecf). Mirror of ``append_to_note`` scoped to the
    task description: an LLM can add a status note / a finding without
    round-tripping the existing description through its context.

    ``expected_version=None`` (default) appends onto whatever state the
    row currently has. ``dedupe_if_tail_matches=True`` makes the call
    a no-op when the body already ends with ``text``.

    Returns ``{task_id, version, appended_chars}``; appended_chars=0
    on dedupe. Refuses with ``body.limit_exceeded`` past
    ``MYCELIUM_NOTE_BODY_MAX_BYTES`` (default 1 MiB)."""
    async with _tenant(token, org_id) as (s, org, user):
        new_version, appended = await tasks.append_to_description(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            text=text,
            separator=separator,
            expected_version=expected_version,
            dedupe_if_tail_matches=dedupe_if_tail_matches,
            channel="mcp",
        )
        return {
            "task_id": task_id,
            "version": new_version,
            "appended_chars": appended,
        }


@mcp.tool()
async def prepend_to_task_description(
    token: str,
    org_id: str,
    task_id: str,
    text: str,
    separator: str = "\n\n",
    expected_version: int | None = None,
    dedupe_if_head_matches: bool = False,
) -> dict[str, Any]:
    """Prepend ``text`` to the FRONT of ``task.description`` without
    first reading the body (task 5662a07f; mirror of
    ``append_to_task_description``). Lets an LLM add a header / context
    on top without round-tripping the existing description.

    ``expected_version=None`` prepends onto the current state.
    ``dedupe_if_head_matches=True`` no-ops when the body already starts
    with ``text``. Returns ``{task_id, version, prepended_chars}``;
    refuses with ``body.limit_exceeded`` past the body postal_code."""
    async with _tenant(token, org_id) as (s, org, user):
        new_version, prepended = await tasks.prepend_to_description(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            text=text,
            separator=separator,
            expected_version=expected_version,
            dedupe_if_head_matches=dedupe_if_head_matches,
            channel="mcp",
        )
        return {
            "task_id": task_id,
            "version": new_version,
            "prepended_chars": prepended,
        }


@mcp.tool()
async def update_task(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    title: str | None = None,
    description: str | None = None,
    importance: int | None = None,
    urgency: int | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    billable: bool | None = None,
    estimate_effort_h: float | None = None,
    required_capabilities: list[str] | None = None,
    parent_task_id: str | None = None,
    monetary_cost: float | None = None,
    location: str | None = None,
    necessity: str | None = None,
    index_scope: str | None = None,
    budget_id: str | None = None,
) -> dict[str, Any]:
    """Edit task fields (only the given ones). ``priority`` is a
    calculated field and is not accepted here --- patch ``importance``
    / ``urgency`` (1..5) and the service re-derives priority.
    ``necessity`` is MoSCoW: ``must`` | ``should`` | ``could``.
    ``index_scope`` is ``org`` | ``none``; ``none`` drops this task's
    blob and keeps the indexer off it, and moving back to ``org``
    re-indexes it. Neither direction changes who can read the row.
    ``required_capabilities`` is the P2 executor capability requirement
    (docs/adr/0025); pass [] to clear it."""
    values: dict[str, Any] = {}
    if title is not None:
        values["title"] = title
    if description is not None:
        values["description"] = description
    if importance is not None:
        values["importance"] = importance
    if urgency is not None:
        values["urgency"] = urgency
    if start_date is not None:
        values["start_date"] = dt.date.fromisoformat(start_date)
    if due_date is not None:
        # A bare ``YYYY-MM-DD`` is date-only ("due that day, no time"); a
        # full ISO datetime is an explicit instant. The core service
        # promotes the date-only case to end-of-day in the OWNER's
        # configured timezone -- the single source of truth shared by the
        # SPA, the HTTP API and here -- so the MCP just parses the shape
        # and forwards it. (Previously this baked end-of-day UTC, which
        # for a non-UTC user rolled into the next day and fired the
        # reminder a day late.)
        values["due_date"] = split_due(due_date)
    if billable is not None:
        values["billable"] = billable
    if estimate_effort_h is not None:
        values["estimate_effort_h"] = Decimal(str(estimate_effort_h))
    if required_capabilities is not None:
        values["required_capabilities"] = list(required_capabilities)
    if parent_task_id is not None:
        values["parent_task_id"] = uuid.UUID(parent_task_id)
    if monetary_cost is not None:
        values["monetary_cost"] = Decimal(str(monetary_cost))
    if location is not None:
        values["location"] = location
    if necessity is not None:
        values["necessity"] = Necessity(necessity)
    if index_scope is not None:
        values["index_scope"] = IndexScope(index_scope)
    if budget_id is not None:
        values["budget_id"] = uuid.UUID(budget_id)
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            values=values,
            channel="mcp",
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def archive_task(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    archived: bool = True,
) -> dict[str, Any]:
    """Archive (or unarchive with ``archived=False``) a task."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.archive_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            archived=archived,
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def delete_task(
    token: str, org_id: str, task_id: str, expected_version: int
) -> dict[str, Any]:
    """Soft-delete a task (recoverable via restore_task)."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.soft_delete_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def restore_task(
    token: str, org_id: str, task_id: str, expected_version: int
) -> dict[str, Any]:
    """Restore a soft-deleted task."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.restore_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def add_task_tag(token: str, org_id: str, task_id: str, tag_id: str) -> dict[str, Any]:
    """Attach a free-form tag (generic / memory channel) to a task,
    idempotently. This is the many-to-many door: a task carries any
    number of these facets.

    The task's client and project are NOT facets: they are structural
    and single-valued (exactly one each, docs/adr/0003), so use
    ``move_task_to_project`` to change the project (it drags the client
    along) and ``set_task_client`` to change the client. Passing one of
    those tags here still performs the structural operation, but the
    named tools say what they do and validate the tag's kind."""
    async with _tenant(token, org_id) as (s, org, user):
        await tasks.attach_tag(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            tag_id=uuid.UUID(tag_id),
        )
        return {"task_id": task_id, "tag_id": tag_id}


@mcp.tool()
async def remove_task_tag(token: str, org_id: str, task_id: str, tag_id: str) -> dict[str, Any]:
    """Detach a free-form tag from a task. The client and the project
    cannot be detached (tag.structural_required): a task has exactly one
    of each and there is no legal state to detach INTO, so re-point it
    with ``move_task_to_project`` / ``set_task_client`` instead."""
    async with _tenant(token, org_id) as (s, org, user):
        await tasks.detach_tag(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            tag_id=uuid.UUID(tag_id),
        )
        return {"task_id": task_id, "tag_id": tag_id, "removed": True}


@mcp.tool()
async def move_task_to_project(
    token: str, org_id: str, task_id: str, project_tag_id: str
) -> dict[str, Any]:
    """Move a task to another project. A task has exactly one project
    and exactly one client, so the new project REPLACES the old one and
    the task's client is snapped to that project's client in the same
    transaction. Moving a task to a project of a DIFFERENT client is
    therefore this one call, and is not an error: the project is the
    truth, the client follows it (docs/adr/0003)."""
    async with _tenant(token, org_id) as (s, org, user):
        project = await _tag_of_kind(s, org, project_tag_id, TagKind.project)
        # One gated call: ``tasks.attach_tag`` keeps the RBAC and
        # existence checks and routes a project tag to
        # ``tag_assignment.move_to_project``, the only writer of a
        # structural junction row. The previous detach-loop + attach
        # composed two writes here, left the client tag untouched (so
        # the task kept the OLD client, violating invariant (c)) and
        # exposed a projectless task in between.
        await tasks.attach_tag(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            tag_id=project,
        )
        return {"task_id": task_id, "project_tag_id": project_tag_id}


@mcp.tool()
async def set_task_client(
    token: str, org_id: str, task_id: str, client_tag_id: str
) -> dict[str, Any]:
    """Re-point a task at a client. A task has exactly one client, so
    this REPLACES the current one and keeps the project. A client that
    contradicts the task's project is refused
    (tag.client_project_mismatch): the client is derived from the
    project, so use ``move_task_to_project`` with a project of the
    wanted client to move the task across clients."""
    async with _tenant(token, org_id) as (s, org, user):
        client = await _tag_of_kind(s, org, client_tag_id, TagKind.client)
        await tasks.attach_tag(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            tag_id=client,
        )
        return {"task_id": task_id, "client_tag_id": client_tag_id}


@mcp.tool()
async def assign_task(token: str, org_id: str, task_id: str, user_id: str) -> dict[str, Any]:
    """Assign a user to a task (idempotent)."""
    async with _tenant(token, org_id) as (s, org, user):
        await tasks.assign(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            user_id=uuid.UUID(user_id),
        )
        return {"task_id": task_id, "user_id": user_id}


@mcp.tool()
async def unassign_task(token: str, org_id: str, task_id: str, user_id: str) -> dict[str, Any]:
    """Unassign a user from a task."""
    async with _tenant(token, org_id) as (s, org, user):
        await tasks.unassign(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            user_id=uuid.UUID(user_id),
        )
        return {"task_id": task_id, "user_id": user_id, "removed": True}


@mcp.tool()
async def list_comments(token: str, org_id: str, task_id: str) -> list[dict[str, Any]]:
    """List a task's work-diary comments (doc_kind='task_description'), oldest
    first. Returns resolved comments too (unlike the SPA, which hides them);
    for open-only use
    list_annotations(doc_kind='task_description', doc_id=task_id, include_resolved=False)."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await tasks.list_comments(s, org_id=org, task_id=uuid.UUID(task_id))
        return [_annotation_dict(c) for c in rows]


def _annotation_dict(a: Any) -> dict[str, Any]:
    """Serialise an annotation row for MCP. ``doc_id`` collapses the
    typed FKs back to the generic markdown-document handle."""
    doc_id = a.task_id if a.doc_kind == "task_description" else a.note_part_id
    return {
        "id": str(a.id),
        "doc_kind": a.doc_kind,
        "doc_id": str(doc_id),
        "kind": a.kind,
        "body": a.body,
        "status": a.status,
        "anchor_quote": a.anchor_quote,
        "original_text": a.original_text,
        "proposed_text": a.proposed_text,
        "parent_id": str(a.parent_id) if a.parent_id else None,
        "author_identity_id": (str(a.author_identity_id) if a.author_identity_id else None),
        "assigned_to_identity_id": (
            str(a.assigned_to_identity_id) if a.assigned_to_identity_id else None
        ),
        "version": a.version,
        "created_at": a.created_at.isoformat(),
    }


@mcp.tool()
async def add_annotation(
    token: str,
    org_id: str,
    doc_kind: str,
    doc_id: str,
    body: str,
    anchor_quote: str | None = None,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """Add an inline comment to a markdown document. ``doc_kind`` is
    ``note_part`` (``doc_id`` = note-part id) or ``task_description``
    (``doc_id`` = task id). ``anchor_quote`` pins it to a passage; omit
    it for a whole-document / work-diary comment. ``parent_id`` makes it
    a reply. Authorship is the calling identity (ai_assistant under an
    agent token)."""
    async with _tenant(token, org_id) as (s, org, user):
        author_id, _tok = await _resolve_agent_context(s, org)
        a = await annotations_svc.create_comment(
            s,
            org_id=org,
            actor_id=user,
            doc_kind=doc_kind,
            doc_id=uuid.UUID(doc_id),
            body=body,
            anchor_quote=anchor_quote,
            parent_id=uuid.UUID(parent_id) if parent_id else None,
            author_identity_id=author_id,
        )
        return _annotation_dict(a)


@mcp.tool()
async def propose_suggestion(
    token: str,
    org_id: str,
    doc_kind: str,
    doc_id: str,
    original_text: str,
    proposed_text: str,
    rationale: str = "",
) -> dict[str, Any]:
    """Propose an edit to a markdown document: replace ``original_text``
    with ``proposed_text`` (with an optional rationale). Nothing changes
    in the document until the suggestion is accepted. ``doc_kind`` is
    ``note_part`` or ``task_description``. The natural output of an LLM
    review pass.

    ``original_text`` is MARKDOWN SOURCE: quote the body exactly as
    ``get_note_part`` / ``get_task`` handed it to you, markup included.
    Quote ``**bold**``, not ``bold``. It must occur exactly once in the body
    (a little more context makes it unique); otherwise the suggestion is
    declined as stale rather than applied to a passage nobody chose.

    Until migration 0099 this was resolved in a RENDERED projection with the
    markup stripped, so quoting the source -- the only thing an agent can
    actually read -- silently failed with SUGGESTION_STALE whenever the span
    contained any markup at all.

    A proposal that would change the document's BLOCK STRUCTURE is refused on
    accept: a ``|`` that adds a cell to a table row, a blank line that splits
    a list item, a ``` run inside a fence, a leading ``#`` that promotes a
    paragraph. Edit inline content; restructure with the part/description
    writers."""
    async with _tenant(token, org_id) as (s, org, user):
        author_id, _tok = await _resolve_agent_context(s, org)
        a = await annotations_svc.propose_suggestion(
            s,
            org_id=org,
            actor_id=user,
            doc_kind=doc_kind,
            doc_id=uuid.UUID(doc_id),
            original_text=original_text,
            proposed_text=proposed_text,
            rationale=rationale,
            author_identity_id=author_id,
        )
        return _annotation_dict(a)


@mcp.tool()
async def list_annotations(
    token: str,
    org_id: str,
    doc_kind: str,
    doc_id: str,
    include_resolved: bool = True,
    include_deleted: bool = False,
    kind: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List the annotations (comments + suggestions) on a markdown document,
    oldest first. include_resolved defaults True (returns resolved/accepted/
    rejected rows too, unlike the SPA); pass include_resolved=False for
    open-only. ``include_deleted=True`` also returns soft-deleted rows, which
    is how you find the id and ``version`` to hand to ``restore_comment``.
    ``kind`` optionally narrows to 'comment' or 'suggestion'. A
    task's work-diary comments are also reachable via list_comments. For just
    the open/total counts use ``count_annotations``. Returns the paginated
    envelope ``{items, next_cursor, truncated}``: pass ``limit`` to page, then
    ``next_cursor`` back as ``cursor`` (keyset, no dupes/gaps)."""
    after: tuple[dt.datetime, uuid.UUID] | None = None
    if cursor:
        cc, ci = _decode_cursor(cursor)
        after = (dt.datetime.fromisoformat(cc), uuid.UUID(ci))
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await annotations_svc.list_for_doc(
            s,
            org_id=org,
            doc_kind=doc_kind,
            doc_id=uuid.UUID(doc_id),
            include_resolved=include_resolved,
            include_deleted=include_deleted,
            kind=kind,
            limit=(limit + 1) if limit else None,
            after=after,
        )
        items, next_cursor, truncated = _page_envelope(
            rows, limit or 0, key=lambda a: [a.created_at, a.id]
        )
        return {
            "items": [_annotation_dict(a) for a in items],
            "next_cursor": next_cursor,
            "truncated": truncated,
        }


@mcp.tool()
async def count_annotations(
    token: str,
    org_id: str,
    doc_kind: str,
    doc_id: str,
    kind: str | None = None,
) -> dict[str, int]:
    """Count the annotations on a markdown document with ``COUNT`` queries
    (no row fetch). Returns ``{"total": n, "open": m}`` -- ``open`` is the
    still-actionable subset (status='open'). ``kind`` optionally narrows to
    'comment' or 'suggestion'. ``doc_kind`` is 'task_description' (then
    ``doc_id`` is the task id) or a note-part kind."""
    async with _tenant(token, org_id) as (s, org, _user):
        total, open_ = await annotations_svc.count_for_doc(
            s,
            org_id=org,
            doc_kind=doc_kind,
            doc_id=uuid.UUID(doc_id),
            kind=kind,
        )
        return {"total": total, "open": open_}


@mcp.tool()
async def edit_annotation(
    token: str, org_id: str, annotation_id: str, body: str, expected_version: int
) -> dict[str, Any]:
    """Edit an annotation's body (author or admin only). ``expected_version``
    is an optimistic-concurrency guard: if someone else saved since you read
    the annotation, this fails with ``concurrency.stale_version`` and the
    error carries the CURRENT ``current_version`` in its params. That is NOT a
    permissions error -- re-read the annotation (``list_annotations`` /
    ``list_comments``), merge your change into the current body, and retry with
    ``current_version``. Do not just resend with the new version blindly: a
    full-body edit would overwrite the other author's change (lost update)."""
    async with _tenant(token, org_id) as (s, org, user):
        ident, _tok = await _resolve_agent_context(s, org)
        v = await annotations_svc.edit(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(annotation_id),
            body=body,
            expected_version=expected_version,
            actor_identity_id=ident,
        )
        return {"id": annotation_id, "version": v}


@mcp.tool()
async def delete_annotation(
    token: str, org_id: str, annotation_id: str, expected_version: int
) -> dict[str, Any]:
    """Soft-delete an annotation / withdraw a pending suggestion (author
    or admin only)."""
    async with _tenant(token, org_id) as (s, org, user):
        ident, _tok = await _resolve_agent_context(s, org)
        v = await annotations_svc.soft_delete(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(annotation_id),
            expected_version=expected_version,
            actor_identity_id=ident,
        )
        return {"id": annotation_id, "version": v, "deleted": True}


@mcp.tool()
async def assign_annotation(
    token: str,
    org_id: str,
    annotation_id: str,
    expected_version: int,
    assignee_handle: str | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    """Assign an annotation to a workspace identity (``assignee_handle``: a
    bare handle, ``@handle``, or login email), or clear it (``clear=true``).
    Any member may assign -- it is coordination, not authorship. An unknown
    handle returns ``identity.not_found``. Returns the new version."""
    async with _tenant(token, org_id) as (s, org, user):
        v = await annotations_svc.assign(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(annotation_id),
            expected_version=expected_version,
            assignee_handle=assignee_handle,
            clear=clear,
        )
        return {"id": annotation_id, "version": v, "cleared": clear}


@mcp.tool()
async def list_assigned_annotations(
    token: str,
    org_id: str,
    assignee_handle: str | None = None,
    include_resolved: bool = False,
) -> list[dict[str, Any]]:
    """The "assigned to me" inbox: annotations assigned to ``assignee_handle``
    (defaults to the calling identity), newest first. Open-only unless
    ``include_resolved=true``. An unknown handle yields an empty list."""
    from mycelium_core.services import identities as identities_svc

    async with _tenant(token, org_id) as (s, org, user):
        if assignee_handle:
            # A read about someone's backlog, deactivated included.
            ident_row = await identities_svc.lookup_by_handle(
                s, org_id=org, handle=assignee_handle, include_inactive=True
            )
            if ident_row is None:
                return []
            ident_id = ident_row.id
        else:
            ident_id = (await identities_svc.ensure_for_user(s, org_id=org, user_id=user)).id
        rows = await annotations_svc.list_assigned(
            s, org_id=org, assignee_identity_id=ident_id, include_resolved=include_resolved
        )
        return [_annotation_dict(a) for a in rows]


@mcp.tool()
async def resolve_annotation(
    token: str, org_id: str, annotation_id: str, expected_version: int
) -> dict[str, Any]:
    """Mark a comment thread resolved."""
    async with _tenant(token, org_id) as (s, org, user):
        by, _tok = await _resolve_agent_context(s, org)
        v = await annotations_svc.resolve(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(annotation_id),
            expected_version=expected_version,
            resolved_by_identity_id=by,
        )
        return {"id": annotation_id, "version": v, "status": "resolved"}


@mcp.tool()
async def reopen_annotation(
    token: str, org_id: str, annotation_id: str, expected_version: int
) -> dict[str, Any]:
    """Reopen a resolved comment thread."""
    async with _tenant(token, org_id) as (s, org, user):
        v = await annotations_svc.reopen(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(annotation_id),
            expected_version=expected_version,
        )
        return {"id": annotation_id, "version": v, "status": "open"}


@mcp.tool()
async def accept_suggestion(
    token: str, org_id: str, annotation_id: str, expected_version: int
) -> dict[str, Any]:
    """Accept a suggestion: splice the proposed text into the document
    body and mark it accepted. Errors with a stale signal (and changes
    nothing) if the target text has moved or gone."""
    async with _tenant(token, org_id) as (s, org, user):
        by, _tok = await _resolve_agent_context(s, org)
        v = await annotations_svc.accept_suggestion(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(annotation_id),
            expected_version=expected_version,
            resolved_by_identity_id=by,
            # Accepting rewrites the target note/task body, so the service
            # requires write on that family too, not just comments:write
            # (task c19f2f63, enabler B). None for a full-access caller.
            granted_scopes=_PRINCIPAL_SCOPE.get(),
        )
        return {"id": annotation_id, "version": v, "status": "accepted"}


@mcp.tool()
async def reject_suggestion(
    token: str, org_id: str, annotation_id: str, expected_version: int
) -> dict[str, Any]:
    """Reject a pending suggestion; the document body is untouched."""
    async with _tenant(token, org_id) as (s, org, user):
        by, _tok = await _resolve_agent_context(s, org)
        v = await annotations_svc.reject_suggestion(
            s,
            org_id=org,
            actor_id=user,
            annotation_id=uuid.UUID(annotation_id),
            expected_version=expected_version,
            resolved_by_identity_id=by,
        )
        return {"id": annotation_id, "version": v, "status": "rejected"}


# --- task dependencies: remove + list (FR-3) ---


def _dependency(d: TaskDependency) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "predecessor_id": str(d.predecessor_id),
        "successor_id": str(d.successor_id),
        "type": d.type.value,
        "lag_working_minutes": d.lag_working_minutes,
        "version": d.version,
    }


@mcp.tool()
async def list_dependencies(
    token: str,
    org_id: str,
    task_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List task dependencies, newest first, optionally only those touching a
    task. Without ``task_id`` this is the whole org graph. Returns the
    paginated envelope ``{items, next_cursor, truncated}``: pass ``limit`` to
    page, then ``next_cursor`` back as ``cursor`` for the next disjoint page
    (keyset, no dupes/gaps)."""
    after: tuple[dt.datetime, uuid.UUID] | None = None
    if cursor:
        cc, ci = _decode_cursor(cursor)
        after = (dt.datetime.fromisoformat(cc), uuid.UUID(ci))
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await dependencies.list_dependencies(
            s,
            org_id=org,
            task_id=uuid.UUID(task_id) if task_id else None,
            limit=(limit + 1) if limit else None,
            after=after,
        )
        items, next_cursor, truncated = _page_envelope(
            rows, limit or 0, key=lambda d: [d.created_at, d.id]
        )
        return {
            "items": [_dependency(d) for d in items],
            "next_cursor": next_cursor,
            "truncated": truncated,
        }


@mcp.tool()
async def remove_dependency(token: str, org_id: str, dependency_id: str) -> dict[str, Any]:
    """Remove a task dependency edge."""
    async with _tenant(token, org_id) as (s, org, user):
        await dependencies.remove_dependency(
            s,
            org_id=org,
            actor_id=user,
            dependency_id=uuid.UUID(dependency_id),
        )
        return {"dependency_id": dependency_id, "removed": True}


# --- F3: calendars + deterministic schedule (FR-4) ---
# Events were unified into tasks in migration 0094; their MCP surface
# (the four ``*_event`` tools and the ``_event`` helper) is gone.
# Appointments are tasks with ``start_at`` + ``duration_minutes``.


def _schedule(s: Schedule) -> dict[str, Any]:
    return {
        "task_id": str(s.task_id),
        "es": s.es.isoformat() if s.es else None,
        "ef": s.ef.isoformat() if s.ef else None,
        "ls": s.ls.isoformat() if s.ls else None,
        "lf": s.lf.isoformat() if s.lf else None,
        "slack_minutes": s.slack_minutes,
        "on_logical_critical_path": s.on_logical_critical_path,
        "on_critical_chain": s.on_critical_chain,
        "projected_cost": str(s.projected_cost),
        "scheduled_start": (s.scheduled_start.isoformat() if s.scheduled_start else None),
        "scheduled_end": (s.scheduled_end.isoformat() if s.scheduled_end else None),
        "assigned_executor_id": (str(s.assigned_executor_id) if s.assigned_executor_id else None),
        "unassignable": s.unassignable,
        "unassignable_reason": s.unassignable_reason,
        "input_fingerprint": s.input_fingerprint,
    }


@mcp.tool()
async def create_calendar(
    token: str,
    org_id: str,
    name: str,
    weekly_hours: dict[str, list[list[str]]],
    timezone: str = "Europe/Rome",
) -> dict[str, Any]:
    """Create a working calendar (weekday -> [start, end] HH:MM windows)."""
    async with _tenant(token, org_id) as (s, org, user):
        cal = await calendars.create_calendar(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            timezone=timezone,
            weekly_hours=weekly_hours,
        )
        return {"id": str(cal.id), "name": cal.name, "version": cal.version}


@mcp.tool()
async def list_calendars(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the org working calendars."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await calendars.list_calendars(s, org_id=org)
        return [
            {
                "id": str(c.id),
                "name": c.name,
                "is_default": c.is_default,
                "timezone": c.timezone,
            }
            for c in rows
        ]


@mcp.tool()
async def add_holiday(token: str, org_id: str, calendar_id: str, day: str) -> dict[str, Any]:
    """Add a holiday (ISO date) to a calendar; idempotent."""
    async with _tenant(token, org_id) as (s, org, user):
        await calendars.add_holiday(
            s,
            org_id=org,
            actor_id=user,
            calendar_id=uuid.UUID(calendar_id),
            day=dt.date.fromisoformat(day),
        )
        return {"calendar_id": calendar_id, "day": day}


@mcp.tool()
async def list_holidays(token: str, org_id: str, calendar_id: str) -> list[dict[str, Any]]:
    """List a calendar's holidays (ascending)."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await calendars.list_holidays(s, org_id=org, calendar_id=uuid.UUID(calendar_id))
        return [{"calendar_id": calendar_id, "day": d.isoformat()} for d in rows]


@mcp.tool()
async def remove_holiday(token: str, org_id: str, calendar_id: str, day: str) -> dict[str, Any]:
    """Remove a holiday (ISO date) from a calendar."""
    async with _tenant(token, org_id) as (s, org, user):
        await calendars.remove_holiday(
            s,
            org_id=org,
            actor_id=user,
            calendar_id=uuid.UUID(calendar_id),
            day=dt.date.fromisoformat(day),
        )
        return {"calendar_id": calendar_id, "day": day, "removed": True}


@mcp.tool()
async def set_user_calendar(
    token: str,
    org_id: str,
    user_id: str,
    calendar_id: str,
    daily_capacity_h: float,
) -> dict[str, Any]:
    """Assign a calendar + daily capacity (hours) to a user."""
    async with _tenant(token, org_id) as (s, org, user):
        await calendars.set_user_calendar(
            s,
            org_id=org,
            actor_id=user,
            user_id=uuid.UUID(user_id),
            calendar_id=uuid.UUID(calendar_id),
            daily_capacity_h=Decimal(str(daily_capacity_h)),
        )
        return {"user_id": user_id, "calendar_id": calendar_id}


# Migration 0097 dropped the standalone events table. Appointments are
# tasks with ``start_at`` + ``duration_minutes`` -- AI agents create
# them through ``create_task`` and pin extra invitees through the
# participants tools below. The four legacy ``*_event`` MCP tools
# (create_event / list_events / reschedule_event / delete_event) were
# removed in the unification commit.


@mcp.tool()
async def list_task_participants(
    token: str,
    org_id: str,
    task_id: str,
) -> list[dict[str, Any]]:
    """List the additional identities pinned to an appointment-task
    (migration 0095/0096, ADR-0008 addendum). The assignee always
    appears here too via the 0096 trigger mirror, so a single read
    returns every identity that owns the slot. Plain tasks /
    reminders return an empty list (no slot to occupy)."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await part_svc.list_participants(s, org_id=org, task_id=uuid.UUID(task_id))
        return [
            {
                "identity_id": str(p.identity_id),
                "handle": i.handle,
                "kind": i.kind.value,
                "start_at": p.start_at.isoformat(),
                "duration_minutes": p.duration_minutes,
            }
            for p, i in rows
        ]


@mcp.tool()
async def add_task_participant(
    token: str,
    org_id: str,
    task_id: str,
    identity_id: str | None = None,
    handle: str | None = None,
) -> dict[str, Any]:
    """Pin an identity to an appointment-task (the task must carry
    ``start_at`` + ``duration_minutes``). Pass either ``identity_id``
    or ``handle`` (the service resolves the handle through the
    org's identities). Idempotent on the same (task, identity).
    Raises ``event.overlap`` (409) when the identity already holds
    another appointment overlapping the window."""
    async with _tenant(token, org_id) as (s, org, user):
        row = await part_svc.add_participant(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            identity_id=uuid.UUID(identity_id) if identity_id else None,
            handle=handle,
        )
        return {
            "task_id": str(row.task_id),
            "identity_id": str(row.identity_id),
            "start_at": row.start_at.isoformat(),
            "duration_minutes": row.duration_minutes,
        }


@mcp.tool()
async def remove_task_participant(
    token: str,
    org_id: str,
    task_id: str,
    identity_id: str,
) -> dict[str, Any]:
    """Unpin an identity from an appointment-task. No-op if the
    identity is not a participant. Removing the assignee's mirror
    row is allowed but the 0096 trigger will re-insert it on the
    next task update -- to permanently remove the primary owner,
    change ``tasks.assignee_id`` instead."""
    async with _tenant(token, org_id) as (s, org, user):
        await part_svc.remove_participant(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            identity_id=uuid.UUID(identity_id),
        )
        return {"task_id": task_id, "identity_id": identity_id, "removed": True}


@mcp.tool()
async def set_task_schedule(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    schedule_mode: str | None = None,
    constraint_kind: str | None = None,
    constraint_date: str | None = None,
    remaining_effort_h: float | None = None,
    actual_start: str | None = None,
    is_milestone: bool | None = None,
) -> dict[str, Any]:
    """Write-back scheduler pins/constraints; survives recompute (FR-4)."""
    values: dict[str, Any] = {}
    if schedule_mode is not None:
        values["schedule_mode"] = ScheduleMode(schedule_mode)
    if constraint_kind is not None:
        values["constraint_kind"] = ConstraintKind(constraint_kind)
    if constraint_date is not None:
        values["constraint_date"] = dt.datetime.fromisoformat(constraint_date)
    if remaining_effort_h is not None:
        values["remaining_effort_h"] = Decimal(str(remaining_effort_h))
    if actual_start is not None:
        values["actual_start"] = dt.datetime.fromisoformat(actual_start)
    if is_milestone is not None:
        values["is_milestone"] = is_milestone
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.set_schedule_fields(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            values=values,
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def recompute_schedule(
    token: str,
    org_id: str,
    project_tag_id: str | None = None,
    as_of: str | None = None,
    policy: str | None = None,
) -> dict[str, Any]:
    """Deterministically recompute the schedule for a scope under a
    resource-leveling ``policy`` (fastest|cheapest|balanced|throughput,
    default balanced). Returns the row count plus the projected makespan
    and projected credit cost so policies are comparable, and the count
    of llm tasks with no admissible executor (P2 dispatch gaps;
    ADR-0025)."""
    async with _tenant(token, org_id) as (s, org, user):
        summary = await scheduler.recompute(
            s,
            org_id=org,
            actor_id=user,
            project_tag_id=(uuid.UUID(project_tag_id) if project_tag_id else None),
            as_of=dt.datetime.fromisoformat(as_of) if as_of else None,
            policy=(SchedulePolicy(policy) if policy else SchedulePolicy.balanced),
        )
        return {
            "count": summary.count,
            "makespan_minutes": summary.makespan_minutes,
            "projected_credit_cost": str(summary.projected_credit_cost),
            "policy": summary.policy.value,
            "unassignable_count": summary.unassignable_count,
        }


@mcp.tool()
async def get_schedule(token: str, org_id: str, task_id: str) -> dict[str, Any] | None:
    """Read one task's derived schedule row."""
    async with _tenant(token, org_id) as (s, org, _user):
        row = await scheduler.get_schedule(s, org_id=org, task_id=uuid.UUID(task_id))
        return _schedule(row) if row is not None else None


@mcp.tool()
async def list_schedule(
    token: str, org_id: str, project_tag_id: str | None = None
) -> list[dict[str, Any]]:
    """List derived schedule rows for a scope."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await scheduler.list_schedule(
            s,
            org_id=org,
            project_tag_id=(uuid.UUID(project_tag_id) if project_tag_id else None),
        )
        return [_schedule(r) for r in rows]


# --- Executor registry (docs/adr/0025, P2) ---
# Co-equal to the REST surface. Reads are member-level; mutations are
# owner-gated inside the service (the RBAC choke point + effective-role
# sudo), mirroring the rate-card / issuer-profile tools.


def _executor(e: Executor) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "kind": e.kind.value,
        "name": e.name,
        "user_id": str(e.user_id) if e.user_id else None,
        "context_switch_cost_minutes": e.context_switch_cost_minutes,
        "provider": e.provider,
        "model_id": e.model_id,
        "max_parallel": e.max_parallel,
        "credit_budget": (str(e.credit_budget) if e.credit_budget is not None else None),
        "credit_rate_per_hour": str(e.credit_rate_per_hour),
        "enabled": e.enabled,
        "capability_tags": list(e.capability_tags or []),
        "version": e.version,
    }


@mcp.tool()
async def executors_list(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the workspace executors (humans + llm agents). Member-level
    (the schedule plan must show its assignments)."""
    async with _tenant(token, org_id) as (s, org, _user):
        return [_executor(e) for e in await executors_svc.list_executors(s, org_id=org)]


@mcp.tool()
async def executor_create(
    token: str,
    org_id: str,
    name: str,
    kind: str = "llm_agent",
    user_id: str | None = None,
    context_switch_cost_minutes: int = 0,
    provider: str | None = None,
    model_id: str | None = None,
    max_parallel: int = 4,
    credit_budget: float | None = None,
    credit_rate_per_hour: float = 0.0,
    enabled: bool = True,
    capability_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Owner: create an executor (docs/adr/0025 P2). An ``llm_agent``
    needs a name + max_parallel>=1 + credit_rate_per_hour>=0; a ``human``
    must be bound to a workspace member (user_id). Owner-gated in the
    service (effective-role sudo enforced)."""
    async with _tenant(token, org_id) as (s, org, user):
        row = await executors_svc.create_executor(
            s,
            org_id=org,
            actor_id=user,
            kind=ExecutorKind(kind),
            name=name,
            user_id=uuid.UUID(user_id) if user_id else None,
            context_switch_cost_minutes=context_switch_cost_minutes,
            provider=provider,
            model_id=model_id,
            max_parallel=max_parallel,
            credit_budget=(Decimal(str(credit_budget)) if credit_budget is not None else None),
            credit_rate_per_hour=Decimal(str(credit_rate_per_hour)),
            enabled=enabled,
            capability_tags=list(capability_tags or []),
        )
        return _executor(row)


@mcp.tool()
async def executor_update(
    token: str,
    org_id: str,
    executor_id: str,
    expected_version: int,
    name: str | None = None,
    context_switch_cost_minutes: int | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    max_parallel: int | None = None,
    credit_budget: float | None = None,
    credit_rate_per_hour: float | None = None,
    enabled: bool | None = None,
    capability_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Owner: patch an executor (optimistic concurrency). ``kind`` and
    ``user_id`` are immutable identity. Owner-gated in the service."""
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if context_switch_cost_minutes is not None:
        values["context_switch_cost_minutes"] = context_switch_cost_minutes
    if provider is not None:
        values["provider"] = provider
    if model_id is not None:
        values["model_id"] = model_id
    if max_parallel is not None:
        values["max_parallel"] = max_parallel
    if credit_budget is not None:
        values["credit_budget"] = Decimal(str(credit_budget))
    if credit_rate_per_hour is not None:
        values["credit_rate_per_hour"] = Decimal(str(credit_rate_per_hour))
    if enabled is not None:
        values["enabled"] = enabled
    if capability_tags is not None:
        values["capability_tags"] = list(capability_tags)
    async with _tenant(token, org_id) as (s, org, user):
        version = await executors_svc.update_executor(
            s,
            org_id=org,
            actor_id=user,
            executor_id=uuid.UUID(executor_id),
            expected_version=expected_version,
            values=values,
        )
        return {"executor_id": executor_id, "version": version}


@mcp.tool()
async def executor_delete(token: str, org_id: str, executor_id: str) -> dict[str, Any]:
    """Owner: delete an executor. Always allowed (including the seeded
    default agent): the scheduler marks affected llm tasks unassignable
    rather than silently rerouting. Owner-gated in the service."""
    async with _tenant(token, org_id) as (s, org, user):
        await executors_svc.delete_executor(
            s,
            org_id=org,
            actor_id=user,
            executor_id=uuid.UUID(executor_id),
        )
        return {"deleted": True}


# --- Agent execution runtime (docs/adr/0025, P3) ---
# Co-equal to the REST surface. Reads are member-level; start/cancel
# are owner-gated INSIDE the service (the RBAC choke point +
# effective-role sudo), because running an agent spends credits -- same
# gate model as the billing-grant tools. The governance (hard tool
# allowlist, step/budget caps, cooperative kill switch) is in
# mycelium_core.services.agent_runtime; this is a thin wrapper.


def _agent_run(r: AgentRun) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "task_id": str(r.task_id),
        "executor_id": str(r.executor_id) if r.executor_id else None,
        "status": r.status.value,
        "steps": r.steps,
        "credits_spent": str(r.credits_spent),
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "error": r.error,
        "artifact_note_id": (str(r.artifact_note_id) if r.artifact_note_id else None),
        "cancel_requested": r.cancel_requested,
        "blocked_reason": r.blocked_reason,
        "version": r.version,
    }


@mcp.tool()
async def agent_run_start(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Owner: run the agent on an already-dispatched ``llm_agent`` task
    end-to-end (spawn → work → artifact → complete). Returns the
    TERMINAL run (succeeded|failed|cancelled|blocked). On-demand, not
    autonomous loop. Bounded (step/budget caps), killable; every tool
    call confined to actor's effective RBAC."""
    async with _tenant(token, org_id) as (s, org, user):
        run = await agent_runtime_svc.start_run(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
        )
        return _agent_run(run)


@mcp.tool()
async def agent_run_get(token: str, org_id: str, run_id: str) -> dict[str, Any]:
    """Read one agent run (member-level, RLS-scoped)."""
    async with _tenant(token, org_id) as (s, org, _user):
        run = await agent_runtime_svc.get_run(s, org_id=org, run_id=uuid.UUID(run_id))
        return _agent_run(run)


@mcp.tool()
async def agent_runs_list(
    token: str, org_id: str, task_id: str | None = None
) -> list[dict[str, Any]]:
    """List agent runs (member-level), newest first, optionally filtered
    to one task."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await agent_runtime_svc.list_runs(
            s,
            org_id=org,
            task_id=uuid.UUID(task_id) if task_id else None,
        )
        return [_agent_run(r) for r in rows]


@mcp.tool()
async def agent_run_cancel(token: str, org_id: str, run_id: str) -> dict[str, Any]:
    """Owner: request cancellation (cooperative kill switch the loop
    observes). Idempotent; a terminal run is an error. Owner-gated in
    the service."""
    async with _tenant(token, org_id) as (s, org, user):
        run = await agent_runtime_svc.cancel_run(
            s,
            org_id=org,
            actor_id=user,
            run_id=uuid.UUID(run_id),
        )
        return _agent_run(run)


# --- P4: coordination handoffs + contract-net (docs/adr/0025) ---


def _handoff(h: TaskHandoff) -> dict[str, Any]:
    return {
        "id": str(h.id),
        "predecessor_task_id": str(h.predecessor_task_id),
        "successor_task_id": str(h.successor_task_id),
        "from_executor_id": (str(h.from_executor_id) if h.from_executor_id else None),
        "to_executor_id": str(h.to_executor_id) if h.to_executor_id else None,
        "message": h.message,
        "artifact_note_id": (str(h.artifact_note_id) if h.artifact_note_id else None),
        "status": h.status.value,
        "delivered_at": h.delivered_at.isoformat() if h.delivered_at else None,
        "consumed_at": h.consumed_at.isoformat() if h.consumed_at else None,
        "version": h.version,
    }


@mcp.tool()
async def task_handoffs_list(token: str, org_id: str, task_id: str) -> list[dict[str, Any]]:
    """List the coordination handoffs touching a task (incoming +
    outgoing). The on-completion creation is automatic (no create
    tool). Member-level, RLS-scoped (a foreign task yields none)."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await coordination_svc.list_handoffs(s, org_id=org, task_id=uuid.UUID(task_id))
        return [_handoff(h) for h in rows]


@mcp.tool()
async def task_offer(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Owner: announce a task to eligible members (contract-net call-
    for-proposals); marks it offered + notifies. Owner-gated in the
    service (effective-role sudo enforced). The llm_agent 'award' is
    the P2 admission dispatch, not this tool."""
    async with _tenant(token, org_id) as (s, org, user):
        task = await coordination_svc.offer_task(
            s, org_id=org, actor_id=user, task_id=uuid.UUID(task_id)
        )
        tagmap = await tasks.tags_by_task(s, task_ids=[task.id])
        state = (await workflow_svc.states_by_ids(s, [task.state_id])).get(task.state_id)
        return _task_full(task, tagmap.get(task.id, []), state=state)


@mcp.tool()
async def task_claim(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Member: claim an offered task (contract-net award) -> the caller
    becomes an assignee, ``offered`` is cleared, the offerer is
    notified. Errors if not offered / already claimed."""
    async with _tenant(token, org_id) as (s, org, user):
        task = await coordination_svc.claim_task(
            s, org_id=org, actor_id=user, task_id=uuid.UUID(task_id)
        )
        tagmap = await tasks.tags_by_task(s, task_ids=[task.id])
        state = (await workflow_svc.states_by_ids(s, [task.state_id])).get(task.state_id)
        return _task_full(task, tagmap.get(task.id, []), state=state)


@mcp.tool()
async def task_decline(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Member: decline an offered task (lightweight: notify the offerer
    + audit, no assignment, ``offered`` left set for others). Errors if
    not offered."""
    async with _tenant(token, org_id) as (s, org, user):
        task = await coordination_svc.decline_task(
            s, org_id=org, actor_id=user, task_id=uuid.UUID(task_id)
        )
        tagmap = await tasks.tags_by_task(s, task_ids=[task.id])
        state = (await workflow_svc.states_by_ids(s, [task.state_id])).get(task.state_id)
        return _task_full(task, tagmap.get(task.id, []), state=state)


# --- P5: closed-loop dispatch + approval gates (docs/adr/0025) ---
# Co-equal to the REST surface. The queue read is member-level; approve
# / deny / tick are owner-gated inside the service (a tick can spend
# credits via the P3 metered path -> effective-role sudo enforced),
# mirroring the agent-run-start / billing-grant tools.


def _dispatch_request(r: DispatchRequest) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "task_id": str(r.task_id),
        "executor_id": str(r.executor_id) if r.executor_id else None,
        "status": r.status.value,
        "projected_credit_cost": str(r.projected_credit_cost),
        "agent_run_id": str(r.agent_run_id) if r.agent_run_id else None,
        "requested_at": r.requested_at.isoformat() if r.requested_at else None,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decided_by": str(r.decided_by) if r.decided_by else None,
        "reason": r.reason,
        "version": r.version,
    }


@mcp.tool()
async def dispatch_requests_list(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the closed-loop dispatch queue (member-level, RLS-scoped),
    newest first. Each row is an approval gate for an admitted
    ``llm_agent`` task: status, the assigned executor, the projected
    credit cost, and the started ``agent_run_id`` once dispatched."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await dispatch_loop_svc.list_requests(s, org_id=org)
        return [_dispatch_request(r) for r in rows]


@mcp.tool()
async def dispatch_approve(
    token: str, org_id: str, request_id: str, expected_version: int
) -> dict[str, Any]:
    """Owner: approve a pending dispatch request, then immediately
    attempt the dispatch inline (approve-then-inline-dispatch: the run
    starts via the P3 metered path in this call). Owner-gated in the
    service (a dispatch spends credits; effective-role sudo enforced).
    Optimistic concurrency on ``expected_version``."""
    async with _tenant(token, org_id) as (s, org, user):
        req = await dispatch_loop_svc.approve_request(
            s,
            org_id=org,
            actor_id=user,
            request_id=uuid.UUID(request_id),
            expected_version=expected_version,
        )
        return _dispatch_request(req)


@mcp.tool()
async def dispatch_deny(
    token: str,
    org_id: str,
    request_id: str,
    expected_version: int,
    reason: str | None = None,
) -> dict[str, Any]:
    """Owner: deny an active dispatch request (never starts a run), with
    an optional short reason. Owner-gated in the service; optimistic
    concurrency on ``expected_version``."""
    async with _tenant(token, org_id) as (s, org, user):
        req = await dispatch_loop_svc.deny_request(
            s,
            org_id=org,
            actor_id=user,
            request_id=uuid.UUID(request_id),
            expected_version=expected_version,
            reason=reason,
        )
        return _dispatch_request(req)


@mcp.tool()
async def dispatch_tick(token: str, org_id: str, policy: str | None = None) -> dict[str, Any]:
    """Owner: run one closed-loop tick now (recompute -> admit -> gate
    -> dispatch via P3). The worker calls the same service on a timer.
    Owner-gated in the service (a tick can spend credits). ``policy`` is
    the resource-leveling policy for the recompute
    (fastest|cheapest|balanced|throughput, default balanced)."""
    async with _tenant(token, org_id) as (s, org, user):
        res = await dispatch_loop_svc.tick(
            s,
            org_id=org,
            actor_id=user,
            policy=(SchedulePolicy(policy) if policy else SchedulePolicy.balanced),
        )
        return {
            "policy": res.policy.value,
            "enabled": res.enabled,
            "created": res.created,
            "approved": res.approved,
            "dispatched": res.dispatched,
            "skipped": res.skipped,
            "failed": res.failed,
            "projected_makespan_minutes": res.projected_makespan_minutes,
            "projected_credit_cost": str(res.projected_credit_cost),
        }


# --- F4: time tracking (FR-5) ---


_EMPTY_TASK_CTX = time_svc.TaskContext()


def _time_entry(
    e: TimeEntry,
    ctx: time_svc.TaskContext = _EMPTY_TASK_CTX,
    note_title: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "task_id": str(e.task_id),
        "user_id": str(e.user_id),
        "started_at": e.started_at.isoformat(),
        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
        "duration_seconds": e.duration_seconds,
        "accumulated_seconds": e.accumulated_seconds,
        "resumed_at": e.resumed_at.isoformat() if e.resumed_at else None,
        "source": e.source.value,
        "executor_kind": e.executor_kind.value,
        "billable": e.billable,
        "parallel": e.parallel,
        "rate_snapshot": (str(e.rate_snapshot) if e.rate_snapshot is not None else None),
        "currency": e.currency,
        "memo": e.memo,
        "note_id": str(e.note_id) if e.note_id else None,
        "note_title": note_title,
        "version": e.version,
        "task_title": ctx.task_title,
        "client_tag_id": (str(ctx.client_tag_id) if ctx.client_tag_id else None),
        "client_name": ctx.client_name,
        "project_tag_id": (str(ctx.project_tag_id) if ctx.project_tag_id else None),
        "project_name": ctx.project_name,
        "client_timezone": ctx.client_timezone,
    }


async def _time_entry_one(s: Any, e: TimeEntry) -> dict[str, Any]:
    titles = await time_svc.resolve_note_titles(s, [e.note_id])
    note_title = titles.get(e.note_id) if e.note_id is not None else None
    return _time_entry(e, await time_svc.context_for_entry(s, e), note_title)


async def _time_entries_many(s: Any, rows: list[TimeEntry]) -> list[dict[str, Any]]:
    ctxs = await time_svc.resolve_task_contexts(s, [e.task_id for e in rows])
    titles = await time_svc.resolve_note_titles(s, [e.note_id for e in rows])
    return [
        _time_entry(
            e,
            ctxs.get(e.task_id, _EMPTY_TASK_CTX),
            titles.get(e.note_id) if e.note_id is not None else None,
        )
        for e in rows
    ]


@mcp.tool()
async def start_timer(
    token: str,
    org_id: str,
    task_id: str | None = None,
    billable: bool | None = None,
    memo: str | None = None,
    note_id: str | None = None,
    parallel: bool = False,
) -> dict[str, Any]:
    """Start the live timer. Serial (default) replaces the running
    serial timer; ``parallel=True`` runs alongside (e.g. concurrent
    LLM tasks). Same task never double-tracked. Proposal A: ``note_id``
    logs time in a work-note (must be linked to a task); billing task
    is derived (``task_id`` optional or must agree). ``memo`` =
    free-text on the entry (not the Note entity)."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.start_timer(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id) if task_id else None,
            billable=billable,
            memo=memo,
            note_id=uuid.UUID(note_id) if note_id else None,
            parallel=parallel,
        )
        return await _time_entry_one(s, e)


@mcp.tool()
async def stop_timer(
    token: str,
    org_id: str,
    task_id: str | None = None,
    memo: str | None = None,
) -> dict[str, Any]:
    """Stop a running timer: the one for ``task_id`` if given, else the
    serial timer. Computes the duration. ``memo`` overwrites the
    entry's free-text note when given."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.stop_timer(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id) if task_id else None,
            memo=memo,
        )
        return await _time_entry_one(s, e)


@mcp.tool()
async def pause_timer(
    token: str,
    org_id: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Pause a running timer without finalizing it: the one for
    ``task_id`` if given, else the serial timer. Banks the elapsed so
    far and freezes it; the entry stays open and can be resumed. No-op
    if already paused; NO_RUNNING_TIMER if nothing is open."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.pause_timer(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id) if task_id else None,
        )
        return await _time_entry_one(s, e)


@mcp.tool()
async def resume_timer(
    token: str,
    org_id: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Resume a paused timer: the one for ``task_id`` if given, else the
    serial timer. The elapsed ticks again from the banked total. No-op
    if already running; NO_RUNNING_TIMER if there is no open (paused)
    entry."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.resume_timer(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id) if task_id else None,
        )
        return await _time_entry_one(s, e)


@mcp.tool()
async def add_time_entry(
    token: str,
    org_id: str,
    task_id: str | None = None,
    started_at: str = "",
    ended_at: str | None = None,
    duration_seconds: int | None = None,
    billable: bool | None = None,
    memo: str | None = None,
    note_id: str | None = None,
) -> dict[str, Any]:
    """Add a manual time entry (provide ended_at or duration_seconds).
    Proposal A: a ``note_id`` derives the billing task (the note must
    be linked to a task; ``task_id`` may be omitted or must agree).
    ``memo`` is the entry's free-text note (not the Note entity)."""
    async with _tenant(token, org_id) as (s, org, user):
        e = await time_svc.add_manual_entry(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id) if task_id else None,
            started_at=dt.datetime.fromisoformat(started_at),
            ended_at=dt.datetime.fromisoformat(ended_at) if ended_at else None,
            duration_seconds=duration_seconds,
            billable=billable,
            memo=memo,
            note_id=uuid.UUID(note_id) if note_id else None,
        )
        return await _time_entry_one(s, e)


@mcp.tool()
async def list_time_entries(
    token: str,
    org_id: str,
    task_id: str | None = None,
    user_id: str | None = None,
    fields: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List time entries, optionally filtered by task or user.
    ``fields`` opt-in keeps only the named columns (``id`` always kept).
    ``limit``/``offset`` paginate at the DB level (default limit 100)."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await time_svc.list_entries(
            s,
            org_id=org,
            task_id=uuid.UUID(task_id) if task_id else None,
            user_id=uuid.UUID(user_id) if user_id else None,
            limit=limit,
            offset=offset,
        )
        entries = await _time_entries_many(s, rows)
        return [_project_fields(e, fields) for e in entries]


@mcp.tool()
async def get_time_entry(token: str, org_id: str, entry_id: str) -> dict[str, Any]:
    """Read one time entry."""
    async with _tenant(token, org_id) as (s, org, _user):
        e = await time_svc.get_entry(s, org_id=org, entry_id=uuid.UUID(entry_id))
        return await _time_entry_one(s, e)


@mcp.tool()
async def list_running_timers(
    token: str, org_id: str, user_id: str | None = None, handle: str | None = None
) -> list[dict[str, Any]]:
    """Live timers (the serial one plus any parallel). Defaults to the
    CALLER's timers; pass ``user_id`` (uuid) or ``handle`` (a workspace
    user handle / ``@handle`` / login email) to read someone else's. An
    unknown ``handle`` or a non-user identity yields an empty list."""
    from mycelium_core.services import identities as identities_svc

    async with _tenant(token, org_id) as (s, org, user):
        if user_id:
            target = uuid.UUID(user_id)
        elif handle:
            # A read about someone's timers, deactivated included.
            ident_row = await identities_svc.lookup_by_handle(
                s, org_id=org, handle=handle, include_inactive=True
            )
            if ident_row is None or ident_row.user_id is None:
                return []
            target = ident_row.user_id
        else:
            target = user
        rows = await time_svc.running_entries(s, org_id=org, user_id=target)
        return await _time_entries_many(s, rows)


@mcp.tool()
async def update_time_entry(
    token: str,
    org_id: str,
    entry_id: str,
    expected_version: int,
    memo: str | None = None,
    billable: bool | None = None,
    task_id: str | None = None,
    note_id: str | None = None,
    clear_note_id: bool = False,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> dict[str, Any]:
    """Correct a time entry. Reassign with ``task_id`` (transitively
    changes project/client). Fix interval with ``started_at``/
    ``ended_at`` (ISO-8601; duration recomputed). Omit ``ended_at`` to
    keep; pass to set/clear. Proposal-A work-note: ``note_id`` to set
    (note must share the entry's task) or ``clear_note_id=True`` to
    unlink; omit both to preserve."""
    values: dict[str, Any] = {}
    if memo is not None:
        values["memo"] = memo
    if billable is not None:
        values["billable"] = billable
    if task_id is not None:
        values["task_id"] = uuid.UUID(task_id)
    if clear_note_id:
        values["note_id"] = None
    elif note_id is not None:
        values["note_id"] = uuid.UUID(note_id)
    if started_at is not None:
        values["started_at"] = dt.datetime.fromisoformat(started_at)
    if ended_at is not None:
        values["ended_at"] = dt.datetime.fromisoformat(ended_at)
    async with _tenant(token, org_id) as (s, org, user):
        version = await time_svc.update_entry(
            s,
            org_id=org,
            actor_id=user,
            entry_id=uuid.UUID(entry_id),
            expected_version=expected_version,
            values=values,
        )
        return {"entry_id": entry_id, "version": version}


@mcp.tool()
async def delete_time_entry(token: str, org_id: str, entry_id: str) -> dict[str, Any]:
    """Delete a time entry."""
    async with _tenant(token, org_id) as (s, org, user):
        await time_svc.delete_entry(
            s,
            org_id=org,
            actor_id=user,
            entry_id=uuid.UUID(entry_id),
        )
        return {"entry_id": entry_id, "deleted": True}


@mcp.tool()
async def time_report(
    token: str,
    org_id: str,
    group_by: str = "project",
    billable: bool | None = None,
) -> list[dict[str, Any]]:
    """Aggregated time report grouped by project|client|generic|user|task."""
    async with _tenant(token, org_id) as (s, org, user):
        rows = await time_svc.report(
            s,
            org_id=org,
            actor_id=user,
            group_by=ReportGroup(group_by),
            billable=billable,
        )
        return [
            {
                "key": r.key,
                "label": r.label,
                "seconds": r.seconds,
                "billable_seconds": r.billable_seconds,
                "amount": str(r.amount),
                "currency": r.currency,
            }
            for r in rows
        ]


@mcp.tool()
async def time_report_by_task(
    token: str,
    org_id: str,
    start_from: str | None = None,
    start_to: str | None = None,
) -> list[dict[str, Any]]:
    """Per-task time aggregate for the caller: total/billable seconds
    and entry count per task, each row carrying the resolved
    project/client and the client's IANA timezone. Ordered by total
    time desc. Optional ISO-8601 ``start_from``/``start_to`` window."""
    async with _tenant(token, org_id) as (s, org, user):
        rows = await time_svc.task_report(
            s,
            org_id=org,
            actor_id=user,
            start_from=dt.datetime.fromisoformat(start_from) if start_from else None,
            start_to=dt.datetime.fromisoformat(start_to) if start_to else None,
            # ``task_report`` is org-wide by default (its user scoping is
            # now an explicit filter, like GET /time/entries). This tool
            # is documented as "for the caller", so it pins the scope
            # instead of silently widening to the whole org.
            user_id=user,
        )
        return [
            {
                "task_id": str(r.task_id),
                "task_title": r.task_title,
                "client_tag_id": (str(r.client_tag_id) if r.client_tag_id else None),
                "client_name": r.client_name,
                "project_tag_id": (str(r.project_tag_id) if r.project_tag_id else None),
                "project_name": r.project_name,
                "client_timezone": r.client_timezone,
                "total_seconds": r.total_seconds,
                "billable_seconds": r.billable_seconds,
                "entry_count": r.entry_count,
            }
            for r in rows
        ]


# --- F4b: budgets + deterministic advisory (FR-13/FR-14) ---


def _budget(b: Budget) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "name": b.name,
        "category": b.category,
        "period_kind": b.period_kind.value,
        "period_start": b.period_start.isoformat(),
        "period_end": b.period_end.isoformat(),
        "amount": str(b.amount),
        "currency": b.currency,
        "version": b.version,
    }


@mcp.tool()
async def create_budget(
    token: str,
    org_id: str,
    name: str,
    period_kind: str,
    period_start: str,
    period_end: str,
    amount: float,
    currency: str = "EUR",
    category: str | None = None,
) -> dict[str, Any]:
    """Create a budget envelope (period_kind: month|quarter|year|custom)."""
    async with _tenant(token, org_id) as (s, org, user):
        b = await budgets_svc.create_budget(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            category=category,
            period_kind=BudgetPeriod(period_kind),
            period_start=dt.date.fromisoformat(period_start),
            period_end=dt.date.fromisoformat(period_end),
            amount=Decimal(str(amount)),
            currency=currency,
        )
        return _budget(b)


@mcp.tool()
async def list_budgets(token: str, org_id: str) -> list[dict[str, Any]]:
    """List budget envelopes."""
    async with _tenant(token, org_id) as (s, org, _user):
        return [_budget(b) for b in await budgets_svc.list_budgets(s, org_id=org)]


@mcp.tool()
async def budget_consumption(token: str, org_id: str, budget_id: str) -> dict[str, Any]:
    """Deterministic consumption vs residual for a budget."""
    async with _tenant(token, org_id) as (s, org, _user):
        c = await budgets_svc.consumption(s, org_id=org, budget_id=uuid.UUID(budget_id))
        return {
            "budget_id": str(c.budget_id),
            "amount": str(c.amount),
            "currency": c.currency,
            "consumed": str(c.consumed),
            "residual": str(c.residual),
            "task_count": c.task_count,
        }


@mcp.tool()
async def update_budget(
    token: str,
    org_id: str,
    budget_id: str,
    expected_version: int,
    name: str | None = None,
    category: str | None = None,
    period_kind: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    amount: float | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    """Edit a budget envelope (only the given fields)."""
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if category is not None:
        values["category"] = category
    if period_kind is not None:
        values["period_kind"] = BudgetPeriod(period_kind)
    if period_start is not None:
        values["period_start"] = dt.date.fromisoformat(period_start)
    if period_end is not None:
        values["period_end"] = dt.date.fromisoformat(period_end)
    if amount is not None:
        values["amount"] = Decimal(str(amount))
    if currency is not None:
        values["currency"] = currency
    async with _tenant(token, org_id) as (s, org, user):
        version = await budgets_svc.update_budget(
            s,
            org_id=org,
            actor_id=user,
            budget_id=uuid.UUID(budget_id),
            expected_version=expected_version,
            values=values,
        )
        return {"budget_id": budget_id, "version": version}


@mcp.tool()
async def delete_budget(token: str, org_id: str, budget_id: str) -> dict[str, Any]:
    """Delete a budget envelope."""
    async with _tenant(token, org_id) as (s, org, user):
        await budgets_svc.delete_budget(
            s,
            org_id=org,
            actor_id=user,
            budget_id=uuid.UUID(budget_id),
        )
        return {"budget_id": budget_id, "deleted": True}


@mcp.tool()
async def what_can_i_do_now(
    token: str,
    org_id: str,
    duration_minutes: int,
    window_start: str | None = None,
    location: str | None = None,
    context_tags: list[str] | None = None,
    focus_tag_ids: list[str] | None = None,
    any_tag_ids: list[str] | None = None,
    min_priority: int | None = None,
    min_necessity: str | None = None,
    narrate: bool = False,
) -> dict[str, Any]:
    """Deterministic plan over the CALLER's OWN actionable tasks for a free
    window, urgency-first ranked. Scoped to tasks you own only; for someone
    else's queue use list_tasks(assignee_handles=[...]).

    window_start (ISO 8601) defaults to UTC now() when omitted; a naive
    value is coerced to UTC. focus_tag_ids (project/client tag ids) is a
    hard SCOPE: when set, only tasks carrying one of those tags are kept.
    any_tag_ids (generic tag ids), min_priority (keep priority<=level, an
    importance floor since 1=top..25) and min_necessity (must|should|could
    floor) then combine by UNION within that scope. location is a soft,
    case-insensitive substring place filter (tasks with no place stay).
    Returns the NarratedPlanOut envelope
    {ranked, over_window, narration, narration_model, narrated}: ``ranked``
    are the tasks completable within the window; ``over_window`` clear every
    other filter but need more time than the window (effort > duration),
    surfaced apart so a too-long overdue/at-risk must is not silently
    dropped. With ``narrate`` true the advisor adds an optional rationale
    over the SAME ranking (metered at the resolve_llm seam), degrading to
    narrated=false when no provider is configured.
    """
    async with _tenant(token, org_id) as (s, org, user):
        if window_start is not None:
            ws = dt.datetime.fromisoformat(window_start)
            if ws.tzinfo is None:
                ws = ws.replace(tzinfo=dt.UTC)
        else:
            ws = dt.datetime.now(dt.UTC)
        plan = await advisory_svc.what_can_i_do_now(
            s,
            org_id=org,
            actor_id=user,
            window_start=ws,
            duration_minutes=duration_minutes,
            location=location,
            context_tags=context_tags,
            focus_tag_ids=[uuid.UUID(x) for x in focus_tag_ids] if focus_tag_ids else None,
            any_tag_ids=[uuid.UUID(x) for x in any_tag_ids] if any_tag_ids else None,
            min_priority=min_priority,
            min_necessity=Necessity(min_necessity) if min_necessity else None,
        )

        def _row(r: advisory_svc.FeasibleTask) -> dict[str, Any]:
            return {
                "task_id": str(r.task_id),
                "title": r.title,
                "necessity": r.necessity.value,
                "priority": r.priority,
                "due_date": r.due_date.isoformat() if r.due_date else None,
                "remaining_minutes": r.remaining_minutes,
                "slack_minutes": r.slack_minutes,
                "deadline_bucket": r.deadline_bucket,
            }

        ranked = [_row(r) for r in plan.fits]
        over_window = [_row(r) for r in plan.over_window]
        narration: str | None = None
        narration_model: str | None = None
        narrated = False
        if narrate and plan.fits:
            np = await advisory_svc.narrate_plan(
                s,
                org_id=org,
                actor_id=user,
                window_start=ws,
                duration_minutes=duration_minutes,
                plan=plan.fits,
            )
            narration = np.narration
            narration_model = np.narration_model
            narrated = np.narrated
        return {
            "ranked": ranked,
            "over_window": over_window,
            "narration": narration,
            "narration_model": narration_model,
            "narrated": narrated,
        }


@mcp.tool()
async def errands(
    token: str,
    org_id: str,
    location: str | None = None,
    context: str | None = None,
) -> list[dict[str, Any]]:
    """Place/context matcher: tasks for an errand run at a location and/or
    context, across the org. Requires at least one of location/context and
    returns [] when both are omitted -- a matcher, not a general task lister."""
    async with _tenant(token, org_id) as (s, org, user):
        rows = await advisory_svc.errands(
            s,
            org_id=org,
            actor_id=user,
            location=location,
            context=context,
        )
        return [
            {
                "task_id": str(r.task_id),
                "title": r.title,
                "location": r.location,
                "necessity": r.necessity.value,
                "priority": r.priority,
            }
            for r in rows
        ]


@mcp.tool()
async def prioritize_within_budget(token: str, org_id: str, budget_id: str) -> dict[str, Any]:
    """Deterministic priority/value-density selection within a budget."""
    async with _tenant(token, org_id) as (s, org, user):
        plan = await advisory_svc.prioritize_within_budget(
            s,
            org_id=org,
            actor_id=user,
            budget_id=uuid.UUID(budget_id),
        )
        return {
            "budget_id": str(plan.budget_id),
            "amount": str(plan.amount),
            "currency": plan.currency,
            "allocated": str(plan.allocated),
            "residual": str(plan.residual),
            "selected": [
                {
                    "task_id": str(p.task_id),
                    "title": p.title,
                    "cost": str(p.cost),
                    "necessity": p.necessity.value,
                    "priority": p.priority,
                    "value": p.value,
                }
                for p in plan.selected
            ],
            "excluded": plan.excluded,
        }


# --- F5: email (FR-7) ---


def _email_account(a: EmailAccount, default_tags: list[Tag] | None = None) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "provider": a.provider.value,
        "email_address": a.email_address,
        "status": a.status.value,
        "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
        "last_error": a.last_error,
        "ingest_to_memory": a.ingest_to_memory,
        "auto_draft_replies": a.auto_draft_replies,
        "default_tags": [
            {"id": str(t.id), "kind": t.kind.value, "name": t.name, "color": t.color}
            for t in (default_tags or [])
        ],
        "version": a.version,
    }


def _email_message(m: EmailMessage) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "account_id": str(m.account_id),
        "from_addr": m.from_addr,
        "subject": m.subject,
        "snippet": m.snippet,
        "received_at": m.received_at.isoformat(),
        "thread_id": m.thread_id,
        "linked_task_id": (str(m.linked_task_id) if m.linked_task_id else None),
        "linked_note_id": (str(m.linked_note_id) if m.linked_note_id else None),
        "version": m.version,
    }


def _email_draft(j: EmailResponderJob) -> dict[str, Any]:
    return {
        "id": str(j.id),
        "message_id": str(j.message_id),
        "status": j.status,
        "draft_reply": j.draft_reply,
        "origin_model_id": j.origin_model_id,
        "error": j.error,
        "created_at": j.created_at.isoformat(),
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


@mcp.tool()
async def create_email_account(
    token: str,
    org_id: str,
    provider: str,
    email_address: str,
    secret: str,
    imap_host: str | None = None,
    imap_port: int | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
) -> dict[str, Any]:
    """Register an email account. The secret is stored encrypted and
    never returned."""
    async with _tenant(token, org_id) as (s, org, user):
        a = await email_svc.create_account(
            s,
            org_id=org,
            actor_id=user,
            provider=EmailProvider(provider),
            email_address=email_address,
            secret=secret,
            imap_host=imap_host,
            imap_port=imap_port,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
        )
        return _email_account(a)


@mcp.tool()
async def list_email_accounts(token: str, org_id: str) -> list[dict[str, Any]]:
    """List email accounts (no secrets), each with its default tags."""
    async with _tenant(token, org_id) as (s, org, _user):
        accounts = await email_svc.list_accounts(s, org_id=org)
        tags_by = await email_svc.default_tags_by_account(s, account_ids=[a.id for a in accounts])
        return [_email_account(a, tags_by.get(a.id, [])) for a in accounts]


@mcp.tool()
async def set_email_default_tags(
    token: str,
    org_id: str,
    account_id: str,
    expected_version: int,
    tag_ids: list[str],
) -> dict[str, Any]:
    """Replace this account's default tags (WS-1): a flat set
    auto-applied to everything ingested from the account (memory blobs
    on the 'email' channel, and email->task/note), so a per-client /
    per-project mailbox is born tagged. Returns the new version.

    The set may hold AT MOST ONE client tag and AT MOST ONE project tag
    (docs/adr/0003); more of either is rejected here, at configuration
    time, rather than on some later synced message. The project decides
    the client, so a client that is not that project's client is
    rejected too, and a project alone is enough. A client with no
    project is legal (mailbox bound to a client, notes stay personal
    until moved); anything else in the set is a free-form facet."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await email_svc.set_default_tags(
            s,
            org_id=org,
            actor_id=user,
            account_id=uuid.UUID(account_id),
            expected_version=expected_version,
            tag_ids=[uuid.UUID(t) for t in tag_ids],
        )
        return {"id": account_id, "version": version}


@mcp.tool()
async def set_email_ingest_to_memory(
    token: str, org_id: str, account_id: str, expected_version: int, enabled: bool
) -> dict[str, Any]:
    """Toggle whether this account's synced (non-bulk) messages are
    ingested into the 'email' memory channel (task 2a901dee). OFF by
    default: enabling ingests third-party PII into searchable memory, so
    it is an explicit per-account opt-in. Returns the new version."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await email_svc.update_account(
            s,
            org_id=org,
            actor_id=user,
            account_id=uuid.UUID(account_id),
            expected_version=expected_version,
            values={"ingest_to_memory": enabled},
        )
        return {"id": account_id, "version": version}


@mcp.tool()
async def sync_email_account(
    token: str, org_id: str, account_id: str, limit: int = 50
) -> dict[str, Any]:
    """Idempotently sync one account (known messages are skipped)."""
    async with _tenant(token, org_id) as (s, org, user):
        r = await email_svc.sync_account(
            s,
            org_id=org,
            actor_id=user,
            account_id=uuid.UUID(account_id),
            limit=limit,
        )
        return {
            "account_id": str(r.account_id),
            "fetched": r.fetched,
            "created": r.created,
            "ok": r.ok,
            "error": r.error,
        }


@mcp.tool()
async def list_email_messages(
    token: str, org_id: str, account_id: str | None = None
) -> list[dict[str, Any]]:
    """List ingested messages, optionally filtered by account."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await email_svc.list_messages(
            s,
            org_id=org,
            account_id=uuid.UUID(account_id) if account_id else None,
        )
        return [_email_message(m) for m in rows]


@mcp.tool()
async def email_thread(token: str, org_id: str, thread_id: str) -> list[dict[str, Any]]:
    """Fetch a whole email thread (oldest first) by its provider thread id
    (WS-2) — recall a full conversation as a unit to answer or reply with
    context, instead of reconstructing it from individual search hits."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await email_svc.get_thread(s, org_id=org, thread_id=thread_id)
        return [_email_message(m) for m in rows]


@mcp.tool()
async def email_to_task(
    token: str,
    org_id: str,
    message_id: str,
    project_tag_id: str | None = None,
) -> dict[str, Any]:
    """Create a task from a message, with a source link."""
    async with _tenant(token, org_id) as (s, org, user):
        task_id = await email_svc.email_to_task(
            s,
            org_id=org,
            actor_id=user,
            message_id=uuid.UUID(message_id),
            project_tag_id=(uuid.UUID(project_tag_id) if project_tag_id else None),
        )
        return {"task_id": str(task_id)}


@mcp.tool()
async def email_to_note(
    token: str,
    org_id: str,
    message_id: str,
    tag_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create a note from a message (WS-3), with a back-link. The account's
    default tags are applied automatically; ``tag_ids`` adds more."""
    async with _tenant(token, org_id) as (s, org, user):
        note_id = await email_svc.email_to_note(
            s,
            org_id=org,
            actor_id=user,
            message_id=uuid.UUID(message_id),
            tag_ids=[uuid.UUID(t) for t in (tag_ids or [])],
        )
        return {"note_id": str(note_id)}


@mcp.tool()
async def send_email(
    token: str,
    org_id: str,
    account_id: str,
    to_addrs: list[str],
    subject: str,
    body_text: str,
) -> dict[str, Any]:
    """Send a message from an account."""
    async with _tenant(token, org_id) as (s, org, user):
        sent = await email_svc.send_message(
            s,
            org_id=org,
            actor_id=user,
            account_id=uuid.UUID(account_id),
            to_addrs=to_addrs,
            subject=subject,
            body_text=body_text,
        )
        return {"sent_id": sent}


@mcp.tool()
async def reply_email(token: str, org_id: str, message_id: str, body_text: str) -> dict[str, Any]:
    """Reply in-thread to an ingested message."""
    async with _tenant(token, org_id) as (s, org, user):
        sent = await email_svc.reply_to_message(
            s,
            org_id=org,
            actor_id=user,
            message_id=uuid.UUID(message_id),
            body_text=body_text,
        )
        return {"sent_id": sent}


@mcp.tool()
async def set_email_auto_draft_replies(
    token: str, org_id: str, account_id: str, expected_version: int, enabled: bool
) -> dict[str, Any]:
    """Toggle the autonomous responder for this account (WS-4). When enabled
    AND the deployment's responder is on, each new non-bulk message gets a
    DRAFT reply (withheld until a human approves it; nothing auto-sends). OFF
    by default. Returns the new version."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await email_svc.update_account(
            s,
            org_id=org,
            actor_id=user,
            account_id=uuid.UUID(account_id),
            expected_version=expected_version,
            values={"auto_draft_replies": enabled},
        )
        return {"id": account_id, "version": version}


@mcp.tool()
async def draft_email_reply(token: str, org_id: str, message_id: str) -> dict[str, Any]:
    """On-demand (WS-4): queue a draft reply for one message (idempotent).
    The responder worker drafts it; review it with ``list_email_drafts`` and
    send with ``approve_email_draft``. Nothing is sent automatically."""
    async with _tenant(token, org_id) as (s, org, user):
        job_id = await email_svc.enqueue_draft(
            s, org_id=org, actor_id=user, message_id=uuid.UUID(message_id)
        )
        return {"job_id": str(job_id)}


@mcp.tool()
async def list_email_drafts(token: str, org_id: str) -> list[dict[str, Any]]:
    """List drafted replies awaiting human review (WS-4)."""
    async with _tenant(token, org_id) as (s, org, _user):
        return [_email_draft(j) for j in await email_svc.list_drafts(s, org_id=org)]


@mcp.tool()
async def approve_email_draft(
    token: str, org_id: str, job_id: str, body_text: str | None = None
) -> dict[str, Any]:
    """Approve a drafted reply and SEND it in-thread (WS-4). ``body_text``
    overrides the draft so you can edit before sending."""
    async with _tenant(token, org_id) as (s, org, user):
        sent = await email_svc.approve_draft(
            s,
            org_id=org,
            actor_id=user,
            job_id=uuid.UUID(job_id),
            body_text=body_text,
        )
        return {"sent_id": sent}


@mcp.tool()
async def reject_email_draft(token: str, org_id: str, job_id: str) -> dict[str, Any]:
    """Discard a drafted reply without sending (WS-4)."""
    async with _tenant(token, org_id) as (s, org, user):
        await email_svc.reject_draft(s, org_id=org, actor_id=user, job_id=uuid.UUID(job_id))
        return {"id": job_id, "status": "rejected"}


# --- F5b: billing / metering (FR-15) ---


def _usage(r: UsageRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "operation_id": r.operation_id,
        "model_id": r.model_id,
        "op": r.op,
        "basis": r.basis.value,
        "units_in": str(r.units_in),
        "units_out": str(r.units_out),
        "credits": str(r.credits),
    }


def _rate_card(c: RateCard) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "model_id": c.model_id,
        "provider": c.provider,
        "unit": c.unit.value,
        "credits_per_input": str(c.credits_per_input),
        "credits_per_output": str(c.credits_per_output),
        "is_active": c.is_active,
        "version": c.version,
    }


@mcp.tool()
async def billing_balance(token: str, org_id: str) -> dict[str, Any]:
    """Current credit balance for the org."""
    async with _tenant(token, org_id) as (s, org, _user):
        return {"balance": str(await billing_svc.balance(s, org_id=org))}


@mcp.tool()
async def grant_credits(
    token: str, org_id: str, amount: float, reason: str | None = None
) -> dict[str, Any]:
    """Admin: top up credits (manual grant; v1 has no payment gateway)."""
    async with _tenant(token, org_id) as (s, org, user):
        new_balance = await billing_svc.grant_credits(
            s,
            org_id=org,
            actor_id=user,
            amount=Decimal(str(amount)),
            reason=reason,
        )
        return {"balance": str(new_balance)}


@mcp.tool()
async def meter_usage(
    token: str,
    org_id: str,
    operation_id: str,
    op: str,
    model_id: str | None = None,
    units_in: float = 0.0,
    units_out: float = 0.0,
    basis: str = "local",
) -> dict[str, Any]:
    """Idempotent metered debit (re-running the same operation_id does
    not charge twice)."""
    async with _tenant(token, org_id) as (s, org, user):
        record = await billing_svc.meter(
            s,
            org_id=org,
            actor_id=user,
            operation_id=operation_id,
            op=op,
            model_id=model_id,
            units_in=Decimal(str(units_in)),
            units_out=Decimal(str(units_out)),
            basis=CostBasis(basis),
        )
        return _usage(record)


@mcp.tool()
async def upsert_rate_card(
    token: str,
    org_id: str,
    model_id: str,
    provider: str,
    credits_per_input: float = 0.0,
    credits_per_output: float = 0.0,
) -> dict[str, Any]:
    """Admin: create or update a model rate card."""
    async with _tenant(token, org_id) as (s, org, user):
        card = await billing_svc.upsert_rate_card(
            s,
            org_id=org,
            actor_id=user,
            model_id=model_id,
            provider=provider,
            values={
                "credits_per_input": Decimal(str(credits_per_input)),
                "credits_per_output": Decimal(str(credits_per_output)),
            },
        )
        return _rate_card(card)


@mcp.tool()
async def list_rate_cards(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the org rate cards."""
    async with _tenant(token, org_id) as (s, org, _user):
        return [_rate_card(c) for c in await billing_svc.list_rate_cards(s, org_id=org)]


@mcp.tool()
async def list_usage(token: str, org_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """List recent metered usage records."""
    async with _tenant(token, org_id) as (s, org, _user):
        return [_usage(r) for r in await billing_svc.list_usage(s, org_id=org, limit=limit)]


# --- F6: hierarchical memory (FR-8) ---


def _blob(b: MemoryBlob, tags: list[Tag] | None = None) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "project_id": str(b.project_id) if b.project_id else None,
        "namespace": b.namespace,
        "tier": b.tier,
        "text": b.text,
        "model_id": b.model_id,
        "cluster_id": str(b.cluster_id) if b.cluster_id else None,
        # Provenance (migration 0085): the authoring identity (a user or an
        # ai_assistant, ADR-0028) and the LLM that produced the text (NULL for
        # a human author) -- so recall shows WHO wrote a memory, not just what.
        "created_by": str(b.created_by) if b.created_by else None,
        "origin_model_id": b.origin_model_id,
        "tags": [
            {"id": str(g.id), "kind": g.kind.value, "name": g.name, "color": g.color}
            for g in (tags or [])
        ],
    }


def _retrieval_meta(m: memory_svc.RetrievalMeta) -> dict[str, Any]:
    """Serialize the recall diagnostics (task 4f3c2207): why a retrieval
    returned what it did, so a caller can tell a genuinely-empty result from
    silently-degraded (keyword-only) recall."""
    return {
        "query_embedded": m.query_embedded,
        "dense_branch_contributed": m.dense_branch_contributed,
        "dense_rejected_by_floor": m.dense_rejected_by_floor,
        "keyword_only_hits": m.keyword_only_hits,
        "abstained": m.abstained,
        "abstain_reason": m.abstain_reason,
        "rerank_failed": m.rerank_failed,
    }


@mcp.tool()
async def memory_write(
    token: str,
    org_id: str,
    text: str,
    operation_id: str,
    project_id: str | None = None,
    namespace: str = "note",
    source_kind: str | None = None,
    source_id: str | None = None,
    tag_ids: list[str] | None = None,
    channel_tag_id: str | None = None,
    channel_key: str | None = None,
    origin_model_id: str | None = None,
) -> dict[str, Any]:
    """Write a memory blob (embedding metered when produced; degrades
    to keyword-only/FTS if the model is unavailable, never errors).
    Optional source provenance for GDPR erasure. Tags = ``tag_ids`` + memory
    channel + tags inherited from tagged sources. Channel by id
    (``channel_tag_id``) or stable slug (``channel_key``); if both
    given they must agree. (org, project) boundary is hard.

    Authorship is recorded automatically: the blob's ``created_by`` is YOUR
    ai_assistant identity when you call over an agent token (the real author,
    first-class now, not the ``agent/<handle>`` tag-lane convention). Pass
    ``origin_model_id`` to record which LLM produced the text."""
    async with _tenant(token, org_id) as (s, org, user):
        # Author = the ai_assistant identity behind the agent token (None on
        # the stdio / human path, where write_blob defaults to the user
        # identity). Mirrors task_create's authorship threading.
        author_identity_id, _tok = await _resolve_agent_context(s, org)
        sources = (
            [(source_kind, source_id)] if source_kind is not None and source_id is not None else []
        )
        blob = await memory_svc.write_blob(
            s,
            org_id=org,
            actor_id=user,
            project_id=uuid.UUID(project_id) if project_id else None,
            text_body=text,
            operation_id=operation_id,
            namespace=namespace,
            sources=sources,
            tag_ids=[uuid.UUID(t) for t in (tag_ids or [])],
            channel_tag_id=uuid.UUID(channel_tag_id) if channel_tag_id else None,
            channel_key=channel_key,
            created_by_identity_id=author_identity_id,
            origin_model_id=origin_model_id,
        )
        tagmap = await memory_svc.tags_by_blob(s, blob_ids=[blob.id])
        return _blob(blob, tagmap.get(blob.id))


@mcp.tool()
async def memory_search(
    token: str,
    org_id: str,
    query: str,
    operation_id: str,
    project_id: str | None = None,
    limit: int = 10,
    offset: int = 0,
    tag_ids: list[str] | None = None,
    channel_tag_id: str | None = None,
    channel_key: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Hybrid RRF retrieval within the (org, project) boundary.
    Degrades to keyword-only without embedder. ``tag_ids``,
    ``channel_tag_id`` and ``channel_key`` narrow within the boundary
    (facets, never cross). ``created_by`` is a provenance filter: pass an
    identity id to recall ONLY what that author (a user or ai_assistant, e.g.
    another agent) wrote, while shared reads stay unrestricted when omitted.
    Channel by id or stable slug; if both given they must agree. Deterministic
    order. ``offset`` pages the ranked results (ranked retrieval has no stable
    keyset, so offset not cursor): the top ``limit`` after skipping ``offset``.

    Returns ``{hits, meta}``. ``meta`` (RetrievalMeta) tells you WHY: an
    empty ``hits`` with ``meta.query_embedded=false`` or
    ``meta.dense_rejected_by_floor>0`` means recall silently degraded to
    keyword-only, not that nothing was relevant (per-hit ``blob.model_id``
    ='none' also flags a keyword-only row)."""
    async with _tenant(token, org_id) as (s, org, user):
        hits, meta = await memory_svc.retrieve_with_meta(
            s,
            org_id=org,
            actor_id=user,
            project_id=uuid.UUID(project_id) if project_id else None,
            query=query,
            operation_id=operation_id,
            limit=limit + max(0, offset),
            tag_ids=[uuid.UUID(t) for t in (tag_ids or [])],
            channel_tag_id=uuid.UUID(channel_tag_id) if channel_tag_id else None,
            channel_key=channel_key,
            created_by=uuid.UUID(created_by) if created_by else None,
        )
        page = hits[offset : offset + limit] if offset > 0 else hits[:limit]
        tagmap = await memory_svc.tags_by_blob(s, blob_ids=[h.blob.id for h in page])
        return {
            "hits": [
                {
                    "blob": _blob(h.blob, tagmap.get(h.blob.id)),
                    "rrf": h.rrf,
                    # Why this hit ranked here (WS-B2 / R8): the per-stage RRF
                    # branch scores + rerank logit, the winning chunk, its
                    # snippet, and the humus provenance marker -- so an agent
                    # can reason about retrieval quality, not just the order.
                    "scores_by_stage": h.scores_by_stage,
                    "chunk_index": h.chunk_index,
                    "chunk_snippet": h.chunk_snippet,
                    "provenance": h.provenance,
                }
                for h in page
            ],
            "meta": _retrieval_meta(meta),
        }


@mcp.tool()
async def graph_focus_context(
    token: str,
    org_id: str,
    seed: str,
    budget: int = 24,
    query: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """PPR-seeded reading set around a seed note: the relevant subgraph and
    nothing else (ADR-0034). Returns up to ``budget`` notes ordered by
    personalised-PageRank mass from ``seed`` (its "neighbourhood of
    attention"), each with a title + snippet so you can decide what to read
    without follow-up lookups. With ``query`` the neighbourhood is re-ranked
    by late RRF fusion with hybrid retrieval, so the parts of the subgraph
    that actually answer the question rise (graph proximity AND content
    relevance). Read-only; vendor-neutral (no LLM)."""
    async with _tenant(token, org_id) as (s, org, user):
        nodes = await focus_context_svc.focus_context(
            s,
            org_id=org,
            actor_id=user,
            seed_id=uuid.UUID(seed),
            budget=budget,
            query=query,
            project_id=uuid.UUID(project_id) if project_id else None,
        )
        return [
            {
                "note_id": str(n.note_id),
                "title": n.title,
                "snippet": n.snippet,
                "ppr_mass": n.ppr_mass,
                "score": n.score,
                "provenance": n.provenance,
            }
            for n in nodes
        ]


@mcp.tool()
async def graph_walk(
    token: str,
    org_id: str,
    seed: str,
    mode: str = "focused",
    budget: int = 24,
) -> list[dict[str, Any]]:
    """Traverse the note graph ("micelio") rooted at ``seed`` and return the
    walked notes as a reading set (WS-B2).

    ``mode='focused'`` returns the seed's personalised-PageRank neighbourhood
    ordered by induced mass (its "neighbourhood of attention").
    ``mode='free_wander'`` runs a Node2Vec biased random walk that drifts
    across the graph for cross-domain serendipity (humus-biased, ADR-0034).
    ``mode='bounded'`` runs the best-first thresholded traversal (Fase 1,
    task 561c6aca): same reading-set shape, cost independent of the org's
    graph size -- ``step`` is the hop distance, ``weight`` the best path
    weight. Each step carries ``title`` + ``snippet`` + ``provenance`` so you
    can navigate multi-hop WITHOUT a lookup per node. For a QUERY-aware
    reading set use ``graph_focus_context`` instead. Read-only; no LLM."""
    async with _tenant(token, org_id) as (s, org, user):
        steps = await focus_context_svc.walk_context(
            s,
            org_id=org,
            actor_id=user,
            seed_id=uuid.UUID(seed),
            mode=mode,
            budget=budget,
        )
        return [
            {
                "note_id": str(w.note_id),
                "step": w.step,
                "weight": w.weight,
                "title": w.title,
                "snippet": w.snippet,
                "provenance": w.provenance,
            }
            for w in steps
        ]


@mcp.tool()
async def kg_extract(token: str, org_id: str, note_id: str) -> dict[str, Any]:
    """Extract a TEMPORAL KNOWLEDGE GRAPH (typed entities + relation facts)
    from a note's body using the org's metered LLM (ADR-0044). Entities are
    resolved/deduped; facts are written EFFECTIVE (user-initiated) with
    bi-temporal validity (valid_from/valid_to) and are idempotent per triple.
    Returns the entity/fact counts + new edge ids."""
    async with _tenant(token, org_id) as (s, org, user):
        res = await kg_svc.extract_facts(s, org_id=org, actor_id=user, note_id=uuid.UUID(note_id))
        return {
            "entities": res.entities,
            "facts": res.facts,
            "edge_ids": [str(e) for e in res.edge_ids],
            "model_id": res.model_id,
        }


@mcp.tool()
async def kg_entities(
    token: str,
    org_id: str,
    query: str,
    entity_type: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Look up knowledge-graph entities whose name matches ``query`` (ADR-0044).
    Optionally filter by ``entity_type`` (person | organization | project |
    place | product | event | concept | other). Returns id + type + canonical
    name -- feed an id into ``kg_neighbors`` to read its facts."""
    async with _tenant(token, org_id) as (s, org, user):
        ents = await kg_svc.search_entities(
            s, org_id=org, actor_id=user, query=query, entity_type=entity_type, limit=limit
        )
        return [{"id": str(e.id), "type": e.entity_type, "name": e.name} for e in ents]


@mcp.tool()
async def kg_neighbors(
    token: str,
    org_id: str,
    entity: str,
    depth: int = 1,
    as_of: str | None = None,
    believed_as_of: str | None = None,
) -> list[dict[str, Any]]:
    """Effective knowledge-graph facts around an entity id (ADR-0044).
    ``depth=1`` = the entity's direct facts; ``depth>1`` = a multi-hop
    traversal for cross-entity questions ("which projects did X and Y share").

    Bi-temporal, both axes ISO date/datetime:
    - ``as_of`` clamps the VALID window (the world) -- "where did X work in
      2024" returns the fact true THEN, not the current one.
    - ``believed_as_of`` clamps the TRANSACTION window (the system's belief) --
      reconstruct what was known at that instant, BEFORE a later invalidation
      (a fact retracted yesterday is still visible at ``believed_as_of`` =
      last week). Default: only the currently-believed facts.

    Each fact carries subject/predicate/object + its validity window."""
    async with _tenant(token, org_id) as (s, org, user):
        tz = await _caller_tz(s, user)
        as_of_dt = _to_instant(as_of, tz) if as_of else None
        tx_as_of_dt = _to_instant(believed_as_of, tz) if believed_as_of else None
        entity_id = uuid.UUID(entity)
        if depth <= 1:
            facts = await kg_svc.entity_facts(
                s,
                org_id=org,
                actor_id=user,
                entity_id=entity_id,
                as_of=as_of_dt,
                tx_as_of=tx_as_of_dt,
            )
        else:
            facts = await kg_svc.traverse(
                s,
                org_id=org,
                actor_id=user,
                seed_id=entity_id,
                depth=depth,
                as_of=as_of_dt,
                tx_as_of=tx_as_of_dt,
            )
        return [
            {
                "edge_id": str(f.edge_id),
                "subject": {"id": str(f.subject_id), "name": f.subject_name},
                "predicate": f.predicate,
                "object": {"id": str(f.object_id), "name": f.object_name},
                "valid_from": f.valid_from.isoformat() if f.valid_from else None,
                "valid_to": f.valid_to.isoformat() if f.valid_to else None,
                "confidence": float(f.confidence) if f.confidence is not None else None,
            }
            for f in facts
        ]


@mcp.tool()
async def search(
    token: str,
    org_id: str,
    q: str,
    operation_id: str = "search",
    kinds: list[str] | None = None,
    project_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
    tag_ids: list[str] | None = None,
    channel_keys: list[str] | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
    rerank: bool = False,
    task_scope: str = "org",
    due_before: str | None = None,
    assignee_handles: list[str] | None = None,
    state_id: str | None = None,
) -> dict[str, Any]:
    """Unified search across tasks/notes/blobs; the TASK branch is org-wide
    even when ``project_id`` is set (each hit's ``scope`` says 'org' or
    'project' so you are never misled).

    ``kinds`` defaults to ``['task', 'blob', 'note']``. note/blob hits are
    project-scoped to ``project_id`` WHEN YOU PASS ONE; **omitting it searches
    every project**, which is what "where did we decide X" needs, since not
    knowing where is the question. Until 2026-09-08 omitting it meant the
    perimeter of blobs belonging to NO project, and a gold set of twenty such
    questions found three: task blobs live at that perimeter by design and
    scored 3 of 6, note blobs carry their note's project and scored 0 of 14.
    Task hits are org-wide UNLESS
    ``task_scope='project'`` (which ANDs the caller's project tag into the
    task branch). Task facets ``due_before`` (ISO date/datetime),
    ``assignee_handles``, ``state_id`` narrow the task branch, so "tasks due
    today assigned to X" is answerable here too. ``note`` hits carry
    ``note_id`` + ``part_id`` + a title; results carry an ``ts_headline``
    snippet. Use this over ``memory_search`` for "everything that mentions
    X". ``rerank=True`` opts into the cross-encoder top-K pass.

    An ENTITY CODE (8+ hex digits, e.g. ``f62ff51d`` or a full UUID) is
    answered as an exact LOOKUP, not a similarity question: the entity whose
    id starts with it, plus the places that cite it verbatim. No embedding,
    no stemming. An unknown code returns an EMPTY list, which is the honest
    answer -- do not read it as "the search is broken".

    Returns ``{hits, meta}``. Each hit carries ``model_id`` ('none' = a
    keyword-only row, no dense vector). ``offset`` pages the ranked results
    (offset, not cursor: ranked retrieval has no stable keyset). ``meta``
    (RetrievalMeta) exposes whether the query embedded and whether the dense
    branch contributed / was rejected by the per-org similarity floor, so an
    empty or thin result distinguishes 'nothing relevant' from 'recall
    silently degraded'.
    """
    async with _tenant(token, org_id) as (s, org, user):
        due_before_dt = _to_instant(due_before, await _caller_tz(s, user)) if due_before else None
        hits, meta = await task_search_svc.search_unified_with_meta(
            s,
            org_id=org,
            actor_id=user,
            project_id=uuid.UUID(project_id) if project_id else None,
            query=q,
            kinds=kinds or ["task", "blob", "note"],
            tag_ids=[uuid.UUID(t) for t in (tag_ids or [])],
            channel_keys=channel_keys or [],
            limit=limit + max(0, offset),
            include_archived=include_archived,
            include_deleted=include_deleted,
            rerank=rerank,
            operation_id=operation_id,
            task_scope=task_scope,
            due_before=due_before_dt,
            assignee_handles=assignee_handles,
            task_state_id=uuid.UUID(state_id) if state_id else None,
        )
        page = hits[offset : offset + limit] if offset > 0 else hits[:limit]
        return {
            "hits": [
                {
                    "kind": h.kind,
                    "scope": h.scope,
                    "model_id": h.model_id,
                    "task_id": str(h.task_id) if h.task_id else None,
                    "note_id": str(h.note_id) if h.note_id else None,
                    "part_id": str(h.part_id) if h.part_id else None,
                    "blob_id": str(h.blob_id),
                    "title": h.title,
                    "snippet": h.snippet,
                    "score": h.score,
                    # Why this hit ranked here, same shape `memory_search`
                    # returns. Branch entries are 1-BASED RANKS, not
                    # scores; only "rrf" is a score and it equals "score"
                    # above. The fused value alone cannot tell a lexical
                    # hit from a dense-only one: RRF fuses by rank, so a
                    # dense-only hit is exactly 0.2/(60+rank) whatever its
                    # cosine was. Empty on the entity-code path, where
                    # nothing was ranked.
                    "scores_by_stage": h.scores_by_stage,
                }
                for h in page
            ],
            "meta": _retrieval_meta(meta),
        }


@mcp.tool()
async def memory_erase(token: str, org_id: str, source_kind: str, source_id: str) -> dict[str, Any]:
    """GDPR erasure by provenance; cascades to embedding/sources."""
    async with _tenant(token, org_id) as (s, org, user):
        deleted = await memory_svc.gdpr_erase(
            s,
            org_id=org,
            actor_id=user,
            source_kind=source_kind,
            source_id=source_id,
        )
        return {"deleted": deleted}


@mcp.tool()
async def memory_consolidate(
    token: str,
    org_id: str,
    blob_ids: list[str],
    operation_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Merge same-(org, project) blobs into one concept, provenance
    preserved. Never crosses org/project."""
    async with _tenant(token, org_id) as (s, org, user):
        blob = await memory_svc.consolidate(
            s,
            org_id=org,
            actor_id=user,
            project_id=uuid.UUID(project_id) if project_id else None,
            blob_ids=[uuid.UUID(b) for b in blob_ids],
            operation_id=operation_id,
        )
        tagmap = await memory_svc.tags_by_blob(s, blob_ids=[blob.id])
        return _blob(blob, tagmap.get(blob.id))


@mcp.tool()
async def memory_delete_blob(token: str, org_id: str, blob_id: str) -> dict[str, Any]:
    """Delete a single memory entry (hard delete; cascades to its
    tags/sources/vector). Member-level, RLS-scoped: a foreign/unknown
    blob id is memory.not_found. Distinct from ``memory_erase`` (which
    removes a provenance link and only deletes a blob left orphaned)."""
    async with _tenant(token, org_id) as (s, org, user):
        await memory_svc.delete_blob(
            s,
            org_id=org,
            actor_id=user,
            blob_id=uuid.UUID(blob_id),
        )
        return {"blob_id": blob_id, "deleted": True}


@mcp.tool()
async def memory_status(token: str, org_id: str) -> dict[str, Any]:
    """Whether semantic (vector) retrieval is available, or memory is
    running keyword-only because the optional embedding model is not
    installed. Member-level."""
    async with _tenant(token, org_id) as (_s, _org, _user):
        return {"semantic": embedder_available()}


@mcp.tool()
async def memory_get_blob(token: str, org_id: str, blob_id: str) -> dict[str, Any]:
    """Read one memory blob by id (with its tags), when you already hold
    the id from an earlier ``memory_search`` / ``search`` hit and do not
    want to re-query. Member-level, RLS-scoped: a foreign/unknown id is
    memory.not_found."""
    async with _tenant(token, org_id) as (s, org, _user):
        blob = await memory_svc.get_blob(s, org_id=org, blob_id=uuid.UUID(blob_id))
        tagmap = await memory_svc.tags_by_blob(s, blob_ids=[blob.id])
        return _blob(blob, tagmap.get(blob.id))


@mcp.tool()
async def memory_attach_tag(token: str, org_id: str, blob_id: str, tag_id: str) -> dict[str, Any]:
    """Curate memory by hand: attach an existing tag to a memory blob
    (idempotent). Lets the agent re-file a blob into a memory channel or
    project after it was written. Member-level."""
    async with _tenant(token, org_id) as (s, org, user):
        await memory_svc.attach_blob_tag(
            s, org_id=org, actor_id=user, blob_id=uuid.UUID(blob_id), tag_id=uuid.UUID(tag_id)
        )
        return {"blob_id": blob_id, "tag_id": tag_id}


@mcp.tool()
async def memory_detach_tag(token: str, org_id: str, blob_id: str, tag_id: str) -> dict[str, Any]:
    """Remove a tag from a memory blob (idempotent). Member-level."""
    async with _tenant(token, org_id) as (s, org, user):
        await memory_svc.detach_blob_tag(
            s, org_id=org, actor_id=user, blob_id=uuid.UUID(blob_id), tag_id=uuid.UUID(tag_id)
        )
        return {"blob_id": blob_id, "tag_id": tag_id, "removed": True}


@mcp.tool()
async def memory_recompute_tiers(
    token: str,
    org_id: str,
    half_life_days: float = 30.0,
    hot_threshold: float = 5.0,
    warm_threshold: float = 1.0,
) -> dict[str, int]:
    """Recompute the hot/warm/cold tier of EVERY memory blob in the
    workspace from a decayed access score + importance (ADR-0016). Never
    deletes: a rarely-used blob is demoted to cold, still queryable.
    Returns the per-tier counts. Member-level; normally run by the
    re-embedding worker, exposed here as an escape hatch."""
    async with _tenant(token, org_id) as (s, org, _user):
        return await memory_svc.recompute_tier(
            s,
            org_id=org,
            half_life_days=half_life_days,
            hot_threshold=hot_threshold,
            warm_threshold=warm_threshold,
        )


@mcp.tool()
async def memory_migration_status(token: str, org_id: str) -> dict[str, int]:
    """Embedding-backfill coverage for the workspace: ``{total, migrated,
    pending, hosted, stale}``. ``total`` = memory blobs with text;
    ``migrated`` = those that already have a local dense vector;
    ``pending`` = local-tier backfill still to do (rows written
    keyword-only, ``model_id='none'``); ``hosted`` = blobs that also have
    the optional per-org hosted vector.

    ``stale`` = blobs that HAVE a local vector, written by a model that is
    no longer the active one. They are counted in ``migrated`` as well,
    because they are embedded; this answers the different question of
    whether the dense branch is ignoring part of a corpus that reports
    itself fully migrated. Vectors from different models are not
    comparable, so those rows are excluded from the dense kNN until the
    sweep re-embeds them, and the number falls to zero as it converges.

    Member-level, read-only. Pair with ``memory_migrate`` to drain
    ``pending`` and ``stale`` to 0 and re-enable semantic recall over the
    back-catalogue."""
    async with _tenant(token, org_id) as (s, _org, _user):
        return await embedding_svc.migration_status(s)


@mcp.tool()
async def memory_migrate(token: str, org_id: str, batch_size: int = 100) -> dict[str, Any]:
    """Backfill missing dense embeddings for this workspace's memory blobs
    (rows written keyword-only, ``model_id='none'``) in ONE batch, then
    report coverage. Incremental + idempotent: embeds up to ``batch_size``
    rows under an IS-NULL guard and returns ``{embedded, status}``; re-call
    until ``status.pending`` is 0. Runs in the API process (which has the
    embedder), so it works even when the background worker image can't embed.
    The autonomous worker loop does the same on a timer; this is the
    on-demand escape hatch. Member-level."""
    async with _tenant(token, org_id) as (s, org, _user):
        embedded = await embedding_svc.run_embedding_backfill(s, org, batch_size=batch_size)
        return {"embedded": embedded, "status": await embedding_svc.migration_status(s)}


# --- Memory channels (controlled, seeded vocabulary; FR-8) ---------
#
# Listing is member-level (the agent needs it to pick a channel);
# create/rename/enable-disable is PLATFORM-ADMIN only. The REST surface
# gates this with the sudo rule "capability (is_admin) AND active
# X-Admin-Mode elevation". MCP is a tool protocol with no per-call
# elevation header, so the equivalent gate here is the capability
# itself (``users.is_admin``); a non-admin caller is rejected exactly
# like the REST 403. (Mirrors the REST gating note in
# api/src/mycelium_api/routers/memory_channels.py.)
_CANONICAL_CHANNEL_KEYS = frozenset(k for k, _ in taxonomy.CANONICAL_MEMORY_CHANNELS)


def _channel(t: Tag) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "name": t.name,
        "system_key": t.system_key,
        "enabled": t.status == "active",
        "seeded": t.system_key in _CANONICAL_CHANNEL_KEYS,
        "description": taxonomy.channel_description(t.system_key),
        "version": t.version,
    }


async def _require_platform_admin(user_id: uuid.UUID) -> None:
    """Platform-admin capability gate for channel management. ``users``
    is global (not RLS-scoped), so it is read via an admin session,
    exactly like the REST admin surface. Raises the same code the REST
    layer maps to 403 for a non-admin."""
    async with admin_session() as s:
        u = (await s.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if u is None or not u.is_admin:
        raise ForbiddenError(MessageCode.CHANNEL_ADMIN_ONLY)


@mcp.tool()
async def memory_channels_list(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the tenant's configured memory channels (seeds the
    canonical four on first call). Any authenticated member may list --
    the agent needs it to pick a channel by ``system_key``. RLS-scoped."""
    async with _tenant(token, org_id) as (s, org, _user):
        channels = await taxonomy.list_memory_channels(s, org_id=org)
        return [_channel(t) for t in channels]


@mcp.tool()
async def memory_channel_create(
    token: str,
    org_id: str,
    name: str,
    system_key: str | None = None,
) -> dict[str, Any]:
    """Create a custom memory channel. PLATFORM-ADMIN only (see the
    module note: REST also requires an active X-Admin-Mode elevation;
    MCP gates on the is_admin capability)."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_platform_admin(user)
        tag = await taxonomy.create_memory_channel(
            s, org_id=org, actor_id=user, name=name, system_key=system_key
        )
        return _channel(tag)


@mcp.tool()
async def memory_channel_update(
    token: str,
    org_id: str,
    channel_id: str,
    name: str | None = None,
    enabled: bool | None = None,
    system_key: str | None = None,
) -> dict[str, Any]:
    """Rename and/or enable/disable a memory channel. PLATFORM-ADMIN
    only. A seeded channel may be renamed and disabled but its
    ``system_key`` is immutable (channel.key_immutable)."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_platform_admin(user)
        tag = await taxonomy.update_memory_channel(
            s,
            org_id=org,
            actor_id=user,
            tag_id=uuid.UUID(channel_id),
            name=name,
            enabled=enabled,
            system_key=system_key,
        )
        return _channel(tag)


@mcp.tool()
async def memory_channel_delete(token: str, org_id: str, channel_id: str) -> dict[str, Any]:
    """Delete a custom memory channel. PLATFORM-ADMIN only. A seeded
    channel is not deletable -- disable it instead
    (channel.seeded_undeletable)."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_platform_admin(user)
        await taxonomy.delete_memory_channel(
            s, org_id=org, actor_id=user, tag_id=uuid.UUID(channel_id)
        )
        return {"deleted": channel_id}


# --- F6b: notes / conversation / canonical intent (FR-16) ---


def _note_part(p: Any) -> dict[str, Any]:
    """Project a NotePart ORM row to a JSON-serialisable dict for the
    MCP get_note payload (task 7070a456 Phase 3). The UI collapse
    state is web-only and not returned over MCP -- LLM consumers
    treat every part as expanded."""
    return {
        "id": str(p.id),
        "note_id": str(p.note_id),
        "ord": p.ord,
        "body": p.body or "",
        "lang": p.lang,
        "merged_from_note_id": (str(p.merged_from_note_id) if p.merged_from_note_id else None),
        "version": p.version,
    }


def _note_part_outline(p: Any) -> dict[str, Any]:
    """Body-free projection of a NotePart for the outline / table of
    contents of a long note: id, ord, title, lang, UTF-8 byte length and
    the first non-empty line (``head``). Lets an LLM pick which part to
    read or edit without pulling every body into context (the get_note /
    list_note_parts payload-economy primitive)."""
    body = p.body or ""
    head = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return {
        "id": str(p.id),
        "note_id": str(p.note_id),
        "ord": p.ord,
        "title": p.title,
        "lang": p.lang,
        "bytes": len(body.encode("utf-8")),
        "head": head[:120],
        "version": p.version,
    }


def _note(
    n: Note,
    tags: list[Tag] | None = None,
    primary_task_id: uuid.UUID | None = None,
    include_transcript: bool = True,
    parts: list[Any] | None = None,
    transcript: str | None = None,
    part_bodies: bool = True,
) -> dict[str, Any]:
    # docs/adr/0029 P3: ``task_id`` comes from the typed link table.
    # Phase 6 final (task 1cd8bc0a): the ``transcript`` column is
    # gone; we derive the field from the parts list when available
    # or accept an explicit ``transcript`` from the caller
    # (single-row paths that didn't load parts).
    # ``parts`` (task 7070a456 Phase 3): when supplied, embed the
    # ordered note_part rows so an LLM gets the structured body in
    # one call. list_notes leaves it None for payload economy.
    # Migration 0016: ``project_id`` is the project-kind tag in
    # ``tags`` (junction is the source of truth, like task_tags).
    project_tag_id = next(
        (g.id for g in (tags or []) if getattr(g.kind, "value", g.kind) == "project"),
        None,
    )
    out: dict[str, Any] = {
        "id": str(n.id),
        "project_id": str(project_tag_id) if project_tag_id else None,
        "task_id": str(primary_task_id) if primary_task_id else None,
        "kind": n.kind.value,
        "status": n.status.value,
        "title": n.title,
        "version": n.version,
        "tags": [_tag_brief(g) for g in (tags or [])],
        # Lifecycle + provenance the picker/agent needs to read a note's
        # state without a second call (audit #6b/#7). maturity / is_archived
        # / timestamps are always-set columns; review_state / summary /
        # deleted_at ride _compact semantics (omitted when unset = zero
        # token cost).
        "maturity": n.maturity,
        "is_archived": n.is_archived,
        # Stated even at its default, unlike the lean task row: this one
        # serializer answers ``get_note`` too, and a caller that just flipped
        # the scope has no other way to read the flip back.
        "index_scope": n.index_scope.value,
        "created_at": n.created_at.isoformat(),
        "updated_at": n.updated_at.isoformat(),
    }
    if n.review_state is not None:
        out["review_state"] = n.review_state
    if n.summary is not None:
        out["summary"] = n.summary
    if n.deleted_at is not None:
        out["deleted_at"] = n.deleted_at.isoformat()
    if include_transcript and part_bodies:
        if parts:
            out["transcript"] = "\n\n".join((p.body or "") for p in parts)
        else:
            out["transcript"] = transcript
    if parts is not None:
        out["parts"] = [(_note_part(p) if part_bodies else _note_part_outline(p)) for p in parts]
    return out


def _turn(t: NoteTurn) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "role": t.role.value,
        "content": t.content,
        "ord": t.ord,
    }


@mcp.tool()
async def create_note(
    token: str,
    org_id: str,
    kind: str,
    text: str | None = None,
    title: str | None = None,
    project_id: str | None = None,
    index_scope: str | None = None,
) -> dict[str, Any]:
    """Capture a note (voice|text|conversation). Unmetered.
    ``project_id`` is a PROJECT tag id (anything else is rejected): the
    note is filed under it and takes that project's client. Omit it for
    a personal note -- no project, default client, a first-class
    retrieval perimeter (docs/adr/0021), not a defect; share it later
    with ``move_note_to_project``. ``index_scope`` is ``org`` (default)
    or ``none``, which keeps every part of the note out of the automatic
    search index -- not out of ``get_note``, and not out of the ``q=``
    filter of ``list_notes``."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind(kind),
            project_id=uuid.UUID(project_id) if project_id else None,
            title=title,
            text=text,
            index_scope=IndexScope(index_scope) if index_scope is not None else IndexScope.org,
            channel="mcp",
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        body = await notes_svc.get_body(s, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid, transcript=body)


@mcp.tool()
async def list_notes(
    token: str,
    org_id: str,
    project_id: str | None = None,
    tag_id: str | None = None,
    task_id: str | None = None,
    link_kinds: list[str] | None = None,
    maturity: str | None = None,
    maturities: list[str] | None = None,
    q: str | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
    include_transcript: bool = False,
    fields: list[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    updated_since: str | None = None,
    order_by: str | None = None,
    order_desc: bool = False,
) -> dict[str, Any]:
    """List notes (newest first); for the @note picker. Returns the paginated
    envelope ``{items, next_cursor, truncated}`` (pass ``next_cursor`` back as
    ``cursor`` for the next disjoint page; keyset on the default order, so no
    dupes/gaps). Each row carries
    maturity / review_state / summary / is_archived / created_at /
    updated_at, so a note's lifecycle is readable without a second
    ``get_note``. ``task_id`` returns the notes linked to that task
    (optionally narrowed by ``link_kinds``) ENRICHED in one call, replacing
    list_note_task_links + per-note get_note. ``maturity`` / ``maturities``
    filter by lifecycle stage (e.g. list the 'dormant' notes). Optional
    project/tag focus and archive/trash views. ``q`` is a case-insensitive
    free-text filter applied server-side over the WHOLE corpus (note title,
    part bodies, part titles and tag names, ANDed) BEFORE ``limit``, so it
    is a reliable lexical find rather than a re-rank of the newest page; for
    semantic or cross-kind retrieval use ``search``. Date filters (ISO; a
    bare date is your-timezone day): ``created_after`` / ``created_before`` /
    ``updated_since``. ``order_by`` one of created_at|updated_at|title
    (+ ``order_desc``); default newest-first. ``include_transcript``
    opt-in (default False) keeps picker payloads small; ``fields``
    keeps only the named columns (``id`` always kept); ``limit`` caps
    rows at the DB level (default 50; raise it to page further)."""
    want_maturities = [m for m in ([maturity] if maturity else []) + (maturities or [])]
    after: tuple[dt.datetime, uuid.UUID] | None = None
    if cursor:
        cc, ci = _decode_cursor(cursor)
        after = (dt.datetime.fromisoformat(cc), uuid.UUID(ci))
        order_by, order_desc = None, False
    async with _tenant(token, org_id) as (s, org, user):
        note_ids: list[uuid.UUID] | None = None
        if task_id is not None:
            note_ids = await note_links_svc.notes_for_task(
                s,
                org_id=org,
                task_id=uuid.UUID(task_id),
                kinds=tuple(link_kinds) if link_kinds else None,
                # The trash view of a task's notes stays reachable: the id
                # source applies the effective perimeter by default, so the
                # opt-in has to travel with the call (task 854f1c28).
                include_deleted=include_deleted,
            )
            if not note_ids:
                return {"items": [], "next_cursor": None, "truncated": False}
        tz = (
            await _caller_tz(s, user)
            if any((created_after, created_before, updated_since))
            else dt.UTC
        )
        rows = await notes_svc.list_notes(
            s,
            org_id=org,
            project_id=uuid.UUID(project_id) if project_id else None,
            tag_id=uuid.UUID(tag_id) if tag_id else None,
            note_ids=note_ids,
            maturities=want_maturities or None,
            q=q,
            include_archived=include_archived,
            include_deleted=include_deleted,
            limit=(limit + 1) if limit > 0 else limit,
            after=after,
            created_from=_to_instant(created_after, tz) if created_after else None,
            created_to=_to_instant(created_before, tz) if created_before else None,
            updated_since=_to_instant(updated_since, tz) if updated_since else None,
            order_by=order_by,
            order_desc=order_desc,
        )
        items, next_cursor, truncated = _page_envelope(
            rows, limit, key=lambda n: [n.created_at, n.id]
        )
        if order_by is not None:
            next_cursor = None
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id for n in items])
        pid_map = await note_links_svc.primary_task_ids_for_notes(
            s, org_id=org, note_ids=[n.id for n in items]
        )
        # Phase 6 final: bodies come from note_part(ord=0)+; batched
        # so the picker stays one round-trip even when
        # include_transcript=True.
        bodies = (
            await notes_svc._bodies_by_note(s, note_ids=[n.id for n in items])
            if include_transcript
            else {}
        )
        return {
            "items": [
                _project_fields(
                    _note(
                        n,
                        tagmap.get(n.id, []),
                        primary_task_id=pid_map.get(n.id),
                        include_transcript=include_transcript,
                        transcript=bodies.get(n.id),
                    ),
                    fields,
                )
                for n in items
            ],
            "next_cursor": next_cursor,
            "truncated": truncated,
        }


@mcp.tool()
async def get_note(
    token: str, org_id: str, note_id: str, include_part_bodies: bool = True
) -> dict[str, Any]:
    """Read one note. Includes the ordered ``parts[]`` (markdown blocks)
    so an LLM gets the structured body in one round-trip; when the note
    has zero parts the field is an empty list. The ``transcript`` field
    is a convenience aggregate derived by joining the part bodies (it is
    not stored separately, so it always mirrors ``parts[]``). Set
    ``include_part_bodies=False`` to read a LARGE note as an outline
    (per-part id/ord/title/byte-length/head, no bodies and no derived
    transcript), then fetch only the parts you need with
    ``get_note_part`` -- this avoids dumping a multi-hundred-KB note into
    context."""
    async with _tenant(token, org_id) as (s, org, _user):
        note = await notes_svc.get_note(s, org_id=org, note_id=uuid.UUID(note_id))
        tagmap = await notes_svc.tags_by_note(s, note_ids=[note.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=note.id)
        parts = await note_parts_svc.list_parts(s, org_id=org, note_id=note.id)
        return _note(
            note,
            tagmap.get(note.id, []),
            primary_task_id=pid,
            parts=parts,
            part_bodies=include_part_bodies,
        )


@mcp.tool()
async def add_note_part(
    token: str,
    org_id: str,
    note_id: str,
    body: str,
    lang: str | None = None,
    ord: int | None = None,
) -> dict[str, Any]:
    """Append a markdown block to a note (task 7070a456 Phase 3).
    Pass ``ord`` to insert at a specific position; every part with
    ord >= value is shifted forward. Omit ``ord`` to land at the end.
    Returns the new part."""
    from mycelium_core.services import note_parts as parts_svc_local

    async with _tenant(token, org_id) as (s, org, user):
        part = await parts_svc_local.create_part(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            body=body,
            lang=lang,
            ord=ord,
        )
        return _note_part(part)


@mcp.tool()
async def update_note_part(
    token: str,
    org_id: str,
    part_id: str,
    expected_version: int,
    body: str | None = None,
    lang: str | None = None,
) -> dict[str, Any]:
    """Edit a part's body / lang. ``expected_version`` enforces
    optimistic concurrency (same contract as update_note). To clear
    the language tag pass ``lang=null`` (the omit-vs-clear semantic
    is preserved through the kwargs)."""
    from mycelium_core.services import note_parts as parts_svc_local

    async with _tenant(token, org_id) as (s, org, user):
        kwargs: dict[str, Any] = {}
        if lang is not None:
            kwargs["lang"] = lang
        version = await parts_svc_local.update_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=uuid.UUID(part_id),
            expected_version=expected_version,
            body=body,
            **kwargs,
        )
        return {"part_id": part_id, "version": version}


@mcp.tool()
async def reorder_note_parts(
    token: str,
    org_id: str,
    note_id: str,
    part_ids: list[str],
) -> list[dict[str, Any]]:
    """Rewrite the entire ordering of a note's parts. ``part_ids``
    must be the FULL set of the note's parts in the desired order; a
    missing or extra id raises a domain error. Returns the reordered
    parts so the caller can verify the new ord values."""
    from mycelium_core.services import note_parts as parts_svc_local

    async with _tenant(token, org_id) as (s, org, user):
        rows = await parts_svc_local.reorder_parts(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            part_ids=[uuid.UUID(p) for p in part_ids],
        )
        return [_note_part(p) for p in rows]


@mcp.tool()
async def trash_note_part(
    token: str,
    org_id: str,
    part_id: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """DELETE one part of a note, restorably: the block leaves the note
    and ``restore_note_part`` puts it back with the same id, body and
    position. This is the delete verb to reach for -- ``delete_note``
    removes the WHOLE note, and ``delete_note_part`` is the irreversible
    purge behind a danger scope.

    Remaining parts keep their ords (no compaction), so deep links by
    ord survive and a restore can aim at the slot this part came from.
    ``expected_version`` is an optional concurrency guard: pass the
    part's ``version`` to refuse trashing a block someone edited since
    you read it (``stale_version``, carrying the current version).

    The part stops being retrievable by search while trashed; restoring
    re-indexes it."""
    from mycelium_core.services import note_parts as parts_svc_local

    async with _tenant(token, org_id) as (s, org, user):
        await parts_svc_local.trash_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=uuid.UUID(part_id),
            expected_version=expected_version,
        )
        return {"part_id": part_id, "trashed": True}


@mcp.tool()
async def restore_note_part(token: str, org_id: str, part_id: str) -> dict[str, Any]:
    """Put a trashed part back into its note -- the inverse of
    ``trash_note_part``. It returns with its ORIGINAL id, body, title,
    lang and version, at the ord it held (the parts at or after that
    slot shift forward by one if it was taken meanwhile).

    Fails with ``note.part.not_trashed`` when nothing matches: never
    trashed, already restored, or purged. Find restorable parts with
    ``list_trashed_note_parts``."""
    from mycelium_core.services import note_parts as parts_svc_local

    async with _tenant(token, org_id) as (s, org, user):
        part = await parts_svc_local.restore_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=uuid.UUID(part_id),
        )
        return _note_part(part)


@mcp.tool()
async def list_trashed_note_parts(
    token: str,
    org_id: str,
    note_id: str,
) -> list[dict[str, Any]]:
    """Parts of a note that were trashed and can still be restored, most
    recently trashed first. The discovery half of ``trash_note_part`` /
    ``restore_note_part``: whoever wants a block back is rarely whoever
    removed it. Returns id / ord / title / lang / body / trashed_at."""
    from mycelium_core.services import note_parts as parts_svc_local

    async with _tenant(token, org_id) as (s, org, _user):
        rows = await parts_svc_local.list_trashed(s, org_id=org, note_id=uuid.UUID(note_id))
        return [
            {
                "id": str(r.id),
                "note_id": str(r.note_id),
                "ord": r.ord,
                "title": r.title,
                "body": r.body or "",
                "lang": r.lang,
                "trashed_at": r.trashed_at.isoformat() if r.trashed_at else None,
            }
            for r in rows
        ]


@mcp.tool()
async def delete_note_part(token: str, org_id: str, part_id: str) -> dict[str, Any]:
    """PURGE a part: irreversible, nothing to restore from afterwards.
    Use ``trash_note_part`` for the ordinary, restorable delete -- this
    tool is the destroy-for-good one, which is why it costs the
    ``delete:notes`` danger scope.

    Accepts a part in either state: a live part is destroyed outright,
    an already-trashed one has its trash entry destroyed. Remaining
    parts keep their ord values (no compaction) so deep links by ord
    survive; reorder is explicit."""
    from mycelium_core.services import note_parts as parts_svc_local

    async with _tenant(token, org_id) as (s, org, user):
        await parts_svc_local.delete_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=uuid.UUID(part_id),
        )
        return {"part_id": part_id, "deleted": True}


@mcp.tool()
async def list_note_parts(
    token: str,
    org_id: str,
    note_id: str,
    include_body: bool = False,
) -> list[dict[str, Any]]:
    """Outline (table of contents) of a note's markdown parts in ``ord``
    order, to navigate a LARGE note without pulling every body into
    context. ``include_body=False`` (default) returns id / ord / title /
    lang / byte-length / first-line head per part; set True for the full
    bodies. Pair with ``get_note_part`` for random access to one part."""
    async with _tenant(token, org_id) as (s, org, _user):
        parts = await note_parts_svc.list_parts(s, org_id=org, note_id=uuid.UUID(note_id))
        if include_body:
            return [_note_part(p) for p in parts]
        return [_note_part_outline(p) for p in parts]


@mcp.tool()
async def get_note_part(token: str, org_id: str, part_id: str) -> dict[str, Any]:
    """Read a single note part by id: random access into a long note's
    body without fetching its other parts. Returns the full part incl.
    body and version (find the part_id via ``list_note_parts``)."""
    async with _tenant(token, org_id) as (s, org, _user):
        part = await note_parts_svc.get_part(s, org_id=org, part_id=uuid.UUID(part_id))
        return _note_part(part)


@mcp.tool()
async def replace_in_note_part(
    token: str,
    org_id: str,
    part_id: str,
    find: str,
    replace: str,
    expected_version: int,
    count: int = 0,
) -> dict[str, Any]:
    """Anchored edit inside ONE note part: replace occurrences of the
    literal ``find`` with ``replace`` without resending the whole body --
    for surgically changing a paragraph of a large note. ``count=0``
    (default) replaces every occurrence; a positive ``count`` replaces
    only the first N. ``expected_version`` guards optimistic concurrency
    (same contract as ``update_note_part``: stale_version on mismatch).
    Returns ``{part_id, version, replacements}``; a no-op (``find``
    absent or empty) returns replacements=0 and does not bump the
    version."""
    async with _tenant(token, org_id) as (s, org, user):
        version, replacements = await note_parts_svc.replace_in_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=uuid.UUID(part_id),
            find=find,
            replace=replace,
            expected_version=expected_version,
            count=count,
        )
        return {"part_id": part_id, "version": version, "replacements": replacements}


@mcp.tool()
async def append_note_part(
    token: str,
    org_id: str,
    chunk: str,
    note_id: str | None = None,
    part_id: str | None = None,
    expected_version: int | None = None,
    chunk_index: int = 0,
    is_last: bool = True,
    operation_id: str | None = None,
    title: str | None = None,
    lang: str | None = None,
) -> dict[str, Any]:
    """Stream a LARGE markdown body into a note part in chunks, past the
    MCP ~100k-char per-call payload postal_code (task 27f4d6c9). Split the source
    client-side into ordered chunks of ~32k chars and call this in
    sequence:

    - FIRST chunk: pass ``note_id`` and omit ``part_id`` (optionally
      ``title`` / ``lang``) to CREATE a new part from the chunk; the
      result carries the new ``part_id`` and ``version`` for the rest.
    - EACH next chunk: pass ``part_id`` and ``expected_version`` (the
      version returned by the previous call -- the cursor). Chunks
      concatenate RAW (no separator) for byte-exact reassembly.

    Idempotent on replay (same chunk at the same cursor is a no-op); a
    different-version writer racing the same part gets stale_version.
    Omit ``expected_version`` to append onto the current version (one
    extra read; NOT retry-safe -- pass the cursor for idempotency). Set
    ``is_last=True`` on the final chunk (default) so the recovery-history
    revision is sealed once for the whole upload. Returns
    ``{part_id, version, appended_chars}``."""
    async with _tenant(token, org_id) as (s, org, user):
        if part_id is None:
            if note_id is None:
                raise DomainError(MessageCode.NOTE_PART_ANCHOR_REQUIRED)
            part = await note_parts_svc.create_part(
                s,
                org_id=org,
                actor_id=user,
                note_id=uuid.UUID(note_id),
                body=chunk,
                title=title,
                lang=lang,
            )
            return {
                "part_id": str(part.id),
                "version": part.version,
                "appended_chars": len(chunk),
            }
        eff_version = expected_version
        if eff_version is None:
            existing = await note_parts_svc.get_part(s, org_id=org, part_id=uuid.UUID(part_id))
            eff_version = existing.version
        version, appended = await note_parts_svc.append_to_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=uuid.UUID(part_id),
            chunk=chunk,
            expected_version=eff_version,
            is_last=is_last,
            operation_id=operation_id,
            channel="mcp",
        )
        return {"part_id": part_id, "version": version, "appended_chars": appended}


@mcp.tool()
async def prepend_note_part(
    token: str,
    org_id: str,
    part_id: str,
    text: str,
    expected_version: int | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Prepend markdown ``text`` to the FRONT of a note part without
    resending the body (task 5662a07f). Single-shot -- the natural shape
    for a header / intro on top; for very large front-matter, append a
    fresh part in chunks and reorder instead. Omit ``expected_version``
    to prepend onto the current version (one extra read; NOT retry-safe).
    A different-version writer racing the same part gets stale_version.
    Returns ``{part_id, version, prepended_chars}``."""
    async with _tenant(token, org_id) as (s, org, user):
        eff_version = expected_version
        if eff_version is None:
            existing = await note_parts_svc.get_part(s, org_id=org, part_id=uuid.UUID(part_id))
            eff_version = existing.version
        version, prepended = await note_parts_svc.prepend_to_part(
            s,
            org_id=org,
            actor_id=user,
            part_id=uuid.UUID(part_id),
            text=text,
            expected_version=eff_version,
            operation_id=operation_id,
            channel="mcp",
        )
        return {"part_id": part_id, "version": version, "prepended_chars": prepended}


@mcp.tool()
async def merge_notes(
    token: str,
    org_id: str,
    source_note_id: str,
    target_note_id: str,
    strategy: str = "append",
) -> dict[str, Any]:
    """Fold the source note's parts into the target (task 7070a456
    Phase 3 / 71c9d670 Phase 2b). Soft-deletes the source, stamps
    every moved part with ``merged_from_note_id``, records target
    supersedes source via NoteNoteLink. Idempotent on a source that
    has already been merged or soft-deleted."""
    from mycelium_core.services import note_parts as parts_svc_local

    async with _tenant(token, org_id) as (s, org, user):
        target = await parts_svc_local.merge_notes(
            s,
            org_id=org,
            actor_id=user,
            source_note_id=uuid.UUID(source_note_id),
            target_note_id=uuid.UUID(target_note_id),
            strategy=strategy,
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[target.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=target.id)
        parts = await parts_svc_local.list_parts(s, org_id=org, note_id=target.id)
        return _note(target, tagmap.get(target.id, []), primary_task_id=pid, parts=parts)


@mcp.tool()
async def get_or_create_task_note(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Open a task's "work note" (creating it on first call). Idempotent:
    repeated calls return the same note. Time spent on the note is billed
    to the task via the task-scoped timer."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.get_or_create_work_note(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        body = await notes_svc.get_body(s, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid, transcript=body)


@mcp.tool()
async def create_task_note(
    token: str,
    org_id: str,
    task_id: str,
    title: str | None = None,
    text: str | None = None,
    index_scope: str | None = None,
) -> dict[str, Any]:
    """TASK-side of the bidirectional Proposal A link: create a *fresh*
    work note pre-linked to the task (NOT idempotent, unlike
    get_or_create_task_note). Title defaults to the task title. Time
    logged in the note rolls up to the task. ``index_scope`` is ``org``
    (default) or ``none``, as on ``create_note``."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.create_note_for_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            title=title,
            text=text,
            index_scope=IndexScope(index_scope) if index_scope is not None else IndexScope.org,
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        body = await notes_svc.get_body(s, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid, transcript=body)


@mcp.tool()
async def append_to_note(
    token: str,
    org_id: str,
    note_id: str,
    text: str,
    target: str = "summary",
    separator: str = "\n\n",
    expected_version: int | None = None,
    dedupe_if_tail_matches: bool = False,
) -> dict[str, Any]:
    """Append ``text`` to ``note.summary`` (default) or ``note.transcript``
    without first reading the note body. Context-blind primitive: a
    long note can grow by a paragraph without round-tripping the
    existing content through the LLM context (task 4ac39ecf).

    ``expected_version`` defaults to None (append onto whatever state
    the row currently has -- natural for log-style writers); pass a
    specific version to assert a coherent view (returns stale_version
    on mismatch). ``dedupe_if_tail_matches=True`` makes the call a
    no-op when the body already ends with ``text`` (safe to retry).

    Returns ``{note_id, version, appended_chars}`` (appended_chars=0
    on dedupe). Refuses with ``body.limit_exceeded`` when the resulting
    body would exceed ``MYCELIUM_NOTE_BODY_MAX_BYTES`` (default 1 MiB).
    """
    async with _tenant(token, org_id) as (s, org, user):
        new_version, appended = await notes_svc.append_to_note_field(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            target=target,
            text=text,
            separator=separator,
            expected_version=expected_version,
            dedupe_if_tail_matches=dedupe_if_tail_matches,
            channel="mcp",
        )
        return {
            "note_id": note_id,
            "version": new_version,
            "appended_chars": appended,
        }


@mcp.tool()
async def update_note(
    token: str,
    org_id: str,
    note_id: str,
    expected_version: int,
    title: str | None = None,
    text: str | None = None,
    task_id: str | None = None,
    clear_task_id: bool = False,
    index_scope: str | None = None,
) -> dict[str, Any]:
    """Edit a note's title/body. A blank title is re-derived from the
    first line of the body, and a call that states neither leaves the
    title alone. ``index_scope`` is ``org`` | ``none``; ``none`` drops
    the blobs of every part and keeps the indexer off them, ``org``
    re-indexes them. Neither direction changes who can read the note.
    Bidirectional Proposal A link: pass
    ``task_id`` to link the note to a task (validated in-org, else
    TASK_NOT_FOUND), or ``clear_task_id=True`` to unlink; omitting both
    leaves the existing link untouched.

    ``text`` is the FLAT body: the same ``\n\n`` join of every part that
    ``get_note`` hands you. That join cannot be inverted (a blank line
    inside a part reads exactly like a part boundary), so on a note with
    SEVERAL parts this tool accepts only the body it can express: sending
    it back unchanged is a no-op, and sending a changed one is refused
    (``note.body.multipart``). Edit the part you mean with
    ``update_note_part``, or append with ``append_note_part``. Notes with
    zero or one part are unaffected. Bodies are stored byte for byte;
    nothing here reformats markdown."""
    async with _tenant(token, org_id) as (s, org, user):
        extra: dict[str, Any] = {}
        if clear_task_id:
            extra["task_id"] = None
        elif task_id is not None:
            extra["task_id"] = uuid.UUID(task_id)
        if index_scope is not None:
            extra["index_scope"] = IndexScope(index_scope)
        version = await notes_svc.update_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            expected_version=expected_version,
            title=title,
            text=text,
            channel="mcp",
            **extra,
        )
        return {"note_id": note_id, "version": version}


@mcp.tool()
async def archive_note(
    token: str,
    org_id: str,
    note_id: str,
    expected_version: int,
    archived: bool = True,
) -> dict[str, Any]:
    """Archive (or unarchive with ``archived=False``) a note."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await notes_svc.archive_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            expected_version=expected_version,
            archived=archived,
        )
        return {"note_id": note_id, "version": version}


@mcp.tool()
async def protect_note(
    token: str,
    org_id: str,
    note_id: str,
    expected_version: int,
    protected: bool = True,
) -> dict[str, Any]:
    """Mark a note as finished prose the distiller must never compact
    (Fase P, task 561c6aca) -- excluded from is_inert and every distillation
    candidate/surface. ``protected=False`` releases it. The user has the
    last word."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await notes_svc.protect_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            expected_version=expected_version,
            protected=protected,
        )
        return {"note_id": note_id, "version": version}


@mcp.tool()
async def delete_note(
    token: str, org_id: str, note_id: str, expected_version: int
) -> dict[str, Any]:
    """Soft-delete a note (recoverable via restore_note)."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await notes_svc.soft_delete_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            expected_version=expected_version,
        )
        return {"note_id": note_id, "version": version}


@mcp.tool()
async def restore_note(
    token: str, org_id: str, note_id: str, expected_version: int
) -> dict[str, Any]:
    """Restore a soft-deleted note."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await notes_svc.restore_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            expected_version=expected_version,
        )
        return {"note_id": note_id, "version": version}


@mcp.tool()
async def gdpr_erase_note(token: str, org_id: str, note_id: str) -> dict[str, Any]:
    """GDPR hard-erasure of a note: cascades to its memory blobs (by note
    provenance) and conversation turns, then deletes the note. Returns
    the count of memory blobs removed and the S3 ``audio_ref`` (if any)
    for the caller/worker to delete out-of-band. Distinct from
    ``delete_note`` (recoverable soft-delete) and from ``memory_erase``
    (memory-only). Member role."""
    async with _tenant(token, org_id) as (s, org, user):
        res = await notes_svc.gdpr_erase_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
        )
        return {
            "note_id": note_id,
            "memory_blobs_deleted": res.memory_blobs_deleted,
            "audio_ref": res.audio_ref,
        }


@mcp.tool()
async def distill_note(
    token: str,
    org_id: str,
    note_id: str,
    distilled_text: str | None = None,
    origin_model_id: str | None = None,
) -> dict[str, Any]:
    """Fungal decomposition (ADR-0034): distil a note's body into a
    reusable atom note and flag both source and distillation as humus so
    the LLM walk surfaces them as fertiliser. Idempotent: an
    already-distilled note returns its existing distillation
    (``created`` false). Member role.

    By default Mycelium runs the distillation on the org's own metered LLM.
    To write the atom with YOUR OWN strong model instead, pass
    ``distilled_text`` (the distillation you produced from the note's body)
    and optionally ``origin_model_id`` (your model's id, recorded for
    provenance). On that path the internal LLM call and the verify pass are
    skipped and Mycelium meters no model call (only the flat gateway fee
    applies) -- the grounding/fidelity is then YOURS to guarantee."""
    async with _tenant(token, org_id) as (s, org, user):
        res = await decomposition_svc.distill_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            distilled_text=distilled_text,
            origin_model_id=origin_model_id,
        )
        return {
            "source_note_id": note_id,
            "distilled_note_id": str(res.distilled_note_id),
            "model_id": res.model_id,
            "created": res.created,
        }


@mcp.tool()
async def extract_cluster_pattern(
    token: str, org_id: str, source_note_ids: list[str]
) -> dict[str, Any]:
    """Phase-2 decomposition (ADR-0039): synthesise a ``pattern`` humus note
    over a set of ARCHIVED source notes (a Leiden cluster, a cross-cluster
    pick, a project window -- you choose the grouping). Reads the sources,
    asks the per-org metered LLM for the through-lines, writes a new note
    linked back to each source. A proposal, never a mutation of live notes;
    needs >=2 archived sources. Idempotent on the source set (``created``
    false on a re-run)."""
    async with _tenant(token, org_id) as (s, org, user):
        res = await decomposition_svc.extract_cluster_pattern(
            s,
            org_id=org,
            actor_id=user,
            source_note_ids=[uuid.UUID(n) for n in source_note_ids],
        )
        return {
            "pattern_note_id": str(res.note_id),
            "model_id": res.model_id,
            "created": res.created,
        }


@mcp.tool()
async def synthesize_season(token: str, org_id: str, year: int, quarter: int) -> dict[str, Any]:
    """Phase-2 decomposition (ADR-0039): synthesise a ``season`` humus note
    for one quarter -- "what I cultivated this season" -- over the notes
    archived in it. ``quarter`` is 1-4. A proposal, never a mutation of live
    notes; metered LLM call inside. Idempotent per (year, quarter)."""
    async with _tenant(token, org_id) as (s, org, user):
        res = await decomposition_svc.synthesize_season(
            s,
            org_id=org,
            actor_id=user,
            year=year,
            quarter=quarter,
        )
        return {
            "season_note_id": str(res.note_id),
            "model_id": res.model_id,
            "created": res.created,
        }


@mcp.tool()
async def list_distillation_candidates(
    token: str, org_id: str, kind: str = "all", limit: int = 50
) -> dict[str, Any]:
    """Are there distillations to do? (task 4995a32f). Distillation is graph
    MAINTENANCE, so this surfaces two families of candidate, read-only and
    with no LLM call:

    - NODE candidates (compact inert material into a denser atom): ``distill``
      (one inert note), ``pattern`` (a Leiden cluster of >=2 archived notes),
      ``season`` (a quarter's archived notes). Act with ``distill_note`` /
      ``extract_cluster_pattern`` / ``synthesize_season`` -- pass your OWN
      strong model's output via ``distilled_text`` (no org credits spent), or
      omit it to use the org's hosted provider (metered).
    - EDGE candidates (curate the link graph): ``link_add`` (a strong
      tag/co-activity pair with no manual link -> create a ``related`` link),
      ``link_prune`` (a ``related`` link whose basis has decayed),
      ``link_direct`` (search traffic keeps traversing the pair in ONE
      direction -> propose promoting to a directed ``hypha_of``, entry
      note as parent; conservative heuristic, never auto-applied).

    ``kind`` filters to one family (all|distill|pattern|season|link_add|
    link_prune|link_direct). After acting, autonomously-produced atoms go through
    ``garden_review_pending`` -> ``garden_review_approve``. Member role;
    RLS-scoped."""
    async with _tenant(token, org_id) as (s, org, _user):
        return await candidates_svc.list_distillation_candidates(
            s, org_id=org, kind=kind, limit=limit
        )


@mcp.tool()
async def list_events(
    token: str,
    org_id: str,
    since: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Read the workspace coordination event bus (ADR-0036): the
    read / propose / commit / reject events humans and other agents emit as
    they act on the graph, newest first, RLS-scoped to your workspace. This is
    how an MCP agent OBSERVES what its collaborators are doing; there is no
    streaming subscription, you poll this.

    ``since`` is an ISO-8601 timestamp cursor -- events at or after it are
    returned (omit for the most recent ``limit``). Delivery is at-least-once:
    the boundary event is re-returned, so dedupe by ``id``. To follow the
    stream, remember the largest ``ts`` you saw and pass it back as ``since``
    next time; if a poll returns exactly ``limit`` rows there may be more, so
    poll again immediately. ``limit`` is 1..500 (default 100). A pure read."""
    n = max(1, min(int(limit), 500))
    since_dt: dt.datetime | None = None
    if since:
        since_dt = dt.datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=dt.UTC)
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await event_bus_svc.recent_events(s, org_id=org, limit=n, since=since_dt)
        return {
            "events": [
                {
                    "id": str(e.id),
                    "actor_id": str(e.actor_id) if e.actor_id else None,
                    "actor_kind": e.actor_kind,
                    "kind": e.kind,
                    "node_kind": e.node_kind,
                    "node_id": str(e.node_id) if e.node_id else None,
                    "parent_event_id": (str(e.parent_event_id) if e.parent_event_id else None),
                    "payload": e.payload,
                    "ts": e.ts.isoformat(),
                    "applied_at": e.applied_at.isoformat() if e.applied_at else None,
                    "applied_state": e.applied_state,
                }
                for e in rows
            ]
        }


@mcp.tool()
async def garden_review_pending(token: str, org_id: str, limit: int = 50) -> dict[str, Any]:
    """Review inbox (ADR-0043): the workspace's AUTONOMOUSLY-generated humus
    notes awaiting human approval (``review_state='proposed'``), newest first.
    Each row carries ``origin_model_id`` -- the model that produced the summary
    (a local 3B != GPT != Scaleway) -- so you can see WHICH model wrote it
    before deciding. A pure read. Member role; RLS-scoped."""
    async with _tenant(token, org_id) as (s, org, _user):
        pending = await garden_review_svc.list_pending(s, org_id=org, limit=limit)
        return {
            "pending": [
                {
                    "note_id": str(p.note_id),
                    "title": p.title,
                    "humus_kind": p.humus_kind,
                    "origin_model_id": p.origin_model_id,
                    "preview": p.preview,
                    "created_at": p.created_at.isoformat(),
                    "version": p.version,
                }
                for p in pending
            ]
        }


@mcp.tool()
async def garden_review_rejected(token: str, org_id: str, limit: int = 50) -> dict[str, Any]:
    """Review BIN (ADR-0043): the proposals a human DECLINED, most recently
    declined first. A reject soft-deletes the node and leaves it
    ``proposed``, a pair of states no other listing shows -- ``list_notes``
    hides proposals even in the trash view -- so this is the only way to
    find one again. Undo it with ``restore_note``, echoing ``version``.
    A pure read. Member role; RLS-scoped."""
    async with _tenant(token, org_id) as (s, org, _user):
        rejected = await garden_review_svc.list_rejected(s, org_id=org, limit=limit)
        return {
            "rejected": [
                {
                    "note_id": str(r.note_id),
                    "title": r.title,
                    "humus_kind": r.humus_kind,
                    "origin_model_id": r.origin_model_id,
                    "preview": r.preview,
                    "created_at": r.created_at.isoformat(),
                    "rejected_at": r.rejected_at.isoformat(),
                    "version": r.version,
                }
                for r in rejected
            ]
        }


@mcp.tool()
async def garden_review_approve(
    token: str, org_id: str, note_id: str, expected_version: int | None = None
) -> dict[str, Any]:
    """Approve a proposed humus note (ADR-0043): it becomes effective and
    re-enters the retrieval walk / search / listings. Audited; emits a bus
    ``commit`` event. Idempotent (a re-approve is a no-op). Member role.
    Pass ``expected_version`` (served by ``garden_review_pending``) to fail
    with ``stale_version`` if the note changed after you read it -- the
    TOCTOU guard (task 2e36e732); omit it for the legacy unguarded path."""
    async with _tenant(token, org_id) as (s, org, user):
        note = await garden_review_svc.approve_node(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            expected_version=expected_version,
        )
        return {
            "note_id": str(note.id),
            "review_state": note.review_state,
            "origin_model_id": note.origin_model_id,
            "version": note.version,
        }


@mcp.tool()
async def garden_review_reject(
    token: str,
    org_id: str,
    note_id: str,
    reason: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Reject a proposed humus note (ADR-0043): soft-delete it so a weak
    summary never pollutes the corpus (reversible via the trash/restore path).
    Audited; emits a bus ``reject`` event carrying ``origin_model_id``.
    Idempotent (a re-reject is a no-op). Member role. ``expected_version``
    mirrors ``garden_review_approve`` (TOCTOU guard, task 2e36e732)."""
    async with _tenant(token, org_id) as (s, org, user):
        note = await garden_review_svc.reject_node(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            reason=reason,
            expected_version=expected_version,
        )
        return {
            "note_id": str(note.id),
            "rejected": note.deleted_at is not None,
            "origin_model_id": note.origin_model_id,
            "version": note.version,
        }


@mcp.tool()
async def note_restore_source(
    token: str, org_id: str, note_id: str, reason: str | None = None
) -> dict[str, Any]:
    """Fase P (task 561c6aca), "ripristina originale": given a humus ATOM,
    un-archive its ``hypha_of`` source notes (the originals are preserved,
    never mutated) and retire the atom (an approved atom is demoted then
    rejected; nothing is hard-deleted -- the provenance chain stays
    queryable). Restoring is choosing the layer under the lens. Idempotent.
    Member role."""
    async with _tenant(token, org_id) as (s, org, user):
        res = await garden_review_svc.restore_source(
            s, org_id=org, actor_id=user, atom_note_id=uuid.UUID(note_id), reason=reason
        )
        return {
            "atom_note_id": str(res.atom_note_id),
            "source_ids": [str(x) for x in res.source_ids],
            "restored_source_ids": [str(x) for x in res.restored_source_ids],
            "atom_retired": res.atom_retired,
        }


@mcp.tool()
async def garden_classify(
    token: str, org_id: str, node_id: str, kinds: str | None = None
) -> dict[str, Any]:
    """Proposal engine (ADR-0032): for a note, propose {tags, links,
    maturity, cluster}, each with a confidence + rationale, plus
    ``signals_used`` for transparency. **Read-only** — never mutates; act
    on a suggestion with ``garden_apply``. ``kinds`` is an optional CSV
    subset of tags,links,maturity,cluster (default all; unknown tokens
    dropped). Member-level, RLS-scoped."""
    wanted = None
    if kinds:
        requested = {k.strip() for k in kinds.split(",") if k.strip()}
        wanted = (
            frozenset(requested & garden_classify_svc.ALL_KINDS) or garden_classify_svc.ALL_KINDS
        )
    async with _tenant(token, org_id) as (s, org, _user):
        res = await garden_classify_svc.classify_node(
            s, org_id=org, node_id=uuid.UUID(node_id), kinds=wanted
        )
        return {
            "node_id": str(res.node_id),
            "node_kind": res.node_kind,
            "tags": [
                {"tag_id": str(t.tag_id), "confidence": t.confidence, "rationale": t.rationale}
                for t in res.tags
            ],
            "links": [
                {
                    "target_id": str(lc.target_id),
                    "link_kind": lc.link_kind,
                    "confidence": lc.confidence,
                    "rationale": lc.rationale,
                }
                for lc in res.links
            ],
            "maturity": (
                {
                    "value": res.maturity.value,
                    "confidence": res.maturity.confidence,
                    "rationale": res.maturity.rationale,
                    "auto_apply": res.maturity.auto_apply,
                }
                if res.maturity is not None
                else None
            ),
            "cluster": (
                {
                    "leiden_id": res.cluster.leiden_id,
                    "modularity": res.cluster.modularity,
                    "confidence": res.cluster.confidence,
                }
                if res.cluster is not None
                else None
            ),
            "signals_used": res.signals_used,
            "model_version": res.model_version,
        }


@mcp.tool()
async def garden_apply(
    token: str,
    org_id: str,
    node_id: str,
    suggestion_type: str,
    suggestion_value: dict[str, Any],
    action: str,
    override_value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply or decline a ``garden_classify`` suggestion (ADR-0032 /
    ADR-0037). ``accept``/``override`` mutate via the existing idempotent
    services; ``reject``/``ignore`` only record the decision. Always
    writes a ``classification_feedback`` event. Member role. ``action`` is
    one of accept|reject|override|ignore (``auto`` is worker-only and is
    rejected here so a client cannot forge a system promotion)."""
    if action not in ("accept", "reject", "override", "ignore"):
        raise DomainError(
            MessageCode.GARDEN_ACTION_INVALID,
            action=action,
            valid="accept, ignore, override, reject",
        )
    async with _tenant(token, org_id) as (s, org, user):
        feedback = await garden_classify_svc.apply_suggestion(
            s,
            org_id=org,
            actor_id=user,
            node_id=uuid.UUID(node_id),
            suggestion_type=suggestion_type,
            suggestion_value=suggestion_value,
            action=action,
            override_value=override_value,
        )
        return {
            "feedback_id": str(feedback.id),
            "node_id": node_id,
            "suggestion_type": suggestion_type,
            "action": action,
            "applied": action in ("accept", "override"),
        }


@mcp.tool()
async def add_note_tag(token: str, org_id: str, note_id: str, tag_id: str) -> dict[str, Any]:
    """Attach a free-form tag (generic / memory channel) to a note,
    idempotently. This is the many-to-many door: a note carries any
    number of these facets.

    The note's client and project are NOT facets: they are structural
    and single-valued (exactly one client, at most one project --
    docs/adr/0003, docs/adr/0021), so use ``move_note_to_project`` to
    share the note into a project (it drags the client along),
    ``clear_note_project`` to un-share it and ``set_note_client`` to
    change the client. Passing one of those tags here still performs
    the structural operation, but the named tools say what they do and
    validate the tag's kind."""
    async with _tenant(token, org_id) as (s, org, user):
        await notes_svc.attach_tag(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            tag_id=uuid.UUID(tag_id),
        )
        return {"note_id": note_id, "tag_id": tag_id}


@mcp.tool()
async def remove_note_tag(token: str, org_id: str, note_id: str, tag_id: str) -> dict[str, Any]:
    """Detach a free-form tag from a note. The client cannot be detached
    (tag.structural_required) -- a note always has one; use
    ``set_note_client`` to change it. Dropping the PROJECT is the
    un-share path and has its own tool, ``clear_note_project``."""
    async with _tenant(token, org_id) as (s, org, user):
        await notes_svc.detach_tag(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            tag_id=uuid.UUID(tag_id),
        )
        return {"note_id": note_id, "tag_id": tag_id, "removed": True}


@mcp.tool()
async def move_note_to_project(
    token: str, org_id: str, note_id: str, project_tag_id: str
) -> dict[str, Any]:
    """Move a note into a project (share it). A note has at most one
    project and exactly one client, so the new project REPLACES the
    previous one and the note's client is snapped to that project's
    client in the same transaction -- moving a note across clients is
    this one call, not an error. The note's indexed memory is re-scoped
    to the project perimeter at once, so peers retrieve it immediately
    without any content edit. ``clear_note_project`` is the reverse."""
    async with _tenant(token, org_id) as (s, org, user):
        project = await _tag_of_kind(s, org, project_tag_id, TagKind.project)
        # ``notes.attach_tag`` is the gated door (RBAC + note existence)
        # and routes a project tag to ``tag_assignment.move_to_project``,
        # the only writer of a structural junction row, which also
        # re-scopes the blobs (task 1d152747).
        await notes_svc.attach_tag(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            tag_id=project,
        )
        return {"note_id": note_id, "project_tag_id": project_tag_id}


@mcp.tool()
async def set_note_client(
    token: str, org_id: str, note_id: str, client_tag_id: str
) -> dict[str, Any]:
    """Re-point a note at a client. A note has exactly one client, so
    this REPLACES the current one and keeps the project. A client that
    contradicts the note's project is refused
    (tag.client_project_mismatch): the client is derived from the
    project, so use ``move_note_to_project`` with a project of the
    wanted client to move the note across clients."""
    async with _tenant(token, org_id) as (s, org, user):
        client = await _tag_of_kind(s, org, client_tag_id, TagKind.client)
        await notes_svc.attach_tag(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            tag_id=client,
        )
        return {"note_id": note_id, "client_tag_id": client_tag_id}


@mcp.tool()
async def clear_note_project(token: str, org_id: str, note_id: str) -> dict[str, Any]:
    """Un-share a note: drop its project, KEEP its client. A projectless
    note is a first-class personal perimeter (docs/adr/0021), not a
    defect: its indexed memory goes back to personal scope at once, so
    peers stop retrieving it under the project. No-op on a note that
    has no project. There is no task counterpart -- a task always has a
    project."""
    async with _tenant(token, org_id) as (s, org, user):
        note = uuid.UUID(note_id)
        # The project is single-valued, so it is not a caller argument:
        # the only thing to clear is whatever the note carries now.
        # ``get_note`` first, so a missing note is NOT_FOUND rather than
        # an empty "nothing to clear".
        await notes_svc.get_note(s, org_id=org, note_id=note)
        project = await notes_svc.project_tag_for_note(s, note_id=note)
        if project is None:
            return {"note_id": note_id, "project_tag_id": None, "cleared": False}
        # ``notes.detach_tag`` routes a project tag to
        # ``tag_assignment.clear_project``, which keeps the client and
        # re-scopes the blobs to the personal (NULL project) perimeter.
        await notes_svc.detach_tag(
            s,
            org_id=org,
            actor_id=user,
            note_id=note,
            tag_id=project,
        )
        return {"note_id": note_id, "project_tag_id": str(project), "cleared": True}


@mcp.tool()
async def list_turns(
    token: str, org_id: str, note_id: str, limit: int | None = None, cursor: str | None = None
) -> dict[str, Any]:
    """List the turns of a conversation note, in order. Returns the paginated
    envelope ``{items, next_cursor, truncated}``: pass ``limit`` to page a long
    transcript, then ``next_cursor`` back as ``cursor`` (keyset, no
    dupes/gaps)."""
    after: tuple[int, uuid.UUID] | None = None
    if cursor:
        co, ci = _decode_cursor(cursor)
        after = (int(co), uuid.UUID(ci))
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await notes_svc.list_turns(
            s,
            org_id=org,
            note_id=uuid.UUID(note_id),
            limit=(limit + 1) if limit else None,
            after=after,
        )
        items, next_cursor, truncated = _page_envelope(
            rows, limit or 0, key=lambda t: [t.ord, t.id]
        )
        return {
            "items": [_turn(t) for t in items],
            "next_cursor": next_cursor,
            "truncated": truncated,
        }


@mcp.tool()
async def start_conversation_session(
    token: str,
    org_id: str,
    title: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Start a new conversation session (a conversation Note)."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.conversation,
            project_id=uuid.UUID(project_id) if project_id else None,
            title=title,
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        body = await notes_svc.get_body(s, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid, transcript=body)


@mcp.tool()
async def append_message(
    token: str, org_id: str, note_id: str, content: str, operation_id: str
) -> dict[str, Any]:
    """Append a user message; returns the metered LLM reply turn."""
    async with _tenant(token, org_id) as (s, org, user):
        reply = await notes_svc.append_message(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            content=content,
            operation_id=operation_id,
        )
        return _turn(reply)


@mcp.tool()
async def transcribe_note(
    token: str, org_id: str, note_id: str, operation_id: str
) -> dict[str, Any]:
    """Run STT on a voice note (metered per audio-minute)."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.transcribe(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            operation_id=operation_id,
        )
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        body = await notes_svc.get_body(s, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid, transcript=body)


@mcp.tool()
async def run_command(token: str, org_id: str, text: str) -> dict[str, Any]:
    """Deterministic canonical NL command (offline, unmetered)."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notes_svc.run_command(s, org_id=org, actor_id=user, text=text)
        tagmap = await notes_svc.tags_by_note(s, note_ids=[n.id])
        pid = await note_links_svc.primary_task_id_for_note(s, org_id=org, note_id=n.id)
        body = await notes_svc.get_body(s, note_id=n.id)
        return _note(n, tagmap.get(n.id, []), primary_task_id=pid, transcript=body)


@mcp.tool()
async def synthesize_speech(
    token: str, org_id: str, text: str, operation_id: str
) -> dict[str, Any]:
    """TTS voice-out (metered per character)."""
    async with _tenant(token, org_id) as (s, org, user):
        return await notes_svc.synthesize(
            s,
            org_id=org,
            actor_id=user,
            text=text,
            operation_id=operation_id,
        )


# --- Attachments on notes / tasks (DB-BYTEA) ---
#
# Binary UPLOAD over MCP: the file bytes are base64-encoded inside the
# JSON tool call (the protocol exchanges JSON, not multipart). The
# workspace's effective size cap still applies — the per-workspace
# admin-tunable ``attachment_max_bytes`` (or the config default),
# checked on the *decoded* payload by the service layer, exactly like
# the REST path. Practical for an LLM agent that wants to attach a
# rendered report or a small PDF without bouncing through REST.


@mcp.tool()
async def upload_attachment(
    token: str,
    org_id: str,
    filename: str,
    data_b64: str,
    note_id: str | None = None,
    task_id: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """Attach a file (base64-encoded bytes) to a note OR a task.

    Exactly one of ``note_id`` / ``task_id`` must be set. ``data_b64``
    is base64 of the raw file contents; the server decodes and enforces
    the workspace's effective attachment size cap (the admin-tunable
    per-workspace ``attachment_max_bytes``, default 10 MiB) on the
    decoded size. The detected mime overrides ``mime_type`` if the
    sniffer disagrees with a misleading client hint.

    Returns the attachment metadata (id, filename, mime_type,
    size_bytes) plus ``markdown_ref`` — a paste-ready reference to drop
    into a note body / task description (``![name](...)`` for images,
    ``[name](...)`` otherwise). The url is the bearer-auth
    /attachments/<id>/download route the app resolves through authFetch;
    it is never public. The binary is never echoed back."""
    import base64
    import binascii

    if (note_id is None) == (task_id is None):
        raise ValueError("provide exactly one of note_id / task_id")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"data_b64 is not valid base64: {exc}") from exc
    async with _tenant(token, org_id) as (s, org, user):
        att = await attachments_svc.add_attachment(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id) if note_id else None,
            task_id=uuid.UUID(task_id) if task_id else None,
            filename=filename,
            mime_type=mime_type,
            data=raw,
        )
        # The label is a filename, i.e. user data: one containing `]` used to
        # emit a string that is not a link at all (mycelium_core.markdown_inline).
        markdown_ref = md_link(
            att.filename,
            f"/attachments/{att.id}/download",
            image=att.mime_type.startswith("image/"),
        )
        return {
            "id": str(att.id),
            "note_id": str(att.note_id) if att.note_id else None,
            "task_id": str(att.task_id) if att.task_id else None,
            "filename": att.filename,
            "mime_type": att.mime_type,
            "size_bytes": att.size_bytes,
            "created_at": att.created_at.isoformat(),
            "markdown_ref": markdown_ref,
        }


@mcp.tool()
async def upload_attachment_instructions(
    token: str,
    org_id: str,
    filename: str,
    note_id: str | None = None,
    task_id: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """Recipe for a TOKEN-FREE large-file upload (MRI, DICOM, PDF, ...).

    ``upload_attachment`` carries the bytes as base64 inside the tool
    call, so a large file blows the context budget (a few MB of base64 is
    hundreds of KB of tokens). This tool instead returns a ready-to-run
    ``curl`` that STREAMS the raw file through the backend gateway to
    object storage: the bytes ride the HTTP body (never a tool argument,
    so zero tokens), are never buffered nor written to disk server-side,
    and S3 is never exposed to the client -- the backend is always the
    gateway (the security model for medical data). No bytes pass through
    this tool; only a command template comes back.

    Requires the s3 attachment backend (the endpoint returns
    ATTACHMENT_STREAM_UNSUPPORTED on the default pg backend). Pass exactly
    one of ``note_id`` / ``task_id``. The ``curl`` has a ``$MYCELIUM_TOKEN``
    placeholder (fill with your agent/session token -- it is NOT echoed
    here, it stays secret) and a ``<path-to-file>`` placeholder. On
    success the endpoint returns the attachment JSON (id, size_bytes,
    ...); use ``markdown_ref_template`` -- substituting the real id -- to
    reference it from a note body / task description."""
    from urllib.parse import quote

    from mycelium_core.config import get_settings

    if (note_id is None) == (task_id is None):
        raise ValueError("provide exactly one of note_id / task_id")
    # Confirm the caller can act in the org (both transports); the
    # endpoint re-checks membership + parent visibility under RLS at
    # upload time, so this is only a fail-fast on a bad org/token.
    async with _tenant(token, org_id) as (_s, org, _user):
        pass
    settings = get_settings()
    # The public path is the SPA origin + ``/api`` (the reverse proxy /
    # Vite strip ``/api`` and forward to FastAPI). Never an S3 URL.
    base = settings.frontend_base_url.rstrip("/")
    params = [f"filename={quote(filename)}"]
    if note_id:
        params.append(f"note_id={note_id}")
    if task_id:
        params.append(f"task_id={task_id}")
    url = f"{base}/api/attachments/stream?" + "&".join(params)
    content_type = mime_type or "application/octet-stream"
    curl = (
        f"curl -fsS -X POST '{url}' \\\n"
        f"  -H 'Authorization: Bearer $MYCELIUM_TOKEN' \\\n"
        f"  -H 'X-Workspace-Id: {org}' \\\n"
        f"  -H 'Content-Type: {content_type}' \\\n"
        f"  --data-binary @<path-to-file>"
    )
    bang = "!" if content_type.startswith("image/") else ""
    return {
        "endpoint": url,
        "method": "POST",
        "curl": curl,
        "headers": {
            "Authorization": "Bearer $MYCELIUM_TOKEN",
            "X-Workspace-Id": str(org),
            "Content-Type": content_type,
        },
        "max_bytes": settings.attachment_stream_max_bytes,
        "notes": (
            "Streams the raw body through the backend gateway to object "
            "storage; the file is never buffered server-side and S3 is "
            "never exposed. Fill $MYCELIUM_TOKEN with your token and "
            "<path-to-file> with the local path. Token-free: no bytes go "
            "through MCP."
        ),
        # ``<id>`` is a placeholder the caller substitutes, so the destination
        # is built by hand; only the user-supplied label needs escaping.
        "markdown_ref_template": (f"{bang}[{md_link_label(filename)}](/attachments/<id>/download)"),
    }


def _text_stream_recipe(
    *, url: str, org: uuid.UUID, method: str, max_bytes: int, returns: str
) -> dict[str, Any]:
    """Shared shape for the TOKEN-FREE inline-body recipes: a ready-to-run
    ``curl`` that streams a local UTF-8 markdown file as the raw request
    body (``--data-binary``), so the body never rides a tool argument.
    Mirrors ``upload_attachment_instructions`` but the bytes land in a
    Postgres TEXT column (a note part / annotation body), so NO S3 backend
    is needed. ``$MYCELIUM_TOKEN`` and ``<path-to-file>`` are placeholders the
    caller fills; the token is never echoed back."""
    content_type = "text/markdown; charset=utf-8"
    curl = (
        f"curl -fsS -X {method} '{url}' \\\n"
        f"  -H 'Authorization: Bearer $MYCELIUM_TOKEN' \\\n"
        f"  -H 'X-Workspace-Id: {org}' \\\n"
        f"  -H 'Content-Type: {content_type}' \\\n"
        f"  --data-binary @<path-to-file>"
    )
    return {
        "endpoint": url,
        "method": method,
        "curl": curl,
        "headers": {
            "Authorization": "Bearer $MYCELIUM_TOKEN",
            "X-Workspace-Id": str(org),
            "Content-Type": content_type,
        },
        "max_bytes": max_bytes,
        "notes": (
            "Streams the file as the raw request body straight into the "
            "document's TEXT column: no bytes go through MCP (token-free) "
            "and no S3 backend is needed. Fill $MYCELIUM_TOKEN with your token "
            "and <path-to-file> with the local markdown file. On success "
            f"the endpoint returns {returns}."
        ),
    }


@mcp.tool()
async def add_note_part_instructions(
    token: str,
    org_id: str,
    note_id: str,
    title: str | None = None,
    lang: str | None = None,
    ord: int | None = None,
) -> dict[str, Any]:
    """Recipe for a TOKEN-FREE note-part create: stream a local markdown
    file straight into a NEW part's body instead of sending it as an
    ``add_note_part`` argument (which spends tokens proportional to the
    body size). The body rides the HTTP request body; the metadata
    (``title`` / ``lang`` / ``ord``) go in the URL. No S3 needed -- the
    text lands in the part's TEXT column. Fill $MYCELIUM_TOKEN +
    <path-to-file>; the response carries the new ``id`` and ``version``."""
    from urllib.parse import quote

    from mycelium_core.config import get_settings

    async with _tenant(token, org_id) as (_s, org, _user):
        pass
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    params: list[str] = []
    if title is not None:
        params.append(f"title={quote(title)}")
    if lang is not None:
        params.append(f"lang={quote(lang)}")
    if ord is not None:
        params.append(f"ord={ord}")
    qs = ("?" + "&".join(params)) if params else ""
    url = f"{base}/api/notes/{note_id}/parts/stream{qs}"
    return _text_stream_recipe(
        url=url,
        org=org,
        method="POST",
        max_bytes=settings.note_body_max_bytes,
        returns="the new part JSON (id, ord, version)",
    )


@mcp.tool()
async def set_note_part_body_instructions(
    token: str,
    org_id: str,
    note_id: str,
    part_id: str,
    expected_version: int,
) -> dict[str, Any]:
    """Recipe for a TOKEN-FREE full-body REPLACE of an existing note part:
    stream the whole new markdown from a local file into the part's body
    column without resending it as a tool argument. ``expected_version``
    is the optimistic cursor (a mismatch is stale_version -> 409); an
    empty file clears the part. For incremental growth use
    ``append_note_part`` instead. The response carries the new
    ``version``. If the agent has no PAT to fill ``$MYCELIUM_TOKEN``, use
    ``set_note_part_body_capability`` for a self-contained token."""
    from mycelium_core.config import get_settings

    async with _tenant(token, org_id) as (_s, org, _user):
        pass
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    url = (
        f"{base}/api/notes/{note_id}/parts/{part_id}/body/stream"
        f"?expected_version={expected_version}"
    )
    return _text_stream_recipe(
        url=url,
        org=org,
        method="PUT",
        max_bytes=settings.note_body_max_bytes,
        returns="the part id + new version",
    )


@mcp.tool()
async def set_note_part_body_capability(
    token: str,
    org_id: str,
    note_id: str,
    part_id: str,
    expected_version: int,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Like ``set_note_part_body_instructions`` but needs NO long-lived
    PAT: mint a single-use, short-TTL capability token scoped to writing
    THIS part's body, and return a ready ``curl`` with that ephemeral
    token already in the Authorization header (not a ``$MYCELIUM_TOKEN``
    placeholder, and no ``X-Workspace-Id`` -- the org is baked into the
    token). Use this for an agent that has no local Mycelium CLI / PAT.

    The token authorizes exactly one write to this part, is consumed on
    first success, and expires in ``ttl_seconds`` (default 300). A
    retried 409 (stale ``expected_version``) does not burn it. If the
    agent DOES have the Mycelium CLI, prefer ``mycelium notes parts set-body``
    instead: there the credential never leaves the machine."""
    from mycelium_core.config import get_settings
    from mycelium_core.services import capability_tokens as cap_svc

    async with _tenant(token, org_id) as (session, org, user):
        grant = await cap_svc.mint(
            session,
            org_id=org,
            actor_id=user,
            action=cap_svc.ACTION_NOTE_PART_BODY_WRITE,
            resource_kind=cap_svc.RESOURCE_NOTE_PART,
            resource_id=uuid.UUID(part_id),
            ttl_seconds=ttl_seconds,
        )
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    url = (
        f"{base}/api/notes/{note_id}/parts/{part_id}/body/stream"
        f"?expected_version={expected_version}"
    )
    content_type = "text/markdown; charset=utf-8"
    curl = (
        f"curl -fsS -X PUT '{url}' \\\n"
        f"  -H 'Authorization: Bearer {grant.raw}' \\\n"
        f"  -H 'Content-Type: {content_type}' \\\n"
        f"  --data-binary @<path-to-file>"
    )
    return {
        "endpoint": url,
        "method": "PUT",
        "curl": curl,
        "headers": {
            "Authorization": f"Bearer {grant.raw}",
            "Content-Type": content_type,
        },
        "max_bytes": settings.note_body_max_bytes,
        "expires_at": grant.expires_at.isoformat(),
        "notes": (
            "The Authorization header already carries a single-use "
            "capability token scoped to writing ONLY this part's body; it "
            "is consumed on first success and expires at the time above. No "
            "PAT and no X-Workspace-Id needed. Fill <path-to-file> with the "
            "local UTF-8 markdown file. On success the endpoint returns the "
            "part id + new version."
        ),
    }


@mcp.tool()
async def download_attachment_capability(
    token: str,
    org_id: str,
    parent_kind: str,
    parent_id: str,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Mint ONE short-TTL capability token that downloads EVERY attachment
    of a note or task with NO long-lived PAT and NO ``X-Workspace-Id`` (the
    org is baked into the token), and return a ready ``curl -o`` per file.
    Use this for an agent that has no local Mycelium CLI / PAT and needs to pull
    a task's or note's attachments to disk.

    ``parent_kind`` is ``"task"`` or ``"note"``; ``parent_id`` its id. The
    token is scoped to ``attachment:read`` on that parent: it authorises
    only attachments hanging off it, and is multi-use until it expires in
    ``ttl_seconds`` (default 300) -- a download is idempotent, so it is NOT
    consumed on first use. If the agent DOES have the Mycelium CLI, prefer
    ``mycelium attachments download`` instead: there the credential never leaves
    the machine."""
    import shlex

    from mycelium_core.config import get_settings
    from mycelium_core.services import attachments as att_svc
    from mycelium_core.services import capability_tokens as cap_svc

    kind = parent_kind.strip().lower()
    if kind not in ("note", "task"):
        raise ValueError("parent_kind must be 'note' or 'task'")
    pid = uuid.UUID(parent_id)
    resource_kind = cap_svc.RESOURCE_NOTE if kind == "note" else cap_svc.RESOURCE_TASK
    async with _tenant(token, org_id) as (session, org, user):
        grant = await cap_svc.mint(
            session,
            org_id=org,
            actor_id=user,
            action=cap_svc.ACTION_ATTACHMENT_READ,
            resource_kind=resource_kind,
            resource_id=pid,
            ttl_seconds=ttl_seconds,
        )
        metas = await att_svc.list_attachments(
            session,
            org_id=org,
            note_id=pid if kind == "note" else None,
            task_id=pid if kind == "task" else None,
        )
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    auth = f"Bearer {grant.raw}"
    attachments: list[dict[str, Any]] = []
    for m in metas:
        url = f"{base}/api/attachments/{m.id}/download"
        attachments.append(
            {
                "id": str(m.id),
                "filename": m.filename,
                "mime_type": m.mime_type,
                "size_bytes": m.size_bytes,
                "url": url,
                "curl": (
                    f"curl -fsS '{url}' -H 'Authorization: {auth}' -o {shlex.quote(m.filename)}"
                ),
            }
        )
    return {
        "parent_kind": kind,
        "parent_id": str(pid),
        "attachment_count": len(attachments),
        "expires_at": grant.expires_at.isoformat(),
        "authorization": auth,
        "attachments": attachments,
        "notes": (
            "Each 'curl' already carries a single capability token scoped to "
            "reading ONLY this " + kind + "'s attachments; no PAT and no "
            "X-Workspace-Id needed. The token is multi-use until 'expires_at' "
            "(so all the curls above share it) and is never consumed. Run each "
            "curl to write the file to the current directory under its original "
            "name; adjust the -o path as needed. If 'attachments' is empty the "
            + kind
            + " has none."
        ),
    }


def _capability_curl(
    *,
    url: str,
    method: str,
    raw_token: str,
    mode: str,
    expires_at: str,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Shared shape for the capability-token block recipes: a ready ``curl``
    with the ephemeral ``mycelium_cap_`` token baked into the Authorization
    header (no ``$MYCELIUM_TOKEN`` placeholder, no ``X-Workspace-Id`` -- the org
    is in the token), mirroring ``set_note_part_body_capability`` /
    ``download_attachment_capability``.

    ``mode``: ``download`` (GET the raw body to a file, dumping headers so
    the caller captures ``X-Version`` + ``X-Body-SHA256`` for a later
    patch), ``markdown`` (full-body replace via ``--data-binary``), or
    ``diff`` (apply a strict unified diff via ``--data-binary``)."""
    auth = f"Bearer {raw_token}"
    if mode == "download":
        curl = f"curl -fsS -D - '{url}' -H 'Authorization: {auth}' -o <path-to-file>"
        headers = {"Authorization": auth}
        note = (
            "Multi-use read capability baked into the Authorization header "
            "(NOT consumed; expires at expires_at). '-D -' dumps the response "
            "headers so you capture X-Version and X-Body-SHA256 -- the base "
            "gate inputs for a later patch. No PAT and no X-Workspace-Id. The "
            "body is written to <path-to-file>."
        )
    else:
        content_type = "text/markdown; charset=utf-8" if mode == "markdown" else "text/x-diff"
        target = "<path-to-file>" if mode == "markdown" else "<path-to-patch.diff>"
        curl = (
            f"curl -fsS -X {method} '{url}' \\\n"
            f"  -H 'Authorization: {auth}' \\\n"
            f"  -H 'Content-Type: {content_type}' \\\n"
            f"  --data-binary @{target}"
        )
        headers = {"Authorization": auth, "Content-Type": content_type}
        if mode == "markdown":
            note = (
                "Single-use write capability baked into the Authorization "
                "header; consumed on first success, expires at expires_at. "
                "Fill <path-to-file> with the local UTF-8 markdown. A retried "
                "409 (stale expected_version) does not burn it. No PAT and no "
                "X-Workspace-Id."
            )
        else:
            note = (
                "Single-use patch capability for a STRICT unified diff. "
                "Workflow: first GET .../raw (the matching get_* capability "
                "tool) to capture X-Version and X-Body-SHA256, edit the file "
                "locally, produce a unified diff (diff -u / git diff), then run "
                "this. The base gate refuses to apply if the body drifted (409 "
                "patch.stale, nothing mutates); a diff that does not apply is "
                "422. Consumed on first success; expires at expires_at. No PAT "
                "and no X-Workspace-Id."
            )
    out: dict[str, Any] = {
        "endpoint": url,
        "method": method,
        "curl": curl,
        "headers": headers,
        "expires_at": expires_at,
        "notes": note,
    }
    if max_bytes is not None:
        out["max_bytes"] = max_bytes
    return out


# Single-id text blocks reachable via the generic *_text_block_capability
# tools. Note parts live under a two-id path (/notes/{id}/parts/{id}) and
# keep dedicated tools. Per kind: (collection, leaf segment, write method).
_TEXT_BLOCK_ROUTES: dict[str, tuple[str, str, str]] = {
    "task_description": ("tasks", "description", "PUT"),
    "annotation": ("annotations", "body", "PATCH"),
}


def _text_block_segment(kind: str, resource_id: str) -> str:
    route = _TEXT_BLOCK_ROUTES.get(kind)
    if route is None:
        raise ValueError(
            "kind must be 'task_description' or 'annotation' "
            "(note parts use the dedicated get/set/patch_note_part_body_capability tools)"
        )
    collection, leaf, _ = route
    return f"{collection}/{resource_id}/{leaf}"


@mcp.tool()
async def upload_attachment_capability(
    token: str,
    org_id: str,
    parent_kind: str,
    parent_id: str,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Mint ONE single-use capability token that UPLOADS a file to a note or
    task with NO long-lived PAT and NO ``X-Workspace-Id`` (the org is baked
    into the token), and return a ready multipart ``curl``. The symmetric
    counterpart of ``download_attachment_capability``.

    ``parent_kind`` is ``"task"`` or ``"note"``; ``parent_id`` its id. The
    token is scoped to ``attachment:write`` on that parent and consumed on
    the first successful upload; it expires in ``ttl_seconds`` (default
    300). Backend-agnostic: the upload lands through the backend gateway on
    the default ``pg`` store, so no S3 is needed and the file bytes never
    ride a tool argument. If the agent DOES have the Mycelium CLI, prefer it:
    there the credential never leaves the machine."""
    from mycelium_core.config import get_settings
    from mycelium_core.services import capability_tokens as cap_svc

    kind = parent_kind.strip().lower()
    if kind not in ("note", "task"):
        raise ValueError("parent_kind must be 'note' or 'task'")
    pid = uuid.UUID(parent_id)
    resource_kind = cap_svc.RESOURCE_NOTE if kind == "note" else cap_svc.RESOURCE_TASK
    async with _tenant(token, org_id) as (session, org, user):
        grant = await cap_svc.mint(
            session,
            org_id=org,
            actor_id=user,
            action=cap_svc.ACTION_ATTACHMENT_WRITE,
            resource_kind=resource_kind,
            resource_id=pid,
            ttl_seconds=ttl_seconds,
        )
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    collection = "notes" if kind == "note" else "tasks"
    url = f"{base}/api/{collection}/{pid}/attachments"
    auth = f"Bearer {grant.raw}"
    curl = (
        f"curl -fsS -X POST '{url}' \\\n"
        f"  -H 'Authorization: {auth}' \\\n"
        f"  -F 'file=@<path-to-file>'"
    )
    return {
        "endpoint": url,
        "method": "POST",
        "curl": curl,
        "headers": {"Authorization": auth},
        "max_bytes": settings.attachment_max_bytes,
        "expires_at": grant.expires_at.isoformat(),
        "notes": (
            "multipart/form-data upload (field 'file'); the Authorization "
            "header carries a single-use attachment:write capability scoped to "
            "this " + kind + ", consumed on first success. No PAT and no "
            "X-Workspace-Id. Fill <path-to-file> with the local path. "
            "Backend-agnostic (works on the default pg store; no S3 needed). "
            "The bytes never pass through MCP."
        ),
    }


@mcp.tool()
async def get_note_part_body_capability(
    token: str,
    org_id: str,
    note_id: str,
    part_id: str,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Mint a multi-use ``note_part_body:read`` capability and return a
    ``curl`` that downloads THIS part's markdown body to a file. ``-D -``
    dumps the response headers so you capture ``X-Version`` +
    ``X-Body-SHA256`` (the base-gate inputs a later
    ``patch_note_part_body_capability`` needs). No PAT, no X-Workspace-Id;
    not consumed; expires in ``ttl_seconds`` (default 300)."""
    from mycelium_core.config import get_settings
    from mycelium_core.services import capability_tokens as cap_svc

    async with _tenant(token, org_id) as (session, org, user):
        grant = await cap_svc.mint(
            session,
            org_id=org,
            actor_id=user,
            action=cap_svc.ACTION_NOTE_PART_BODY_READ,
            resource_kind=cap_svc.RESOURCE_NOTE_PART,
            resource_id=uuid.UUID(part_id),
            ttl_seconds=ttl_seconds,
        )
    base = get_settings().frontend_base_url.rstrip("/")
    url = f"{base}/api/notes/{note_id}/parts/{part_id}/body/raw"
    return _capability_curl(
        url=url,
        method="GET",
        raw_token=grant.raw,
        mode="download",
        expires_at=grant.expires_at.isoformat(),
    )


@mcp.tool()
async def patch_note_part_body_capability(
    token: str,
    org_id: str,
    note_id: str,
    part_id: str,
    expected_version: int,
    base_sha256: str,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Mint a single-use ``note_part_body:patch`` capability and return a
    ``curl`` that applies a STRICT unified diff to THIS part's body. Get the
    body first with ``get_note_part_body_capability`` to obtain
    ``expected_version`` + ``base_sha256`` (the ``X-Version`` /
    ``X-Body-SHA256`` headers). The base gate refuses to apply if the body
    drifted (409, nothing mutates); a diff that does not apply cleanly is
    422. Consumed on first success; expires in ``ttl_seconds``."""
    from mycelium_core.config import get_settings
    from mycelium_core.services import capability_tokens as cap_svc

    async with _tenant(token, org_id) as (session, org, user):
        grant = await cap_svc.mint(
            session,
            org_id=org,
            actor_id=user,
            action=cap_svc.ACTION_NOTE_PART_BODY_PATCH,
            resource_kind=cap_svc.RESOURCE_NOTE_PART,
            resource_id=uuid.UUID(part_id),
            ttl_seconds=ttl_seconds,
        )
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    url = (
        f"{base}/api/notes/{note_id}/parts/{part_id}/body/patch"
        f"?expected_version={expected_version}&base_sha256={base_sha256}"
    )
    return _capability_curl(
        url=url,
        method="POST",
        raw_token=grant.raw,
        mode="diff",
        max_bytes=settings.note_patch_max_bytes,
        expires_at=grant.expires_at.isoformat(),
    )


@mcp.tool()
async def get_text_block_capability(
    token: str,
    org_id: str,
    kind: str,
    resource_id: str,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Mint a multi-use ``<kind>:read`` capability for a task description
    (``kind='task_description'``, ``resource_id`` = task id) or a comment
    body (``kind='annotation'``, ``resource_id`` = annotation id), and return
    a ``curl`` that downloads it to a file. ``-D -`` dumps headers so you
    capture ``X-Version`` + ``X-Body-SHA256`` for a later patch. Note parts
    use ``get_note_part_body_capability`` (two-id path). No PAT, no
    X-Workspace-Id; not consumed."""
    from mycelium_core.config import get_settings
    from mycelium_core.services import capability_tokens as cap_svc

    seg = _text_block_segment(kind, resource_id)
    async with _tenant(token, org_id) as (session, org, user):
        grant = await cap_svc.mint(
            session,
            org_id=org,
            actor_id=user,
            action=cap_svc.text_block_action(kind, "read"),
            resource_kind=cap_svc.text_block_resource_kind(kind),
            resource_id=uuid.UUID(resource_id),
            ttl_seconds=ttl_seconds,
        )
    base = get_settings().frontend_base_url.rstrip("/")
    url = f"{base}/api/{seg}/raw"
    return _capability_curl(
        url=url,
        method="GET",
        raw_token=grant.raw,
        mode="download",
        expires_at=grant.expires_at.isoformat(),
    )


@mcp.tool()
async def set_text_block_capability(
    token: str,
    org_id: str,
    kind: str,
    resource_id: str,
    expected_version: int,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Mint a single-use ``<kind>:write`` capability for a task description
    or comment body and return a ``curl`` that REPLACES it with a local file
    (``--data-binary``). ``expected_version`` is the optimistic cursor (409
    on mismatch). Consumed on first success. Note parts use
    ``set_note_part_body_capability``."""
    from mycelium_core.config import get_settings
    from mycelium_core.services import capability_tokens as cap_svc

    seg = _text_block_segment(kind, resource_id)
    method = _TEXT_BLOCK_ROUTES[kind][2]
    async with _tenant(token, org_id) as (session, org, user):
        grant = await cap_svc.mint(
            session,
            org_id=org,
            actor_id=user,
            action=cap_svc.text_block_action(kind, "write"),
            resource_kind=cap_svc.text_block_resource_kind(kind),
            resource_id=uuid.UUID(resource_id),
            ttl_seconds=ttl_seconds,
        )
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    url = f"{base}/api/{seg}/stream?expected_version={expected_version}"
    return _capability_curl(
        url=url,
        method=method,
        raw_token=grant.raw,
        mode="markdown",
        max_bytes=settings.note_body_max_bytes,
        expires_at=grant.expires_at.isoformat(),
    )


@mcp.tool()
async def patch_text_block_capability(
    token: str,
    org_id: str,
    kind: str,
    resource_id: str,
    expected_version: int,
    base_sha256: str,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Mint a single-use ``<kind>:patch`` capability for a task description
    or comment body and return a ``curl`` that applies a STRICT unified diff
    (``--data-binary``). Get the body first with ``get_text_block_capability``
    to obtain ``expected_version`` + ``base_sha256``. 409 if the body
    drifted, 422 if the diff does not apply, nothing mutates on failure.
    Consumed on first success. Note parts use
    ``patch_note_part_body_capability``."""
    from mycelium_core.config import get_settings
    from mycelium_core.services import capability_tokens as cap_svc

    seg = _text_block_segment(kind, resource_id)
    async with _tenant(token, org_id) as (session, org, user):
        grant = await cap_svc.mint(
            session,
            org_id=org,
            actor_id=user,
            action=cap_svc.text_block_action(kind, "patch"),
            resource_kind=cap_svc.text_block_resource_kind(kind),
            resource_id=uuid.UUID(resource_id),
            ttl_seconds=ttl_seconds,
        )
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    url = f"{base}/api/{seg}/patch?expected_version={expected_version}&base_sha256={base_sha256}"
    return _capability_curl(
        url=url,
        method="POST",
        raw_token=grant.raw,
        mode="diff",
        max_bytes=settings.note_patch_max_bytes,
        expires_at=grant.expires_at.isoformat(),
    )


@mcp.tool()
async def add_comment_instructions(
    token: str,
    org_id: str,
    doc_kind: str,
    doc_id: str,
    anchor_quote: str | None = None,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """Recipe for a TOKEN-FREE inline comment: stream the comment text
    from a local file into ``annotation.body`` instead of spending it as
    an ``add_annotation`` argument. ``doc_kind`` is ``note_part`` (doc_id
    = note-part id) or ``task_description`` (doc_id = task id);
    ``anchor_quote`` pins it to a passage (omit for a whole-document /
    work-diary comment); ``parent_id`` makes it a reply. An agent token
    attributes the comment to its AI-assistant identity, same as the MCP
    tool. The response carries the new annotation JSON."""
    from urllib.parse import quote

    from mycelium_core.config import get_settings

    if doc_kind not in ("note_part", "task_description"):
        raise ValueError("doc_kind must be 'note_part' or 'task_description'")
    async with _tenant(token, org_id) as (_s, org, _user):
        pass
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    params = [f"doc_kind={quote(doc_kind)}", f"doc_id={doc_id}"]
    if anchor_quote is not None:
        params.append(f"anchor_quote={quote(anchor_quote)}")
    if parent_id is not None:
        params.append(f"parent_id={parent_id}")
    url = f"{base}/api/annotations/comment/stream?" + "&".join(params)
    return _text_stream_recipe(
        url=url,
        org=org,
        method="POST",
        max_bytes=settings.note_body_max_bytes,
        returns="the new annotation JSON (id, kind, status, version)",
    )


@mcp.tool()
async def propose_suggestion_instructions(
    token: str,
    org_id: str,
    doc_kind: str,
    doc_id: str,
    original_text: str,
    rationale: str = "",
) -> dict[str, Any]:
    """Recipe for a TOKEN-FREE suggestion: stream the PROPOSED replacement
    text (the large free-form field) from a local file as the request
    body; the struck ``original_text`` it replaces is a bounded query
    param (the agent already holds it, capped to keep the URL short). An
    empty file is a deletion suggestion. Nothing changes in the document
    until the suggestion is accepted. ``doc_kind`` is ``note_part`` or
    ``task_description``. The response carries the new annotation JSON."""
    from urllib.parse import quote

    from mycelium_core.config import get_settings

    if doc_kind not in ("note_part", "task_description"):
        raise ValueError("doc_kind must be 'note_part' or 'task_description'")
    async with _tenant(token, org_id) as (_s, org, _user):
        pass
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    params = [
        f"doc_kind={quote(doc_kind)}",
        f"doc_id={doc_id}",
        f"original_text={quote(original_text)}",
    ]
    if rationale:
        params.append(f"rationale={quote(rationale)}")
    url = f"{base}/api/annotations/suggestion/stream?" + "&".join(params)
    return _text_stream_recipe(
        url=url,
        org=org,
        method="POST",
        max_bytes=settings.note_body_max_bytes,
        returns="the new suggestion JSON (id, original_text, proposed_text, version)",
    )


@mcp.tool()
async def edit_annotation_body_instructions(
    token: str,
    org_id: str,
    annotation_id: str,
    expected_version: int,
) -> dict[str, Any]:
    """Recipe for a TOKEN-FREE replace of an annotation's body (a
    comment's text or a suggestion's rationale): stream the new text from
    a local file instead of spending it as an ``edit_annotation``
    argument. ``expected_version`` is the optimistic cursor (a mismatch is
    stale_version -> 409); author-or-admin only. The response carries the
    new version."""
    from mycelium_core.config import get_settings

    async with _tenant(token, org_id) as (_s, org, _user):
        pass
    settings = get_settings()
    base = settings.frontend_base_url.rstrip("/")
    url = f"{base}/api/annotations/{annotation_id}/body/stream?expected_version={expected_version}"
    return _text_stream_recipe(
        url=url,
        org=org,
        method="PATCH",
        max_bytes=settings.note_body_max_bytes,
        returns="the annotation id + new version",
    )


@mcp.tool()
async def list_attachments(
    token: str,
    org_id: str,
    note_id: str | None = None,
    task_id: str | None = None,
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List a note's OR a task's attachments (metadata only; the binary
    is never returned). Pass exactly one of note_id / task_id.
    ``fields`` opt-in keeps only the named columns (``id`` always kept)."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await attachments_svc.list_attachments(
            s,
            org_id=org,
            note_id=uuid.UUID(note_id) if note_id else None,
            task_id=uuid.UUID(task_id) if task_id else None,
        )
        return [
            _project_fields(
                {
                    "id": str(r.id),
                    "note_id": str(r.note_id) if r.note_id else None,
                    "task_id": str(r.task_id) if r.task_id else None,
                    "filename": r.filename,
                    "mime_type": r.mime_type,
                    "size_bytes": r.size_bytes,
                    "created_at": r.created_at.isoformat(),
                },
                fields,
            )
            for r in rows
        ]


@mcp.tool()
async def delete_attachment(token: str, org_id: str, attachment_id: str) -> dict[str, Any]:
    """Hard-delete an attachment (the stored blob goes with the row).
    Member-level, org-scoped (RLS)."""
    async with _tenant(token, org_id) as (s, org, user):
        await attachments_svc.delete_attachment(
            s,
            org_id=org,
            actor_id=user,
            attachment_id=uuid.UUID(attachment_id),
        )
        return {"attachment_id": attachment_id, "deleted": True}


# --- F7: electronic invoicing (FR-9) ---


def _invoice(i: Invoice) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "kind": i.kind.value,
        "document_type": i.document_type.value,
        "series": i.series,
        "year": i.year,
        "number": i.number,
        "state": i.state.value,
        "total": str(i.total),
        "identificativo_sdi": i.identificativo_sdi,
        "sdi_status": i.sdi_status.value,
        # Non-null = an unsettled dispatch owns the invoice (ADR-0046);
        # with a null identificativo_sdi it marks the invoice retryable.
        "sdi_dispatch_started_at": (
            i.sdi_dispatch_started_at.isoformat() if i.sdi_dispatch_started_at else None
        ),
        "conservation_status": i.conservation_status.value,
        "version": i.version,
    }


# --- AltriDatiGestionali (FatturaPA 2.2.1.16, migration 0088) ---

# The block's four sub-elements. Held as a set so an unrecognised key is
# REFUSED rather than dropped: a misspelt "riferimeno_testo" would
# otherwise cost the invoice a fiscally relevant reference in silence,
# and nobody would notice until SdI (or the client) complained.
_ALTRI_DATI_KEYS = frozenset(
    {"tipo_dato", "riferimento_testo", "riferimento_numero", "riferimento_data"}
)


def _adg_invalid(detail: str) -> DomainError:
    # The same code the service raises for a facet violation: one stable
    # MessageCode for "this block is malformed", whether the wire shape
    # or the value was wrong (ADR-0017 -- code, never rendered text).
    return DomainError(MessageCode.INVOICE_ALTRI_DATI_INVALID, detail=detail)


def _adg_date(value: Any) -> dt.date:
    """RiferimentoData is an xs:date. Accept the plain "YYYY-MM-DD" an
    agent is told to send, and tolerate a full ISO timestamp by keeping
    its calendar day: models emit one often enough that refusing it
    would only cost a round trip (the service coerces likewise)."""
    text = str(value)
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        raise _adg_invalid("riferimento_data: not an ISO date (YYYY-MM-DD)") from None


def _adg_in(blocks: list[dict[str, Any]]) -> list[invoice_svc.AltriDatiBlock]:
    """Wire dicts -> the service's ``AltriDatiBlock``, order preserved.

    Shape and scalar typing only. The XSD facets (tipo_dato 1..10
    Basic-Latin, testo <=60 Latin-1, numero <=11 integer digits and <=8
    decimals) are checked once inside the service, before any write, so
    MCP and REST cannot drift apart on what a valid block is."""
    out: list[invoice_svc.AltriDatiBlock] = []
    for raw in blocks:
        # The generated JSON schema is a bare array of objects, so a
        # model that passes a list of strings gets a coded error rather
        # than an AttributeError surfaced as an internal failure.
        if not isinstance(raw, dict):
            raise _adg_invalid("each block must be an object")
        unknown = sorted(set(raw) - _ALTRI_DATI_KEYS)
        if unknown:
            raise _adg_invalid(f"unknown field(s): {', '.join(unknown)}")
        tipo = raw.get("tipo_dato")
        if not isinstance(tipo, str):
            raise _adg_invalid("tipo_dato: required (a label, e.g. INTENTO)")
        testo = raw.get("riferimento_testo")
        if testo is not None and not isinstance(testo, str):
            raise _adg_invalid("riferimento_testo: not a string")
        numero_raw = raw.get("riferimento_numero")
        numero: Decimal | None = None
        if numero_raw is not None:
            # str() bridge, as for unit_price below: a JSON number lands
            # as a float and Decimal(float) would carry the double's
            # binary artefacts into a field the XML prints verbatim.
            try:
                numero = Decimal(str(numero_raw))
            except InvalidOperation:
                raise _adg_invalid("riferimento_numero: not a number") from None
        data_raw = raw.get("riferimento_data")
        out.append(
            invoice_svc.AltriDatiBlock(
                tipo_dato=tipo,
                riferimento_testo=testo,
                riferimento_numero=numero,
                riferimento_data=_adg_date(data_raw) if data_raw is not None else None,
            )
        )
    return out


def _adg_out(b: InvoiceLineAltriDati) -> dict[str, Any]:
    # Decimal and date as strings: JSON has no exact decimal, and a
    # float round trip must not touch a fiscal value. The number goes
    # through the XML emitter's own _amount8 so the read-back is the
    # string the document carries ("12.00", not the Numeric(21,8)
    # "12.00000000") -- lossless here, since the service already caps
    # the value at 11 integer digits and 8 decimals.
    return {
        "ord": b.ord,
        "tipo_dato": b.tipo_dato,
        "riferimento_testo": b.riferimento_testo,
        "riferimento_numero": (
            invoice_format_svc._amount8(b.riferimento_numero)
            if b.riferimento_numero is not None
            else None
        ),
        "riferimento_data": b.riferimento_data.isoformat() if b.riferimento_data else None,
    }


def _invoice_line(ln: InvoiceLine, blocks: list[InvoiceLineAltriDati]) -> dict[str, Any]:
    # Amounts as strings at their stored precision -- Numeric(12,4) /
    # Numeric(14,4), which is also what the XML now emits: a float here
    # would re-introduce exactly the rounding the emitter was fixed to
    # avoid (Quantita x PrezzoUnitario must equal PrezzoTotale).
    return {
        "id": str(ln.id),
        "line_no": ln.line_no,
        "description": ln.description,
        "quantity": str(ln.quantity),
        "unit_price": str(ln.unit_price),
        "vat_rate": str(ln.vat_rate),
        "vat_nature": ln.vat_nature,
        "altri_dati": [_adg_out(b) for b in blocks],
    }


@mcp.tool()
async def set_issuer_profile(
    token: str,
    org_id: str,
    legal_name: str,
    label: str = "Principale",
    vat_number: str | None = None,
    tax_code: str | None = None,
    address: str = "",
    postal_code: str = "",
    city: str = "",
    sdi_code: str | None = None,
) -> dict[str, Any]:
    """Create-or-update the default issuer profile, the invoice
    "intestazione" (admin). Idempotent on the org default: updates it if
    one exists, else creates it (and flags it default). ``sdi_code`` is
    this issuer's own reception CodiceDestinatario (passive SdI cycle)."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_scope(s, "invoices:write")
        current = await invoice_svc.get_default_issuer_profile(s, org_id=org)
        if current is None:
            p = await invoice_svc.create_issuer_profile(
                s,
                org_id=org,
                actor_id=user,
                label=label,
                legal_name=legal_name,
                vat_number=vat_number,
                tax_code=tax_code,
                address=address,
                postal_code=postal_code,
                city=city,
                sdi_code=sdi_code,
                is_default=True,
            )
        else:
            values: dict[str, Any] = {
                "label": label,
                "legal_name": legal_name,
                "vat_number": vat_number,
                "tax_code": tax_code,
                "address": address,
                "postal_code": postal_code,
                "city": city,
            }
            if sdi_code is not None:
                values["sdi_code"] = sdi_code
            p = await invoice_svc.update_issuer_profile(
                s,
                org_id=org,
                actor_id=user,
                profile_id=current.id,
                values=values,
            )
        return {
            "id": str(p.id),
            "label": p.label,
            "legal_name": p.legal_name,
            "is_default": p.is_default,
            "version": p.version,
        }


@mcp.tool()
async def create_invoice(
    token: str, org_id: str, client_tag_id: str, series: str = "A"
) -> dict[str, Any]:
    """Create a draft invoice."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_scope(s, "invoices:write")
        inv = await invoice_svc.create_draft(
            s,
            org_id=org,
            actor_id=user,
            client_tag_id=uuid.UUID(client_tag_id),
            series=series,
        )
        return _invoice(inv)


@mcp.tool()
async def add_invoice_line(
    token: str,
    org_id: str,
    invoice_id: str,
    description: str,
    unit_price: float,
    quantity: float = 1.0,
    vat_rate: float | None = None,
    vat_nature: str | None = None,
    altri_dati: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Add a line to a draft invoice.

    ``vat_rate``/``vat_nature`` left unset: the service resolves them from the
    issuer's regime -- forfettario (RF19) -> 0% + Natura N2.2, ordinary
    regime -> 22%. Pass them explicitly to override (e.g. an exempt or
    reverse-charge line carries its own Natura). The previous hard 22%
    default silently produced regime-inconsistent lines for forfettario
    issuers, which SdI rejects.

    ``quantity`` and ``unit_price`` are stored AND emitted in the XML
    with 4 decimals; a 5th decimal is rounded away by the database.

    ``altri_dati`` is the line's AltriDatiGestionali (FatturaPA
    2.2.1.16): OMIT IT for the normal line, which carries none. When
    given it is an ORDERED list of blocks (the order is the emission
    order), each {tipo_dato, riferimento_testo?, riferimento_numero?,
    riferimento_data?}.

    ``tipo_dato`` is a LABEL naming the KIND of data -- max 10
    characters, ASCII only -- NOT a description: write a sentence there
    and SdI rejects the invoice. Free text belongs in
    ``riferimento_testo`` (<=60 characters, accented Latin-1 allowed).
    ``riferimento_numero`` is a decimal (<=11 integer digits, <=8
    decimals); ``riferimento_data`` an ISO date "YYYY-MM-DD".

    The spec fixes no enum, but three labels are conventional:
    - "INTENTO" -- dichiarazione d'intento. ``riferimento_testo`` =
      protocollo + progressivo joined by "-" or "/" (e.g.
      "08060120347895-000001"); the other two fields stay empty.
    - "N.DOC.COMM" -- documento commerciale. ``riferimento_testo`` = its
      id, ``riferimento_numero`` = its progressivo, ``riferimento_data``
      = its date.
    - "NB3" -- bollo exemption between a bank and its account holders:
      all three reference fields stay EMPTY.

    Draft-only, like every line edit: once transmitted the XML is frozen
    (ADR-0009, ADR-0046). To change the blocks of a line that already
    exists use ``set_invoice_line_altri_dati``."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_scope(s, "invoices:write")
        # Parsed before the call so a malformed block fails the tool
        # instead of half-creating a line (the service validates the
        # facets under the same rule).
        blocks = _adg_in(altri_dati) if altri_dati is not None else None
        ln = await invoice_svc.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=uuid.UUID(invoice_id),
            description=description,
            unit_price=Decimal(str(unit_price)),
            quantity=Decimal(str(quantity)),
            vat_rate=Decimal(str(vat_rate)) if vat_rate is not None else None,
            vat_nature=vat_nature,
            altri_dati=blocks,
        )
        # Echoed back so the caller sees what was actually stored
        # (normalised, ord assigned). A line born without blocks has
        # none, so the read is skipped rather than queried for [].
        stored = (
            await invoice_svc.list_line_altri_dati(s, org_id=org, line_id=ln.id) if blocks else []
        )
        return {
            "id": str(ln.id),
            "line_no": ln.line_no,
            "altri_dati": [_adg_out(b) for b in stored],
        }


@mcp.tool()
async def set_invoice_line_altri_dati(
    token: str,
    org_id: str,
    invoice_id: str,
    line_id: str,
    altri_dati: list[dict[str, Any]],
) -> dict[str, Any]:
    """Set one draft line's AltriDatiGestionali (FatturaPA 2.2.1.16).

    A REPLACE of the whole ordered set, not an append: pass EVERY block
    the line must keep, and ``[]`` to clear them. List order is the
    emission order in the XML. Get ``line_id`` from
    ``list_invoice_lines`` (or from ``add_invoice_line``'s result).

    Each block is {tipo_dato, riferimento_testo?, riferimento_numero?,
    riferimento_data?}.

    ``tipo_dato`` is a LABEL naming the KIND of data -- max 10
    characters, ASCII only -- NOT a description: write a sentence there
    and SdI rejects the invoice. Free text belongs in
    ``riferimento_testo`` (<=60 characters, accented Latin-1 allowed).
    ``riferimento_numero`` is a decimal (<=11 integer digits, <=8
    decimals); ``riferimento_data`` an ISO date "YYYY-MM-DD".

    The spec fixes no enum, but three labels are conventional:
    - "INTENTO" -- dichiarazione d'intento. ``riferimento_testo`` =
      protocollo + progressivo joined by "-" or "/" (e.g.
      "08060120347895-000001"); the other two fields stay empty.
    - "N.DOC.COMM" -- documento commerciale. ``riferimento_testo`` = its
      id, ``riferimento_numero`` = its progressivo, ``riferimento_data``
      = its date.
    - "NB3" -- bollo exemption between a bank and its account holders:
      all three reference fields stay EMPTY.

    Draft-only (scope ``invoices:write``): a transmitted invoice is
    immutable and its XML frozen (ADR-0009, ADR-0046), so a later edit
    could not reach what was sent -- the call is refused, not ignored.
    A line carries no block by default; that is the normal case."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_scope(s, "invoices:write")
        rows = await invoice_svc.replace_line_altri_dati(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=uuid.UUID(invoice_id),
            line_id=uuid.UUID(line_id),
            blocks=_adg_in(altri_dati),
        )
        return {
            "invoice_id": invoice_id,
            "line_id": line_id,
            "altri_dati": [_adg_out(b) for b in rows],
        }


@mcp.tool()
async def transmit_invoice(token: str, org_id: str, invoice_id: str) -> dict[str, Any]:
    """Validate, allocate the progressive number and transmit (channel
    injected; manual export by default)."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_scope(s, "invoices:write")
        inv = await invoice_svc.transmit(
            s, org_id=org, actor_id=user, invoice_id=uuid.UUID(invoice_id)
        )
        return _invoice(inv)


@mcp.tool()
async def invoice_credit_note(
    token: str, org_id: str, parent_invoice_id: str, purpose: str | None = None
) -> dict[str, Any]:
    """Create a TD04 credit note linked to a transmitted invoice."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_scope(s, "invoices:write")
        inv = await invoice_svc.create_credit_note(
            s,
            org_id=org,
            actor_id=user,
            parent_invoice_id=uuid.UUID(parent_invoice_id),
            purpose=purpose,
        )
        return _invoice(inv)


@mcp.tool()
async def ingest_sdi_receipt(
    token: str, org_id: str, identificativo_sdi: str, outcome: str
) -> dict[str, Any]:
    """Correlate an SdI receipt (RC/MC/NS/AT) by IdentificativoSdI."""
    async with _tenant(token, org_id) as (s, org, user):
        await _require_scope(s, "invoices:write")
        inv = await invoice_svc.ingest_receipt(
            s,
            org_id=org,
            actor_id=user,
            identificativo_sdi=identificativo_sdi,
            outcome=outcome,
        )
        return _invoice(inv)


@mcp.tool()
async def list_invoices(
    token: str,
    org_id: str,
    client_tag_id: str | None = None,
    issuer_profile_id: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List invoices, newest first. Filter by ``client_tag_id`` (the recipient)
    -- the FIRST result is that client's most recent invoice ("controlla
    l'ultima fattura del cliente XY") -- and/or ``issuer_profile_id`` (the
    cedente), and/or ``state`` (draft|transmitted|delivered|accepted|rejected).
    Read-only (scope ``invoices:read``)."""
    async with _tenant(token, org_id) as (s, org, _user):
        await _require_scope(s, "invoices:read")
        rows = await invoice_svc.list_invoices(
            s,
            org_id=org,
            client_tag_id=uuid.UUID(client_tag_id) if client_tag_id else None,
            issuer_profile_id=uuid.UUID(issuer_profile_id) if issuer_profile_id else None,
            state=InvoiceState(state) if state else None,
        )
        return [_invoice(i) for i in rows[: max(1, limit)]]


@mcp.tool()
async def get_invoice(token: str, org_id: str, invoice_id: str) -> dict[str, Any]:
    """Read one invoice's status + data (state, SdI status, number, total,
    conservation). Read-only (scope ``invoices:read``)."""
    async with _tenant(token, org_id) as (s, org, _user):
        await _require_scope(s, "invoices:read")
        inv = await invoice_svc.get_invoice(s, org_id=org, invoice_id=uuid.UUID(invoice_id))
        return _invoice(inv)


@mcp.tool()
async def list_invoice_lines(token: str, org_id: str, invoice_id: str) -> list[dict[str, Any]]:
    """List an invoice's lines with their AltriDatiGestionali blocks.

    Lines come in emission order, each with ``altri_dati`` (``[]`` for
    the normal line, which carries none).

    This is how an agent gets the ``line_id`` that
    ``set_invoice_line_altri_dati`` needs on an invoice it did not just
    build itself, and how it reads back what a transmitted invoice
    actually emitted. Amounts come back as strings at their stored
    4-decimal precision, which is what the XML emits. Read-only (scope
    ``invoices:read``)."""
    async with _tenant(token, org_id) as (s, org, _user):
        await _require_scope(s, "invoices:read")
        inv_id = uuid.UUID(invoice_id)
        # Existence check first: list_lines on an id outside the tenant
        # returns [], which would read as "an invoice with no lines".
        await invoice_svc.get_invoice(s, org_id=org, invoice_id=inv_id)
        lines = await invoice_svc.list_lines(s, org_id=org, invoice_id=inv_id)
        # One grouped query for the whole invoice, not one per line.
        grouped = await invoice_svc.list_invoice_altri_dati(s, org_id=org, invoice_id=inv_id)
        return [_invoice_line(ln, grouped.get(ln.id, [])) for ln in lines]


@mcp.tool()
async def get_invoice_xml(token: str, org_id: str, invoice_id: str) -> dict[str, Any]:
    """Return an invoice's FatturaPA XML inline (the frozen transmitted document,
    or a live preview for a draft). XML is text, so it travels over MCP directly;
    for the courtesy PDF (binary) download it from the REST ``/api/v1`` surface.
    Read-only (scope ``invoices:read``)."""
    async with _tenant(token, org_id) as (s, org, _user):
        await _require_scope(s, "invoices:read")
        xml = await invoice_svc.get_xml_preview(s, org_id=org, invoice_id=uuid.UUID(invoice_id))
        return {"invoice_id": invoice_id, "xml": xml}


@mcp.tool()
async def list_issuer_profiles(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the workspace's issuer profiles (the cedente VAT subjects) so an
    agent can pick which identity to invoice under. Read-only (scope
    ``invoices:read``)."""
    async with _tenant(token, org_id) as (s, org, _user):
        await _require_scope(s, "invoices:read")
        rows = await invoice_svc.list_issuer_profiles(s, org_id=org)
        return [
            {
                "id": str(p.id),
                "label": p.label,
                "legal_name": p.legal_name,
                "vat_number": p.vat_number,
                "is_default": p.is_default,
            }
            for p in rows
        ]


# --- F8: notifications / recurrence / reminders (FR-12) ---


@mcp.tool()
async def set_notification_pref(
    token: str,
    org_id: str,
    user_id: str,
    channel: str,
    target: str,
    enabled: bool = True,
) -> dict[str, Any]:
    """Set a user's per-channel notification preference."""
    async with _tenant(token, org_id) as (s, org, user):
        p = await notif_svc.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=uuid.UUID(user_id),
            channel=NotificationChannelKind(channel),
            enabled=enabled,
            target=target,
        )
        return {"channel": p.channel.value, "enabled": p.enabled}


@mcp.tool()
async def dispatch_notifications(token: str, org_id: str) -> dict[str, Any]:
    """Send pending notifications (per-item fault isolation)."""
    async with _tenant(token, org_id) as (s, org, user):
        r = await notif_svc.dispatch_pending(s, org_id=org, actor_id=user)
        return {"sent": r.sent, "failed": r.failed}


@mcp.tool()
async def create_recurrence(
    token: str,
    org_id: str,
    task_id: str,
    freq: str,
    next_run: str,
    interval: int = 1,
) -> dict[str, Any]:
    """Make a task recurring (mutually exclusive with dependencies)."""
    async with _tenant(token, org_id) as (s, org, user):
        rec = await notif_svc.create_recurrence(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            freq=RecurrenceFreq(freq),
            next_run=dt.datetime.fromisoformat(next_run),
            interval=interval,
        )
        return {"task_id": str(rec.task_id), "freq": rec.freq.value}


@mcp.tool()
async def spawn_due_recurrences(
    token: str, org_id: str, as_of: str | None = None
) -> dict[str, Any]:
    """Materialize due recurrences as independent task rows."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notif_svc.spawn_due(
            s,
            org_id=org,
            actor_id=user,
            now=dt.datetime.fromisoformat(as_of) if as_of else None,
        )
        return {"count": n}


@mcp.tool()
async def scan_reminders(token: str, org_id: str, within_days: int = 1) -> dict[str, Any]:
    """Enqueue idempotent due-date reminders for assignees."""
    async with _tenant(token, org_id) as (s, org, user):
        n = await notif_svc.scan_reminders(s, org_id=org, actor_id=user, within_days=within_days)
        return {"count": n}


# --- F6c: workflows (FR-6) ---


def _workflow(w: WorkflowDefinition) -> dict[str, Any]:
    return {
        "id": str(w.id),
        "name": w.name,
        # What this workflow is for, in the org's own words. Migration 0065
        # added the column and the model says it is "surfaced to MCP agents
        # so they can reason about a task's workflow without inferring from
        # name"; this layer dropped it, so the promise was storage-only.
        "description": w.description,
        "is_default": w.is_default,
        "version": w.version,
    }


def _state(st: WorkflowState) -> dict[str, Any]:
    return {
        "id": str(st.id),
        "name": st.name,
        "ord": st.ord,
        "is_initial": st.is_initial,
        "is_terminal": st.is_terminal,
        # Whether the SPA hides this column by default. An agent listing a
        # board wants to know that a state exists but is not on screen.
        "is_hidden": st.is_hidden,
        # What this state means in THIS workflow. The model's own comment:
        # an agent should know what "in_review" or "blocked" means here
        # "without guessing from the state name". Guessing is what happened
        # on 2026-09-08: a session finishing three tasks found `todo ->
        # verify` refused, had nothing that said what `verify` is for or who
        # moves a task into it, and picked `done` because the transition
        # existed. A name is not a contract.
        "description": st.description,
    }


def _transition(tr: WorkflowTransition) -> dict[str, Any]:
    return {
        "from_state_id": str(tr.from_state_id),
        "to_state_id": str(tr.to_state_id),
    }


@mcp.tool()
async def list_workflows(token: str, org_id: str) -> list[dict[str, Any]]:
    """List the org workflow definitions, each with its description."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await workflow_svc.list_workflows(s, org)
        return [_workflow(w) for w in rows]


@mcp.tool()
async def task_workflow(token: str, org_id: str, task_id: str) -> dict[str, Any]:
    """Which workflow governs THIS task, what its states mean, and where the
    task can go from where it is.

    Ask this before moving a task, not after a transition is refused. There
    is no single workflow to learn once: a project can override the org
    default, so the states and the meanings that apply depend on the task's
    project, and reasoning from a workflow seen on another task is reasoning
    from the wrong machine.

    Returns the workflow with its ``description``, every state with its own
    (ordered, so the board's order is the list's order), the task's current
    state, and ``allowed_next``: the states reachable from it in one step,
    already resolved so a caller does not have to intersect the transition
    table itself. An empty ``allowed_next`` on a non-terminal state is a
    real answer and means the workflow has no way out of here.

    The descriptions are the point. A name is a label: only the description
    says what `verify` is for, who moves a task into it, and what has to be
    true before it leaves. Where one is null, nobody has written it down --
    say so rather than inferring it from the name.
    """
    async with _tenant(token, org_id) as (s, org, _user):
        task = await tasks.get_task(s, org_id=org, task_id=uuid.UUID(task_id))
        wf = await workflow_svc.effective_workflow_for_task(s, org, uuid.UUID(task_id))
        states = await workflow_svc.get_states(s, wf.id)
        transitions = await workflow_svc.list_transitions(s, wf.id)

        by_id = {st.id: st for st in states}
        current = by_id.get(task.state_id) if task.state_id else None

        return {
            "task_id": task_id,
            "workflow": _workflow(wf),
            "is_org_default": wf.is_default,
            "states": [_state(st) for st in states],
            "current_state": _state(current) if current is not None else None,
            "allowed_next": _allowed_next(states, transitions, current),
        }


def _allowed_next(states, transitions, current):  # type: ignore[no-untyped-def]
    """The states one transition away from ``current``, in the board's order.

    A module-level function rather than a comprehension inside the tool
    because it is the only reasoning that tool does, and a tool body reached
    only through a session and a tenant is a tool body no unit test runs.
    That is not hypothetical: the first version of ``task_workflow`` called
    an import that did not exist in this module, the whole suite passed, and
    the linter is what caught it.

    ``current`` of ``None`` yields nothing, which is the honest answer for a
    task sitting in no state: there is no row to read transitions from. It is
    not the same as a state with no way out, and the caller can tell the two
    apart because ``current_state`` is null in one and not in the other.

    Ordered by ``states`` rather than by the transition table, so a board
    reads down the columns the way a human sees them.
    """
    if current is None:
        return []

    reachable = {tr.to_state_id for tr in transitions if tr.from_state_id == current.id}
    return [_state(st) for st in states if st.id in reachable]


@mcp.tool()
async def workflow_states(token: str, org_id: str, workflow_id: str) -> list[dict[str, Any]]:
    """List a workflow's states (ordered)."""
    async with _tenant(token, org_id) as (s, _org, _user):
        rows = await workflow_svc.get_states(s, uuid.UUID(workflow_id))
        return [_state(st) for st in rows]


@mcp.tool()
async def workflow_transitions(token: str, org_id: str, workflow_id: str) -> list[dict[str, Any]]:
    """List a workflow's allowed (from -> to) transitions."""
    async with _tenant(token, org_id) as (s, _org, _user):
        rows = await workflow_svc.list_transitions(s, uuid.UUID(workflow_id))
        return [_transition(tr) for tr in rows]


@mcp.tool()
async def create_workflow(
    token: str,
    org_id: str,
    name: str,
    states: list[dict[str, Any]],
    transitions: list[list[str]],
    description: str | None = None,
) -> dict[str, Any]:
    """Create a workflow. ``states`` items: {name, ord?, is_initial?,
    is_terminal?, is_hidden?, description?} (exactly one initial).
    ``transitions``: [from, to] name pairs.

    Write the descriptions. A state name is a label, not a contract: only
    the workflow's own description says what `verify` is for, who moves a
    task into it, and what has to be true before it leaves. An agent given
    the names alone guesses, and a guess about a terminal state is not
    recoverable by the agent that made it."""
    async with _tenant(token, org_id) as (s, org, user):
        w = await workflow_svc.create_workflow(
            s,
            org_id=org,
            actor_id=user,
            name=name,
            description=description,
            states=[
                StateSpec(
                    name=st["name"],
                    ord=int(st.get("ord", 0)),
                    is_initial=bool(st.get("is_initial", False)),
                    is_terminal=bool(st.get("is_terminal", False)),
                    is_hidden=bool(st.get("is_hidden", False)),
                    description=st.get("description"),
                )
                for st in states
            ],
            transitions=[(t[0], t[1]) for t in transitions],
        )
        return _workflow(w)


@mcp.tool()
async def update_workflow(
    token: str,
    org_id: str,
    workflow_id: str,
    name: str,
    states: list[dict[str, Any]],
    transitions: list[list[str]],
    description: str | None = None,
) -> dict[str, Any]:
    """Rename + reconcile a workflow's states (match by ``id``; new
    ones omit it; dropped only if unused) and replace transitions.

    This is a REPLACE, including the descriptions: a state dict without a
    ``description`` clears the one it had, exactly as it clears an ``ord``
    it does not send. Read the states first and send them back whole."""
    async with _tenant(token, org_id) as (s, org, user):
        await workflow_svc.update_workflow(
            s,
            org_id=org,
            actor_id=user,
            workflow_id=uuid.UUID(workflow_id),
            name=name,
            description=description,
            states=[
                StateEdit(
                    id=uuid.UUID(st["id"]) if st.get("id") else None,
                    name=st["name"],
                    ord=int(st.get("ord", 0)),
                    is_initial=bool(st.get("is_initial", False)),
                    is_terminal=bool(st.get("is_terminal", False)),
                    is_hidden=bool(st.get("is_hidden", False)),
                    description=st.get("description"),
                )
                for st in states
            ],
            transitions=[(t[0], t[1]) for t in transitions],
        )
        return {"workflow_id": workflow_id, "updated": True}


@mcp.tool()
async def delete_workflow(token: str, org_id: str, workflow_id: str) -> dict[str, Any]:
    """Delete a workflow (refused for the default or if its states
    still hold tasks)."""
    async with _tenant(token, org_id) as (s, org, user):
        await workflow_svc.delete_workflow(
            s,
            org_id=org,
            actor_id=user,
            workflow_id=uuid.UUID(workflow_id),
        )
        return {"workflow_id": workflow_id, "deleted": True}


@mcp.tool()
async def set_default_workflow(token: str, org_id: str, workflow_id: str) -> dict[str, Any]:
    """Promote a workflow to the org default (keeps exactly one)."""
    async with _tenant(token, org_id) as (s, org, user):
        await workflow_svc.set_default_workflow(
            s,
            org_id=org,
            actor_id=user,
            workflow_id=uuid.UUID(workflow_id),
        )
        return {"workflow_id": workflow_id, "is_default": True}


@mcp.tool()
async def set_project_workflow(
    token: str,
    org_id: str,
    project_tag_id: str,
    expected_version: int,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Set (or clear, with ``workflow_id=None``) a project's workflow
    override. None falls back to the org default."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await workflow_svc.set_project_workflow(
            s,
            org_id=org,
            actor_id=user,
            project_tag_id=uuid.UUID(project_tag_id),
            workflow_id=uuid.UUID(workflow_id) if workflow_id else None,
            expected_version=expected_version,
        )
        return {"project_tag_id": project_tag_id, "version": version}


# ---------------------------------------------------------------------------
# Garden ecosystem (docs/adr/0029 P1): typed note links + named lifecycle
# operations + maturity setter. Wraps services/note_links.py.
# ---------------------------------------------------------------------------


@mcp.tool()
async def set_note_maturity(
    token: str,
    org_id: str,
    note_id: str,
    maturity: str,
) -> dict[str, Any]:
    """Manual override of a note's garden lifecycle (seed | growing |
    mature | dormant). Cannot run on a note already transplanted
    (``promoted_at`` set)."""
    async with _tenant(token, org_id) as (s, org, user):
        note = await note_links_svc.set_maturity(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            maturity=maturity,
        )
        return {"note_id": str(note.id), "maturity": note.maturity}


@mcp.tool()
async def link_notes(
    token: str,
    org_id: str,
    parent_note_id: str,
    child_note_id: str,
    kind: str,
) -> dict[str, Any]:
    """Link two notes with a typed relation. The mycelial 4-verb model
    (ADR-0040). Kinds: hypha_of, related, supersedes, contradicts.

    ``hypha_of`` = this note (child) DERIVED / sprouted from the other
    (parent = origin), directional; ``related`` = simply connected,
    UNDIRECTED (the order of parent/child does not matter, the server
    canonicalises it); ``supersedes`` = this note makes the target
    obsolete; ``contradicts`` = this note refutes the target as false.
    ``supersedes`` and ``contradicts`` decay the target toward
    ``dormant`` (the killed idea rots into the deadwood -> humus cycle).
    Importance is computed undirected, so a derived idea can outrank the
    idea it grew from. Humus is a node facet, not a link kind."""
    async with _tenant(token, org_id) as (s, org, user):
        link = await note_links_svc.link_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=uuid.UUID(parent_note_id),
            child_note_id=uuid.UUID(child_note_id),
            kind=kind,
        )
        return {
            "link_id": str(link.id),
            "parent_note_id": str(link.parent_note_id),
            "child_note_id": str(link.child_note_id),
            "kind": link.kind,
        }


@mcp.tool()
async def unlink_notes(
    token: str,
    org_id: str,
    parent_note_id: str,
    child_note_id: str,
    kind: str,
) -> dict[str, Any]:
    """Remove a typed note-to-note link. Returns ``removed`` true/false
    (false when the link did not exist). Both notes must be visible ones:
    NOT_FOUND if either is in the trash or awaiting review, the same rule
    that governs creating the link."""
    async with _tenant(token, org_id) as (s, org, user):
        removed = await note_links_svc.unlink_notes(
            s,
            org_id=org,
            actor_id=user,
            parent_note_id=uuid.UUID(parent_note_id),
            child_note_id=uuid.UUID(child_note_id),
            kind=kind,
        )
        return {"removed": removed}


@mcp.tool()
async def derive_task_from_note(
    token: str,
    org_id: str,
    note_id: str,
    title: str,
    description: str | None = None,
    estimate_effort_h: float | None = None,
) -> dict[str, Any]:
    """Create a task as a fruit of this note. The note stays alive
    (no transplant). A ``derived_from`` link is recorded."""
    async with _tenant(token, org_id) as (s, org, user):
        from decimal import Decimal

        eff = Decimal(str(estimate_effort_h)) if estimate_effort_h is not None else None
        task, link = await note_links_svc.derive_task_from_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            title=title,
            description=description,
            estimate_effort_h=eff,
        )
        return {
            "task_id": str(task.id),
            "link_id": str(link.id),
            "kind": link.kind,
        }


@mcp.tool()
async def promote_note_to_task(
    token: str,
    org_id: str,
    note_id: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Transplant the note to a task. The note becomes read-only
    (``promoted_at`` set); a ``promoted_from`` link records the
    provenance."""
    async with _tenant(token, org_id) as (s, org, user):
        task, link = await note_links_svc.promote_note_to_task(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            title=title,
        )
        return {
            "task_id": str(task.id),
            "link_id": str(link.id),
            "kind": link.kind,
        }


@mcp.tool()
async def start_task_on_note(
    token: str,
    org_id: str,
    task_id: str,
    note_id: str,
) -> dict[str, Any]:
    """Watering: this task is the work of growing the note. Records a
    ``subject`` link."""
    async with _tenant(token, org_id) as (s, org, user):
        link = await note_links_svc.start_task_on_note(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            note_id=uuid.UUID(note_id),
        )
        return {"link_id": str(link.id), "kind": link.kind}


@mcp.tool()
async def record_task_artifact(
    token: str,
    org_id: str,
    task_id: str,
    note_id: str,
) -> dict[str, Any]:
    """The task produced (or updated) this note. Records an
    ``artifact`` link (Proposal A semantics, surfaced explicitly)."""
    async with _tenant(token, org_id) as (s, org, user):
        link = await note_links_svc.record_task_artifact(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            note_id=uuid.UUID(note_id),
        )
        return {"link_id": str(link.id), "kind": link.kind}


@mcp.tool()
async def unlink_note_task(
    token: str,
    org_id: str,
    note_id: str,
    task_id: str,
    kind: str,
) -> dict[str, Any]:
    """Remove a typed note↔task link (``subject``, ``artifact``,
    ``derived_from``). ``promoted_from`` is refused: a transplant cannot
    be undone via unlink. Idempotent: returns ``removed`` false when no
    matching row exists; NOT_FOUND when the note is in the trash or
    awaiting review, as for creating the link."""
    async with _tenant(token, org_id) as (s, org, user):
        removed = await note_links_svc.unlink_note_task(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            task_id=uuid.UUID(task_id),
            kind=kind,
        )
        return {"removed": removed}


# ---------------------------------------------------------------------------
# Garden / graph navigation (read side): traverse the typed links that the
# operations above create. The create/remove primitives existed without a
# read counterpart, so an agent could build the graph but not walk it.
# ---------------------------------------------------------------------------


async def _note_titles(s: AsyncSession, note_ids: list[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    """Batch ``{note_id: title}`` lookup (RLS-scoped) used to enrich the
    link/suggestion traversals with human-readable titles."""
    if not note_ids:
        return {}
    rows = (await s.execute(select(Note.id, Note.title).where(Note.id.in_(note_ids)))).all()
    return {nid: title for nid, title in rows}


def _nn_link(link: Any) -> dict[str, Any]:
    return {
        "link_id": str(link.id),
        "parent_note_id": str(link.parent_note_id),
        "child_note_id": str(link.child_note_id),
        "kind": link.kind,
    }


def _nt_link(link: Any) -> dict[str, Any]:
    return {
        "link_id": str(link.id),
        "note_id": str(link.note_id),
        "task_id": str(link.task_id),
        "kind": link.kind,
    }


@mcp.tool()
async def list_note_links(token: str, org_id: str, note_id: str) -> dict[str, Any]:
    """Traverse a note's typed note↔note links. Returns
    ``{outgoing, incoming}``: ``outgoing`` are links where this note is
    the parent, ``incoming`` are backlinks where it is the child. Each
    link is ``{link_id, parent_note_id, child_note_id, kind}`` with kind
    in hypha_of | related | supersedes | contradicts (ADR-0040). For the
    undirected ``related`` kind, parent/child are the canonical order,
    not a direction. Read counterpart of ``link_notes`` /
    ``unlink_notes``."""
    async with _tenant(token, org_id) as (s, org, _user):
        outgoing, incoming = await note_links_svc.list_note_links(
            s, org_id=org, note_id=uuid.UUID(note_id)
        )
        return {
            "outgoing": [_nn_link(x) for x in outgoing],
            "incoming": [_nn_link(x) for x in incoming],
        }


@mcp.tool()
async def list_note_task_links(
    token: str,
    org_id: str,
    note_id: str | None = None,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    """Traverse typed note↔task links. Pass ``task_id`` to get every note
    linked to a task (the "notes of this task" view), or ``note_id`` to
    get every task linked to a note; at least one is required. Each row
    is ``{link_id, note_id, task_id, kind}`` with kind in subject |
    artifact | derived_from | promoted_from. Read counterpart of
    derive/promote/start_task_on_note/record_task_artifact."""
    async with _tenant(token, org_id) as (s, org, _user):
        links = await note_links_svc.list_note_task_links(
            s,
            org_id=org,
            note_id=uuid.UUID(note_id) if note_id else None,
            task_id=uuid.UUID(task_id) if task_id else None,
        )
        return [_nt_link(x) for x in links]


@mcp.tool()
async def suggest_note_links(
    token: str, org_id: str, note_id: str, k: int = 5
) -> list[dict[str, Any]]:
    """Suggest the top-``k`` candidate notes to link from this note
    (Adamic-Adar over shared tags + personalised-PageRank co-visit, minus
    already-linked pairs; ADR-0029/0033 garden link prediction). Returns
    ``{note_id, title, score, signals, rationale}`` ranked; the agent or
    user confirms a link via ``link_notes`` -- nothing is auto-created.
    Empty when the workspace has <2 notes or there is no signal."""
    async with _tenant(token, org_id) as (s, org, _user):
        suggestions = await link_prediction_svc.suggest_links_for_note(
            s, org_id=org, note_id=uuid.UUID(note_id), k=k
        )
        titles = await _note_titles(s, [sg.note_id for sg in suggestions])
        return [
            {
                "note_id": str(sg.note_id),
                "title": titles.get(sg.note_id),
                "score": round(sg.score, 4),
                "signals": sg.signals,
                "rationale": sg.rationale,
            }
            for sg in suggestions
        ]


@mcp.tool()
async def resolve_prefix(
    token: str,
    org_id: str,
    prefix: str,
    kinds: list[str] | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Resolve a short UUID prefix (the 8-char id in a roadmap note or a
    markdown chip, e.g. ``91cf6aaa``) to the task / note it points at
    (ADR-0038). ``kinds`` defaults to both ['task','note']; tasks rank
    first, then most-recently-updated. Each match is ``{kind, id, title,
    state_name, is_terminal, is_archived, is_deleted}``. Use it to jump
    from a referenced id to the live entity in one round-trip."""
    async with _tenant(token, org_id) as (s, _org, _user):
        matches = await lookup_svc.resolve_prefix(
            s,
            prefix=prefix,
            kinds=tuple(kinds) if kinds else ("task", "note"),
            include_archived=include_archived,
            include_deleted=include_deleted,
            limit=limit,
        )
        return [
            {
                "kind": m.kind,
                "id": str(m.id),
                "title": m.title,
                "state_name": m.state_name,
                "is_terminal": m.is_terminal,
                "is_archived": m.is_archived,
                "is_deleted": m.is_deleted,
            }
            for m in matches
        ]


@mcp.tool()
async def list_task_relations(
    token: str,
    org_id: str,
    task_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List symmetric "related task" links (a pure navigation aid,
    distinct from dependencies: no direction, no cycle rules), newest
    first. Pass ``task_id`` for that task's relations, omit it for the whole
    workspace. Returns the paginated envelope ``{items, next_cursor,
    truncated}``: pass ``limit`` to page, then ``next_cursor`` back as
    ``cursor`` (keyset, no dupes/gaps). Each item is ``{relation_id,
    task_a_id, task_b_id}``; the edge is the same regardless of order."""
    after: tuple[dt.datetime, uuid.UUID] | None = None
    if cursor:
        cc, ci = _decode_cursor(cursor)
        after = (dt.datetime.fromisoformat(cc), uuid.UUID(ci))
    async with _tenant(token, org_id) as (s, org, _user):
        rels = await task_relations_svc.list_relations(
            s,
            org_id=org,
            task_id=uuid.UUID(task_id) if task_id else None,
            limit=(limit + 1) if limit else None,
            after=after,
        )
        items, next_cursor, truncated = _page_envelope(
            rels, limit or 0, key=lambda r: [r.created_at, r.id]
        )
        return {
            "items": [
                {
                    "relation_id": str(r.id),
                    "task_a_id": str(r.task_a_id),
                    "task_b_id": str(r.task_b_id),
                }
                for r in items
            ],
            "next_cursor": next_cursor,
            "truncated": truncated,
        }


@mcp.tool()
async def add_task_relation(token: str, org_id: str, task_id: str, other_id: str) -> dict[str, Any]:
    """Link two tasks as "related" (symmetric, bidirectional; NOT a
    dependency, so no ordering and no cycle check). Idempotent on the
    unordered pair; both tasks must exist in the workspace. Returns the
    canonical relation row."""
    async with _tenant(token, org_id) as (s, org, user):
        rel = await task_relations_svc.add_relation(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            other_id=uuid.UUID(other_id),
        )
        return {
            "relation_id": str(rel.id),
            "task_a_id": str(rel.task_a_id),
            "task_b_id": str(rel.task_b_id),
        }


@mcp.tool()
async def remove_task_relation(token: str, org_id: str, relation_id: str) -> dict[str, Any]:
    """Remove a symmetric task relation by its id (from
    ``list_task_relations``). Idempotent-ish: an unknown id raises
    not_found."""
    async with _tenant(token, org_id) as (s, org, user):
        await task_relations_svc.remove_relation(
            s, org_id=org, actor_id=user, relation_id=uuid.UUID(relation_id)
        )
        return {"relation_id": relation_id, "removed": True}


# ---------------------------------------------------------------------------
# Identity-first task ownership / assignment (docs/adr/0028 D5 follow-up):
# explicit primitives that wrap ``update_task``. Useful for Telegram chat
# ("assegna @marco al task X") and the conversational assistant: a named
# operation reads better than a generic update.
# ---------------------------------------------------------------------------


@mcp.tool()
async def set_task_owner(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    owner_id: str | None = None,
    owner_handle: str | None = None,
) -> dict[str, Any]:
    """Reassign accountability for a task (docs/adr/0028 D2). The owner
    is always a real user, so ``owner_id`` is a *user* id; alternatively
    pass ``owner_handle``, which accepts a bare handle (``angelo``), a
    leading-@ handle (``@angelo``), or the member's login email
    (``angelo@leto.blue``), all resolved under the current org to a user.

    Id-space note (tasks 901f0f9f + 2d3abdc3): ``owner_id`` is a user id;
    ``set_task_assignee`` takes an *identity* id but also accepts a
    member's user id; ``assign_task`` / ``unassign_task`` take user ids.
    When unsure, pass a handle or email and let the server resolve it."""
    async with _tenant(token, org_id) as (s, org, user):
        if owner_id is None and owner_handle:
            from mycelium_core.models.identity import IdentityKind
            from mycelium_core.services import identities as identities_svc

            # Resolve via the shared resolver so ``owner_handle`` accepts a
            # bare handle, a leading-@ handle, or a login email (task
            # 901f0f9f) -- same rules as the assignee path, and it
            # self-heals drifted identity rows. Owner must be a real user.
            ident = await identities_svc.lookup_by_handle(s, org_id=org, handle=owner_handle)
            if ident is None or ident.kind != IdentityKind.user or ident.user_id is None:
                raise NotFoundError(
                    MessageCode.USER_NOT_FOUND,
                    passed=owner_handle,
                    expected="handle, @handle, or member login email (must be a user)",
                    valid_handles=await identities_svc.list_handles(s, org_id=org),
                )
            resolved = ident.user_id
        elif owner_id is not None:
            resolved = uuid.UUID(owner_id)
        else:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        version = await tasks.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            values={"owner_id": resolved},
        )
        return {"task_id": task_id, "owner_id": str(resolved), "version": version}


@mcp.tool()
async def set_task_assignee(
    token: str,
    org_id: str,
    task_id: str,
    expected_version: int,
    assignee_id: str | None = None,
    assignee_handle: str | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    """Set or clear who should work on the task (docs/adr/0028 D2).
    Pass ``assignee_id`` (a uuid into ``identities`` -- or, for DX, a
    member's *user* id, auto-resolved 1:1 to their identity; task
    2d3abdc3) or ``assignee_handle``, which accepts a bare handle
    (``angelo``), a leading-@ handle (``@angelo``), or the member's login
    email (``angelo@leto.blue``); all resolve under the current org. An
    unresolved handle/id returns ``identity.not_found`` with params
    naming what was passed, what was expected, and the valid handles.
    ``clear=True`` unassigns the task; the routing kind then falls back
    to ``task.executor_kind`` (ADR-0028)."""
    async with _tenant(token, org_id) as (s, org, user):
        values: dict[str, Any] = {}
        if clear:
            values["assignee_id"] = None
        elif assignee_id is not None:
            values["assignee_id"] = uuid.UUID(assignee_id)
        elif assignee_handle:
            values["assignee_handle"] = assignee_handle
        else:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        version = await tasks.update_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            expected_version=expected_version,
            values=values,
        )
        return {"task_id": task_id, "version": version, "cleared": clear}


# ---------------------------------------------------------------------------
# Checklist tools: the second tab next to the markdown description in the
# SPA task view. Voice / agent automations dispatch through these instead
# of patching the description's text — every add / remove / check / uncheck
# is an atomic API call against a stable item id.
# ---------------------------------------------------------------------------


def _checklist_item(it: Any) -> dict[str, Any]:
    return {
        "id": str(it.id),
        "task_id": str(it.task_id),
        "text": it.text,
        "done": it.done,
        "position": it.position,
        "done_at": it.done_at.isoformat() if it.done_at else None,
        "done_by": str(it.done_by) if it.done_by else None,
        "created_by": str(it.created_by) if it.created_by else None,
        "version": it.version,
    }


@mcp.tool()
async def list_checklist(token: str, org_id: str, task_id: str) -> list[dict[str, Any]]:
    """List a task's checklist items, ordered by position."""
    async with _tenant(token, org_id) as (s, org, _user):
        rows = await checklist_svc.list_items(s, org_id=org, task_id=uuid.UUID(task_id))
        return [_checklist_item(r) for r in rows]


@mcp.tool()
async def add_checklist_item(
    token: str,
    org_id: str,
    task_id: str,
    text: str,
    position: int | None = None,
) -> dict[str, Any]:
    """Append a checklist item to a task ("alexa, add bread to the
    shopping list"). When ``position`` is omitted the item lands at
    the end; pass an explicit integer to insert at a specific slot."""
    async with _tenant(token, org_id) as (s, org, user):
        item = await checklist_svc.add_item(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            text=text,
            position=position,
        )
        return _checklist_item(item)


@mcp.tool()
async def check_item(
    token: str,
    org_id: str,
    task_id: str,
    item_id: str,
    expected_version: int,
) -> dict[str, Any]:
    """Mark a checklist item as done. Stamps ``done_at`` / ``done_by``."""
    async with _tenant(token, org_id) as (s, org, user):
        item = await checklist_svc.update_item(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            item_id=uuid.UUID(item_id),
            expected_version=expected_version,
            done=True,
        )
        return _checklist_item(item)


@mcp.tool()
async def uncheck_item(
    token: str,
    org_id: str,
    task_id: str,
    item_id: str,
    expected_version: int,
) -> dict[str, Any]:
    """Un-tick a checklist item (clears ``done_at`` / ``done_by``)."""
    async with _tenant(token, org_id) as (s, org, user):
        item = await checklist_svc.update_item(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            item_id=uuid.UUID(item_id),
            expected_version=expected_version,
            done=False,
        )
        return _checklist_item(item)


@mcp.tool()
async def remove_item(
    token: str,
    org_id: str,
    task_id: str,
    item_id: str,
) -> dict[str, Any]:
    """Remove an item from the task's checklist."""
    async with _tenant(token, org_id) as (s, org, user):
        await checklist_svc.delete_item(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            item_id=uuid.UUID(item_id),
        )
        return {"task_id": task_id, "item_id": item_id, "removed": True}


@mcp.tool()
async def clear_done(
    token: str,
    org_id: str,
    task_id: str,
) -> dict[str, Any]:
    """Remove every item already ticked done. Returns the count for
    the UX layer (e.g. "Removed N completed items")."""
    async with _tenant(token, org_id) as (s, org, user):
        removed = await checklist_svc.clear_done(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
        )
        return {"task_id": task_id, "removed": removed}


def _revision_payload(rev: Any) -> dict[str, Any]:
    """Serialize an EntityRevision row into the JSON shape returned by
    every revision-flavored MCP tool. ``snapshot`` is already JSON-
    safe (the service coerces Decimal/date/UUID at write time)."""
    return {
        "id": str(rev.id),
        "entity_kind": rev.entity_kind,
        "entity_id": str(rev.entity_id),
        "snapshot": rev.snapshot or {},
        "changed_fields": list(rev.changed_fields or []),
        "channel": rev.channel,
        "actor_id": str(rev.actor_id) if rev.actor_id else None,
        "actor_kind": rev.actor_kind,
        "actor_subject_id": (str(rev.actor_subject_id) if rev.actor_subject_id else None),
        "edit_session_id": rev.edit_session_id,
        "version_from": rev.version_from,
        "version_to": rev.version_to,
        "edit_count": rev.edit_count,
        "started_at": rev.started_at.isoformat() if rev.started_at else None,
        "last_edit_at": rev.last_edit_at.isoformat() if rev.last_edit_at else None,
        "sealed_at": rev.sealed_at.isoformat() if rev.sealed_at else None,
        "restored_from": str(rev.restored_from) if rev.restored_from else None,
    }


@mcp.tool()
async def list_task_revisions(
    token: str,
    org_id: str,
    task_id: str,
    limit: int = 50,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """Recovery-history timeline for a task, most recent first.
    ``before`` is an ISO-8601 timestamp that filters on
    ``COALESCE(sealed_at, last_edit_at)`` and supports cursor-style
    paging. The open-window revision (sealed_at IS NULL) is included
    at the head of the first page when present."""
    cutoff = dt.datetime.fromisoformat(before) if before else None
    async with _tenant(token, org_id) as (s, org, _user):
        await tasks.get_task(s, org_id=org, task_id=uuid.UUID(task_id), include_deleted=True)
        rows = await revisions_svc.list_revisions(
            s,
            entity_kind=revisions_svc.ENTITY_KIND_TASK,
            entity_id=uuid.UUID(task_id),
            limit=limit,
            before=cutoff,
        )
        return [_revision_payload(r) for r in rows]


@mcp.tool()
async def get_task_revision(
    token: str,
    org_id: str,
    task_id: str,
    revision_id: str,
) -> dict[str, Any]:
    """Single revision lookup; 404 if the id doesn't belong to this
    task (defense in depth on top of RLS)."""
    async with _tenant(token, org_id) as (s, org, _user):
        # Same guard as ``list_task_revisions``: the snapshot carries the
        # whole description, so the read answers to the task's perimeter
        # and not to RLS alone. ``include_deleted`` keeps the bin readable,
        # which is what a restore needs.
        await tasks.get_task(s, org_id=org, task_id=uuid.UUID(task_id), include_deleted=True)
        rev = await revisions_svc.get_revision(
            s,
            revision_id=uuid.UUID(revision_id),
            entity_kind=revisions_svc.ENTITY_KIND_TASK,
            entity_id=uuid.UUID(task_id),
        )
        return _revision_payload(rev)


@mcp.tool()
async def restore_task_revision(
    token: str,
    org_id: str,
    task_id: str,
    revision_id: str,
    expected_version: int,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Revert a task's restorable content fields to a past revision.
    ``fields`` narrows the restore to a subset (omit for the full
    restorable payload). Identity/state columns are filtered out by
    the service. Produces a NEW sealed revision on the ``restore``
    channel; the source revision is not mutated."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await tasks.restore_revision(
            s,
            org_id=org,
            actor_id=user,
            task_id=uuid.UUID(task_id),
            revision_id=uuid.UUID(revision_id),
            expected_version=expected_version,
            fields=fields,
        )
        return {"task_id": task_id, "version": version}


@mcp.tool()
async def list_note_revisions(
    token: str,
    org_id: str,
    note_id: str,
    limit: int = 50,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """Recovery-history timeline for a note, most recent first.
    Symmetric to ``list_task_revisions``."""
    cutoff = dt.datetime.fromisoformat(before) if before else None
    async with _tenant(token, org_id) as (s, org, _user):
        await notes_svc.get_note(s, org_id=org, note_id=uuid.UUID(note_id), include_deleted=True)
        rows = await revisions_svc.list_revisions(
            s,
            entity_kind=revisions_svc.ENTITY_KIND_NOTE,
            entity_id=uuid.UUID(note_id),
            limit=limit,
            before=cutoff,
        )
        return [_revision_payload(r) for r in rows]


@mcp.tool()
async def get_note_revision(
    token: str,
    org_id: str,
    note_id: str,
    revision_id: str,
) -> dict[str, Any]:
    """Single note-revision lookup; 404 if the id doesn't belong to
    this note."""
    async with _tenant(token, org_id) as (s, org, _user):
        # Same guard as ``list_note_revisions``: a revision snapshot carries
        # the note's whole body, so it must not be readable for a note the
        # gate hides. ``include_deleted`` keeps the bin readable, which is
        # what restoring from the trash needs (task a186c989).
        await notes_svc.get_note(s, org_id=org, note_id=uuid.UUID(note_id), include_deleted=True)
        rev = await revisions_svc.get_revision(
            s,
            revision_id=uuid.UUID(revision_id),
            entity_kind=revisions_svc.ENTITY_KIND_NOTE,
            entity_id=uuid.UUID(note_id),
        )
        return _revision_payload(rev)


@mcp.tool()
async def restore_note_revision(
    token: str,
    org_id: str,
    note_id: str,
    revision_id: str,
    expected_version: int,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Revert a note's ``title`` / ``transcript`` to a past revision.
    ``fields`` narrows the restore to one of the two; omitting it
    restores both. Lifecycle / status / linkage are intentionally
    not restorable."""
    async with _tenant(token, org_id) as (s, org, user):
        version = await notes_svc.restore_revision(
            s,
            org_id=org,
            actor_id=user,
            note_id=uuid.UUID(note_id),
            revision_id=uuid.UUID(revision_id),
            expected_version=expected_version,
            fields=fields,
        )
        return {"note_id": note_id, "version": version}
