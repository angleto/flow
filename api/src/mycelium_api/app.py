"""FastAPI app factory. Maps domain errors to HTTP codes and renders
messages via the i18n catalog (docs/adr/0017). No business logic here
(docs/adr/0001)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html
from starlette.responses import HTMLResponse, JSONResponse, Response

from mycelium_api import correlation
from mycelium_api.deps import enforce_route_scope
from mycelium_api.routers import (
    actors,
    admin_sdi,
    admin_users,
    advisory,
    agent_runs,
    agent_self,
    agent_tokens,
    ai_assistants,
    attachments,
    auth,
    billing,
    budgets,
    buildinfo,
    calendars,
    capabilities,
    connector_webhooks,
    dependencies,
    dispatch,
    email,
    embedder_provider,
    executors,
    export,
    garden,
    invoices,
    issuer_api_keys,
    llm_provider,
    lookup,
    memory,
    memory_channels,
    mfa,
    notes,
    notifications,
    oauth,
    oauth_google,
    payment_connectors,
    public_invoices,
    received_invoices,
    schedule,
    search,
    tags,
    task_relations,
    tasks,
    telegram,
    time_tracking,
    webhook_endpoints,
    workflows,
    workspace,
)
from mycelium_api.routers import (
    annotations as annotations_router,
)
from mycelium_core.ai_providers import set_llm_override
from mycelium_core.config import Settings, get_settings
from mycelium_core.errors import (
    AuthError,
    ConflictError,
    DomainError,
    ForbiddenError,
    LockedError,
    NotFoundError,
    QuotaExceededError,
    UnprocessableError,
    jsonable_params,
)
from mycelium_core.i18n import DEFAULT_LOCALE, render
from mycelium_core.llm_ollama import OllamaLLM
from mycelium_core.notification_channel import set_sender_override
from mycelium_core.readiness import not_ready_because
from mycelium_core.schema_revision import verify_schema_revision
from mycelium_core.services.mailer import build_system_mailer, set_mailer
from mycelium_core.services.notification_sender import build_notification_sender

_STATUS: dict[type[DomainError], int] = {
    AuthError: 401,
    ForbiddenError: 403,
    NotFoundError: 404,
    ConflictError: 409,
    LockedError: 423,
    UnprocessableError: 422,
    QuotaExceededError: 429,
    DomainError: 400,
}


def _locale(request: Request) -> str:
    raw = request.headers.get("accept-language", DEFAULT_LOCALE)
    return raw.split(",")[0].split("-")[0].strip().lower() or DEFAULT_LOCALE


def _error_body(request: Request, exc: DomainError) -> dict[str, Any]:
    """The envelope every surface reads: a stable ``code`` to branch on,
    localized ``detail`` for a person, the ``params`` that name what was
    violated, and the identifier that ties this answer to the log line.

    ``params`` is the half that used to be dropped here. The domain
    already computes it -- a stale-version conflict carries the version
    the caller should have presented -- and the MCP adapter has always
    emitted it, so a client on this side had to issue a blind extra read
    to learn what the server already knew, and could livelock doing it
    when someone was writing continuously. The params are coerced to
    JSON-safe scalars by the same helper the other adapter uses, so the
    two envelopes cannot drift.

    Nothing internal goes in: ``params`` holds domain values (an id, a
    version, a limit), never a schema name, a path or a stack."""
    body: dict[str, Any] = {
        "code": exc.code.value,
        "detail": render(exc.code, _locale(request), **exc.params),
        "correlation_id": correlation.current(request),
    }
    params = jsonable_params(exc.params)
    if params:
        body["params"] = params
    return body


def _make_handler(
    status: int,
) -> Callable[[Request, Exception], Awaitable[Response]]:
    async def handler(request: Request, exc: Exception) -> Response:
        correlation_id = correlation.current(request)
        if isinstance(exc, DomainError):
            body = _error_body(request, exc)
        else:
            body = {
                "code": "internal",
                "detail": "internal error",
                "correlation_id": correlation_id,
            }
        return JSONResponse(
            status_code=status,
            content=body,
            headers={correlation.HEADER: correlation_id},
        )

    return handler


async def _unhandled(request: Request, exc: Exception) -> Response:
    """The fault that reached here is ours, and the caller gets the same
    envelope every other failure uses -- one error model, or a client
    ends up with a branch per surface.

    The two halves of the rule are visible in one function: the internal
    detail goes to the LOG, under the identifier the caller is shown, and
    nothing about the exception goes into the response. Before this
    existed the caller got Starlette's bare "Internal Server Error" with
    nothing to quote, which is the failure that makes a user describe an
    error instead of reporting it."""
    correlation_id = correlation.current(request)
    logging.getLogger(__name__).exception(
        "unhandled error correlation_id=%s method=%s path=%s",
        correlation_id,
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal",
            "detail": "internal error",
            "correlation_id": correlation_id,
        },
        headers={correlation.HEADER: correlation_id},
    )


def _wire_local_llm_override(settings: Settings) -> bool:
    """Install the bundled local Ollama provider as the rank-0 env fallback
    when configured, so narration (T3) and any in-process LLM use run on the
    in-cluster model -- the API-process counterpart of the worker startup.
    Returns whether an override was installed. NOT a hosted-provider
    override: per-org OpenAI/Anthropic selection is owned by
    ``resolve_llm`` (task 8afda4e7). A later ``set_llm_override`` (e.g. CI's
    scripted fake) still wins; unset settings leave the ``LocalLLM`` stub."""
    if settings.ollama_url and settings.open_model:
        url = settings.ollama_url
        model = settings.open_model
        set_llm_override(lambda: OllamaLLM(base_url=url, model=model))
        return True
    return False


def _make_lifespan(mcp_app: Any) -> Any:
    """Parent lifespan that ALSO drives the mounted MCP sub-app's
    lifespan. FastAPI/Starlette do NOT run a mounted app's lifespan
    automatically, so the MCP ``StreamableHTTPSessionManager.run()``
    (entered by ``streamable_http_app()``'s own lifespan) would never
    start — every POST /mcp then 500s with "Task group is not
    initialized. Make sure to use run()". We enter the sub-app's
    lifespan context here so its session-manager task group is live
    for the process lifetime.

    Lifespan events fire only under a real ASGI server (uvicorn) or a
    ``with TestClient(...)`` block; the unit suite drives the app via
    bare ``httpx.ASGITransport`` which doesn't emit lifespan, so the
    MCP task group simply isn't started in tests (no MCP HTTP test
    exercises it in-process). SMTP wiring stays as before."""

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        import asyncio
        import logging

        from mycelium_mcp.gateway import prewarm as prewarm_mcp_gateway

        settings = get_settings()
        # Before anything is wired and before uvicorn serves: refuse to
        # come up against a schema this build does not expect, naming the
        # revision that is missing. A deploy that skipped the migrate step
        # otherwise starts, serves, and fails on whichever request first
        # touches the changed table.
        await verify_schema_revision()
        # Local Ollama provider as the rank-0 fallback (task T5), the
        # API-process counterpart of the worker. Cleared on shutdown below.
        _wire_local_llm_override(settings)
        if settings.smtp_configured:
            set_mailer(build_system_mailer(settings))
        # Install the concrete notification sender (telegram over an email
        # fallback that uses the mailer wired just above). Without this,
        # get_sender() returns DefaultSender and EVERY reminder dispatch
        # fails. Unconditional and safe: in a dev/OSS deploy the email half
        # falls back to LogMailer and the telegram half fails per-item
        # until the bot is configured. Must run after set_mailer.
        _sender = build_notification_sender()
        set_sender_override(lambda: _sender)
        # Drive the mounted MCP app's lifespan (starts the streamable
        # HTTP session manager's task group).
        async with mcp_app.router.lifespan_context(mcp_app):
            # Warm the embedding index for the MCP search/describe/execute
            # gateway off the request path. Without this, the first
            # ``search_tools`` call after a roll pays the model load +
            # ~140-text encode inline (10-20s worst case), long enough
            # that the MCP client reports "connection lost". Fired as a
            # background task so a missing optional dep or a slow load
            # never blocks app boot or the readiness probe.
            async def _prewarm() -> None:
                try:
                    await prewarm_mcp_gateway()
                except Exception:  # pragma: no cover - defensive
                    logging.getLogger(__name__).exception(
                        "mcp gateway prewarm failed; first search_tools will pay the cost"
                    )

            # Keep a reference so the task is not garbage-collected
            # while it runs (asyncio holds only weakrefs to tasks).
            prewarm_task = asyncio.create_task(_prewarm())
            try:
                yield
            finally:
                prewarm_task.cancel()
                # Clear the local LLM override on shutdown (hygiene the
                # worker omits): keeps set_llm_override precedence + clean
                # teardown predictable across reloads and in CI.
                set_llm_override(None)

    return _lifespan


def create_app() -> FastAPI:
    from mycelium_mcp.server_http import make_mcp_app

    mcp_app = make_mcp_app()
    app = FastAPI(
        title="Mycelium API",
        version="0.0.0",
        lifespan=_make_lifespan(mcp_app),
        # App-level dependency: runs before every route's own dependencies, so
        # a scoped assistant is confined to the routes its scope covers on THIS
        # surface too. Without it the MCP per-tool gate is bypassable by simply
        # not speaking MCP -- same token, ungated transport (task c19f2f63,
        # enabler B). No-op for human sessions, bare tokens and capability
        # tokens.
        dependencies=[Depends(enforce_route_scope)],
    )

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        # Liveness and startup only: answers from the process and touches
        # nothing, because the remedy for a failing liveness probe is a
        # restart and a restart cannot fix a dependency. Readiness is
        # ``/readyz`` and is a different question.
        return {"status": "ok"}

    @app.get("/readyz", tags=["meta"])
    async def readyz() -> Response:
        # Readiness: can this process serve right now. 503 takes the pod
        # out of rotation without killing it, which is the correct remedy
        # for a dependency that is down.
        dependency = await not_ready_because()
        if dependency is None:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not-ready", "dependency": dependency}, status_code=503)

    @app.get("/apidocs", include_in_schema=False)
    async def apidocs() -> HTMLResponse:
        # Human-facing REST API reference (ReDoc over the auto-generated
        # OpenAPI -- zero maintenance, always in sync with the routes). A
        # RELATIVE ``openapi_url`` so the page works behind the ``/api`` reverse
        # proxy (which strips the prefix) without ``root_path`` (which would
        # perturb the OAuth metadata URLs): at ``/api/apidocs`` the browser
        # resolves ``openapi.json`` to ``/api/openapi.json`` -> backend
        # ``/openapi.json``. Linked from the SPA "About this build" panel.
        return get_redoc_html(openapi_url="openapi.json", title="Mycelium API")

    for exc_type, status in _STATUS.items():
        app.add_exception_handler(exc_type, _make_handler(status))
    app.add_exception_handler(Exception, _unhandled)

    # Outermost of the user middleware, so the identifier exists before
    # anything else can fail and every response carries it back. Raw
    # ASGI: a BaseHTTPMiddleware would buffer the streaming endpoints.
    app.add_middleware(correlation.CorrelationMiddleware)

    # Cross-origin SPA (production serves the SPA and the API on
    # different hosts: mycelium.xeno.garden vs api.mycelium.xeno.garden). Enabled
    # only when origins are configured (MYCELIUM_CORS_ORIGINS).
    origins = get_settings().cors_origin_list
    if origins:
        if "*" in origins:
            # A wildcard origin WITH credentials is spec-violating and would also
            # cover the machine-to-machine /api/v1 and /mcp surfaces. Fail fast at
            # startup rather than ship an exploitable CORS config (task 19b7e874).
            raise RuntimeError(
                "MYCELIUM_CORS_ORIGINS must list explicit origins, not '*' "
                "(credentialled CORS cannot use a wildcard)."
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            # Response headers are not readable cross-origin unless they
            # are named here. Without this the SPA can see the identifier
            # in a same-origin deploy and not in a split one, which is the
            # deployment this block exists for.
            expose_headers=[correlation.HEADER],
        )

    app.include_router(auth.router)
    app.include_router(admin_users.router)
    app.include_router(admin_sdi.router)
    app.include_router(mfa.router)
    app.include_router(workspace.router)
    app.include_router(tags.router)
    app.include_router(tasks.router)
    app.include_router(annotations_router.router)
    app.include_router(workflows.router)
    app.include_router(dependencies.router)
    app.include_router(task_relations.router)
    app.include_router(calendars.router)
    app.include_router(schedule.router)
    app.include_router(executors.router)
    app.include_router(agent_runs.router)
    app.include_router(agent_self.router)
    app.include_router(agent_tokens.router)
    app.include_router(ai_assistants.router)
    app.include_router(actors.router)
    # OAuth shim (#48). The /api-prefixed router carries the
    # authorize + token endpoints; the well-known one mounts at the
    # host root so MCP clients fetch metadata from
    # /.well-known/oauth-* (RFC 8414 / 9728). The latter is included
    # on the SAME FastAPI app — see the api_prefix("") below for
    # why nginx must proxy /.well-known/oauth-* to the backend.
    app.include_router(oauth.oauth_router)
    app.include_router(oauth.well_known_router)
    app.include_router(dispatch.router)
    app.include_router(time_tracking.router)
    app.include_router(budgets.router)
    app.include_router(advisory.router)
    app.include_router(email.router)
    app.include_router(billing.router)
    app.include_router(llm_provider.router)
    app.include_router(embedder_provider.router)
    app.include_router(memory.router)
    app.include_router(memory_channels.router)
    app.include_router(search.router)
    app.include_router(lookup.router)
    app.include_router(notes.router)
    app.include_router(garden.router)
    app.include_router(attachments.router)
    app.include_router(capabilities.router)
    app.include_router(invoices.router)
    app.include_router(issuer_api_keys.router)
    app.include_router(webhook_endpoints.router)
    app.include_router(payment_connectors.router)
    app.include_router(connector_webhooks.router)
    app.include_router(public_invoices.router)
    app.include_router(received_invoices.router)
    app.include_router(notifications.router)
    app.include_router(oauth_google.router)
    app.include_router(telegram.router)
    app.include_router(buildinfo.router)
    app.include_router(export.router)

    # MCP streamable-http transport — same process, same DB, same
    # ingress. Authenticated by Authorization: Bearer mycelium_at_…; the
    # middleware in mycelium_mcp.server_http resolves the principal via
    # the SECURITY DEFINER authenticate_agent_token (migration 0059)
    # and publishes it into a ContextVar that ``_tenant`` inside every
    # @mcp.tool short-circuits on. URL becomes /mcp at the public
    # ingress (mycelium.xeno.garden/mcp); behind nginx the SPA's same-origin
    # routing already covers /api/, the deploy adds a /mcp/ proxy.
    # Mount the SAME mcp_app instance whose lifespan _make_lifespan
    # drives — mounting a fresh one would leave the session manager
    # uninitialised again.
    app.mount("/mcp", mcp_app)
    return app
