"""Database access and tenant context for RLS.

Multi-tenant isolation is a primary defense (docs/adr/0002, 0007):
every application transaction sets session GUCs that the RLS policies
read via ``current_setting(...)``. Queries cannot see another
org/project's data even if they forget the predicate.

GUCs used:
- ``app.current_org``     uuid of the current organization
- ``app.current_user``    uuid of the authenticated user
- ``app.current_project`` uuid of the current project (memory), optional
"""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.util import await_only

from mycelium_core.config import get_settings

if TYPE_CHECKING:  # import-time cost + no runtime need: typing only
    from sqlalchemy.engine.interfaces import DBAPIConnection, Dialect
    from sqlalchemy.pool import ConnectionPoolEntry

_log = logging.getLogger("mycelium.db")

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


# SQLSTATEs whose meaning is "THIS connection attempt lost the race with the
# network or with a server that is a moment away from being ready" -- i.e.
# transient by construction AND safe to repeat, because nothing happened that
# a second attempt could duplicate. Deliberately a short ALLOW-list: anything
# not listed surfaces on the first attempt.
#
# Excluded on purpose, because these are the tempting ones:
#
# - 53300 too_many_connections and 08004
#   sqlserver_rejected_establishment_of_sqlconnection are ADMISSION decisions:
#   the server (or a pooler in front of it) is at its limit and said so.
#   Retrying multiplies our connect rate against a server that is by
#   definition already saturated, which is the opposite of what helps. The
#   answer to 53300 is the connection budget in ``get_engine``, not a retry.
# - 08P01 protocol_violation is a wire-level disagreement (driver or pooler
#   bug, a mangled stream). It is deterministic: the retry reproduces it.
# - 08007 transaction_resolution_unknown is about a transaction whose outcome
#   is unknown, not about a connection that failed to open. Blind-retrying an
#   unknown-outcome operation is not safe by construction.
# - 28000/28P01 (invalid authorization / password), 3D000 (unknown database)
#   and 42501 (insufficient privilege) are permanent configuration errors;
#   for the auth codes retrying is actively harmful, since repeated failures
#   are what trip account lockout / fail2ban.
_RETRYABLE_CONNECT_SQLSTATES = frozenset(
    {
        "08000",  # connection_exception (class root: generic connection loss)
        "08001",  # sqlclient_unable_to_establish_sqlconnection
        "08003",  # connection_does_not_exist (the server hung up on us)
        "08006",  # connection_failure
        "57P03",  # cannot_connect_now (starting up / in recovery / failover)
    }
)


def _is_transient_connect_error(exc: BaseException) -> bool:
    """True iff ``exc`` is a failure to ESTABLISH a connection that a retry
    can plausibly fix.

    Narrow on purpose. This classifier only ever sees exceptions raised by
    ``dialect.connect()``, so no query error can reach it; what it must still
    exclude is the permanent-configuration family (bad password, wrong
    database, untrusted certificate), which a retry cannot fix.

    - ``OSError`` covers the whole socket/TLS-handshake family:
      ``ConnectionResetError`` (a reset handshake),
      ``ConnectionRefusedError``, ``socket.gaierror`` (transient DNS),
      ``TimeoutError`` and ``ssl.SSLError``. At the connect boundary an
      OSError is by definition a network-level failure.
    - ``ssl.SSLCertVerificationError`` is carved back OUT: it is an OSError
      subclass, but a certificate that does not verify now will not verify
      100 ms later. Surface it immediately instead of hiding it behind three
      identical failures.
    - Driver errors are matched on ``sqlstate`` (asyncpg and psycopg both
      expose it) against the allow-list above, so no driver import is needed
      here and the classifier stays dialect-agnostic.
    """
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, OSError):
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate in _RETRYABLE_CONNECT_SQLSTATES


def _connect_with_retry(
    dialect: Dialect,
    conn_rec: ConnectionPoolEntry | None,
    cargs: tuple[Any, ...],
    cparams: dict[str, Any],
) -> DBAPIConnection:
    """``do_connect`` hook: retry connection CREATION with bounded backoff.

    SQLAlchemy retries nothing here. When a managed Postgres reached over the
    network resets the socket mid-TLS-handshake, that ``ConnectionResetError``
    propagates out of ``pool._create_connection`` and becomes a 500 on a
    request that never even ran a query. ``pool_pre_ping`` does NOT cover
    this: it validates a REUSED connection before handing it out, so it is a
    no-op on the creation path.

    Scope, stated plainly: this absorbs a SINGLE-EVENT failure -- one dropped
    handshake, a server a moment away from accepting connections again. It is
    not a circuit breaker and not an outage cure. If the server or the network
    path to it is refusing connections, every attempt fails, the bound below
    is exhausted (at worst ``db_connect_max_attempts`` x the per-attempt
    connect timeout) and the original driver exception surfaces unchanged.

    Why ``do_connect`` and not ``async_creator=``: this hook is handed the
    ``cargs``/``cparams`` SQLAlchemy already derived from the URL -- host,
    port, user, password, database, the ``ssl`` option, plus whatever
    ``connect_args`` added (our connect timeout). Delegating to
    ``dialect.connect(*cargs, **cparams)`` reproduces the stock connect
    EXACTLY: there is no DSN to re-parse and no way for a URL option to be
    silently dropped on the retry path. ``async_creator=`` would hand us an
    empty canvas and force us to rebuild the DSN by hand, which is how
    credentials and TLS options get lost.

    Greenlet semantics: ``do_connect`` runs inside SQLAlchemy's greenlet
    bridge (the asyncpg DBAPI adapter itself calls ``await_only`` under us),
    so the backoff sleep MUST be ``await_only(asyncio.sleep(...))`` -- a
    ``time.sleep`` here would block the event loop of every other in-flight
    request, i.e. turn one slow connect into a fleet-wide stall.

    Bounded in total wall clock: at most ``db_connect_max_attempts`` attempts,
    each capped by the asyncpg-level ``db_connect_timeout_seconds``, plus a
    full-jitter exponential backoff off ``db_connect_retry_base_seconds``. A
    request cannot hang here; it fails with the original driver exception.
    """
    settings = get_settings()
    attempts = max(1, settings.db_connect_max_attempts)
    base = max(0.0, settings.db_connect_retry_base_seconds)
    for attempt in range(1, attempts + 1):
        try:
            return dialect.connect(*cargs, **cparams)
        except Exception as exc:
            if attempt >= attempts or not _is_transient_connect_error(exc):
                raise
            # Full jitter (AWS's "exponential backoff and jitter"): when a
            # blip takes out N in-flight connects at the same instant, a
            # deterministic backoff would have all N retry at the same instant
            # too -- a self-inflicted thundering herd on top of the blip.
            jitter_ceiling = base * 2 ** (attempt - 1)
            delay = random.uniform(0.0, jitter_ceiling)  # noqa: S311 (jitter, not crypto)
            _log.warning(
                "db connect attempt %d/%d failed (%s: %s); retrying in %.3fs",
                attempt,
                attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            await_only(asyncio.sleep(delay))
    raise AssertionError("unreachable: the loop above always returns or raises")


def get_engine() -> AsyncEngine:
    """The process-wide async engine, with an EXPLICITLY sized pool.

    Explicit sizing makes this app's connection footprint on a managed
    instance shared with another project a KNOWN number. The ceiling below is
    10 + 5 = 15 per process, the same total SQLAlchemy's implicit defaults
    already gave us (5 + 10) and that production has run on; what changes is
    the SPLIT. Ten of the fifteen are now persistent instead of five, so a
    burst is served from already-open connections rather than from overflow
    connections, which are opened on the request's critical path and then
    discarded on return. Do not shrink the total below the worker's structural
    concurrency (~12 overlapping sweep loops, one session each), or normal
    operation turns into checkout timeouts.

    Connection budget. The premise, to re-derive if any of it changes: the
    instance is a Scaleway Managed Postgres ``db-dev-s`` with
    ``max_connections = 100``, shared with a co-tenant project, and Postgres
    keeps ``superuser_reserved_connections`` (3) aside. THREE processes hold
    one of these pools, one per deployment, each 1 replica x 1 process: the
    backend (uvicorn, no ``--workers`` in docker/backend.Dockerfile, with the
    MCP server mounted into the same app), the worker
    (``python -m mycelium_worker.main``) and sdi-inbound (uvicorn; its handler
    calls ``ingest_notification`` -> ``tenant_session``, so it holds a pool
    like the others).

        per-process ceiling   = pool_size + max_overflow = 10 + 5 = 15
        steady-state ceiling  = 3 pools x 15             =         45
        rolling-update ceiling= 6 pools x 15             =         90

    Those two totals are WORST CASES, not expectations: a QueuePool opens
    connections on demand and only reaches its ceiling under sustained
    concurrency, which the backend (single-digit requests per minute) and
    sdi-inbound (event-driven) never approach. The number to watch is the
    worker's, since it is the one with a dozen concurrent loops. The
    rolling-update line assumes every pool of both the old and the new pod
    saturated at the same instant, which is why it is a bound and not a plan:
    if it ever becomes real, lower MYCELIUM_DB_POOL_SIZE for the roll rather
    than discovering it as 53300.

    The invariant to preserve when scaling out: summed over the deployments,
    ``replicas x processes_per_replica x (pool_size + max_overflow) x 2`` must
    stay under ``max_connections - reserved - co-tenant share``. Adding a
    replica or a uvicorn ``--workers`` value WITHOUT lowering
    MYCELIUM_DB_POOL_SIZE breaks it, and the failure mode is 53300
    too_many_connections for everyone on the instance, us and the co-tenant
    alike. The knobs are per-deployment env vars: the worker and sdi-inbound
    do not need the backend's ceiling, so trimming theirs is the first lever
    when the instance gets tight.

    What this does and does not address. Sizing bounds the footprint and lets
    a burst be served by connections that are already open; the ``do_connect``
    retry absorbs a single-event connect failure; ``pool_pre_ping`` heals a
    connection killed while idle, one checkout at a time, which is also why
    there is no age-based ``pool_recycle`` window by default (see config.py).
    NONE of that is a defence against a database that stops admitting new
    connections at all -- a firewall/ACL change, a hard outage: every attempt
    then fails, the retry exhausts its bound, and the error surfaces. That
    failure mode is diagnosed on the server side, not survived here.
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            # -1 (never) by default: pool connections tend to be opened
            # together, so their ages are correlated and an age window expires
            # the whole pool at once. config.py has the full argument.
            pool_recycle=settings.db_pool_recycle_seconds,
            # LIFO: hand back the most recently used connection, so reuse
            # concentrates on a small hot set instead of round-robining over
            # every connection in the pool. Under light traffic the cold tail
            # is simply not handed out; when it eventually is, pool_pre_ping
            # replaces it if it died meanwhile.
            pool_use_lifo=True,
            # Bounds the handshake itself; asyncpg's own default is 60 s,
            # which is unbounded enough to hang a request. See config.py.
            connect_args={"timeout": settings.db_connect_timeout_seconds},
            future=True,
        )
        # Registered on ``sync_engine``: ``do_connect`` is a DialectEvents hook
        # and AsyncEngine deliberately refuses sync-level event registration.
        event.listen(_engine.sync_engine, "do_connect", _connect_with_retry)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


# Closed set of actor kinds; mirrored at the DB level by the
# ``ck_activity_log_actor_kind`` CHECK constraint (baseline 0001, widened in
# migration 0077 to add ``issuer_api_key``). Kept as a literal type rather than
# an Enum so it remains a plain string at the GUC boundary.
ActorKind = str  # human_direct|human_api|human_telegram|agent_run|mcp_token|system|issuer_api_key


@asynccontextmanager
async def index_maintenance_scope(session: AsyncSession) -> AsyncIterator[None]:
    """Run search-index maintenance without the caller's PROJECT perimeter.

    ``p_memory_blobs`` carries a project term that ``p_blob_sources``,
    ``p_notes``, ``p_note_part`` and ``p_tasks`` do not (baseline 0001), so
    a request that arms ``app.current_project`` can read and write the
    source row while the blob derived from it is invisible. The index is
    not in the caller's perimeter: a task blob is deliberately
    ``project_id`` NULL so the hit is org-wide, and a note blob takes the
    project of its NOTE, not of the request. Maintaining it under the
    request's filter was reading the wrong perimeter, and silently: the
    blob DELETE behind ``index_scope='none'`` matched nothing while the
    opt-out looked applied, and on the note side the provenance DELETE
    (org-scoped) succeeded while the blob DELETE did not, leaving a blob
    with the note's text and no provenance -- a row no erase-by-provenance
    path can reach again. Reproduced on both before this existed.

    The previous value comes from the stash ``tenant_session`` writes, not
    from a probe query: this runs on the commit path of every transaction,
    and a statement issued here would also break the callers that reach the
    flush with an aborted transaction and no pending work, which today pass
    through it without emitting SQL.

    What this widens: for the length of the flush the org term still binds,
    and it is the only one that ever governed these rows. What it does not
    cover: the inline ``delete_part_index_now`` helpers, which have the same
    shape and the same pre-existing exposure on a project-scoped request.
    """
    stashed = session.info.get("tenant_gucs")
    prev = stashed[2] if stashed else ""
    if not prev:
        yield
        return
    await session.execute(text("SELECT set_config('app.current_project', '', true)"))
    yield
    # Restored on the clean path only, deliberately: no ``finally``. A
    # statement issued on the failure path lands on a transaction that is
    # already going away, and it does not fail quietly. If the body died in
    # an ORM flush the Session is deactivated, so the restore raises
    # ``PendingRollbackError`` -- a subclass of ``InvalidRequestError``,
    # which is exactly what both flush functions catch and downgrade to a
    # warning: the original error is discarded, the flush returns normally
    # and the caller is told a rolled-back transaction succeeded. Measured:
    # with a project armed an in-window FK violation exited ``tenant_session``
    # with no exception and nothing committed, while the same transaction
    # without a project raised. Nothing is lost by skipping the restore: the
    # blank is ``is_local``, so it dies with the rollback that an in-window
    # failure makes unavoidable, and ``tenant_rollback`` re-arms from the
    # same stash this reads. What it does not cover: an ``InvalidRequestError``
    # raised while the transaction is still healthy leaves the GUC blank for
    # the rest of it; both callers commit or re-arm immediately after, and
    # neither reads anything project-scoped in between.
    await session.execute(
        text("SELECT set_config('app.current_project', :prj, true)"), {"prj": prev}
    )


@asynccontextmanager
async def tenant_session(
    org_id: str,
    user_id: str,
    project_id: str | None = None,
    *,
    actor_kind: ActorKind = "human_direct",
    actor_subject_id: str | None = None,
) -> AsyncIterator[AsyncSession]:
    """A transactional session with the tenant GUCs set for RLS.

    The GUCs are ``set_config(..., is_local => true)``: they apply only
    to the current transaction, no leak across pool connections.

    ``actor_kind`` and ``actor_subject_id`` propagate the **type of
    caller** down to ``audit.log``, which reads them via
    ``current_setting`` and persists them on ``activity_log``. The
    default ``human_direct`` keeps every existing call site
    backward-compatible (the test suite and the SPA's REST paths still
    work without changes).
    """
    # Imported lazily so the services layer (which depends on the models
    # registered at import time) doesn't pull db.py into a circular cycle.
    # The side-effect of the import is what matters: it registers the
    # SQLAlchemy mapper-level event listeners that track task/checklist
    # and note-part mutations into ``session.info``.
    from mycelium_core.services.note_search import flush_note_search_dirty
    from mycelium_core.services.task_search import flush_task_search_dirty

    sm = get_sessionmaker()
    async with sm() as session:
        # Explicit begin/commit instead of the ``session.begin()`` context
        # manager: SQLAlchemy forbids further statements after a mid-CM
        # commit, and the two-phase transmit (ADR-0046) must be able to
        # ``tenant_checkpoint`` -- commit the prepared fiscal identifiers and
        # keep working on the same session. Semantics are unchanged for every
        # other caller: begin-on-first-statement (autobegin), commit on clean
        # exit, rollback on exception.
        try:
            # Stashed so ``tenant_rollback`` can re-arm after an ABORTED
            # transaction (an aborted tx cannot be queried for its GUCs).
            session.info["tenant_gucs"] = (
                org_id,
                user_id,
                project_id or "",
                actor_kind,
                actor_subject_id or "",
                "",
            )
            await session.execute(
                text(
                    "SELECT set_config('app.current_org', :org, true),"
                    "       set_config('app.current_user', :usr, true),"
                    "       set_config('app.current_project', :prj, true),"
                    "       set_config('app.current_actor_kind', :ak, true),"
                    "       set_config('app.current_actor_subject', :asubj, true)"
                ),
                {
                    "org": org_id,
                    "usr": user_id,
                    "prj": project_id or "",
                    "ak": actor_kind,
                    "asubj": actor_subject_id or "",
                },
            )
            yield session
            # Resync task-search index for any task/checklist row that
            # mutated inside this transaction. Sits before the commit so
            # the blob upsert is atomic with the source mutation (FTS is
            # visible the instant the commit lands; the embedding vector
            # is best-effort within a 2 s timeout and the backfill worker
            # fills the rest).
            await flush_task_search_dirty(session)
            # Same chokepoint for the note index: one blob per note PART,
            # resync'd atomically with the part mutation.
            await flush_note_search_dirty(session)
            trans = session.sync_session.get_transaction()
            if trans is not None and not trans.is_active:
                # A flush error contained by a savepoint pattern (e.g. the
                # EXCLUDE-overlap 409s) deactivates the outer transaction
                # while the request still renders a handled error response.
                # The old ``session.begin()`` CM rolled this back silently
                # on exit; keep that contract instead of raising
                # PendingRollbackError from an unconditional commit.
                await session.rollback()
            else:
                await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def _capture_gucs(session: AsyncSession) -> tuple[str, ...]:
    row = (
        await session.execute(
            text(
                "SELECT current_setting('app.current_org', true),"
                "       current_setting('app.current_user', true),"
                "       current_setting('app.current_project', true),"
                "       current_setting('app.current_actor_kind', true),"
                "       current_setting('app.current_actor_subject', true),"
                "       current_setting('app.current_role', true)"
            )
        )
    ).one()
    return tuple(v or "" for v in row)


async def _rearm_gucs(session: AsyncSession, captured: tuple[str, ...]) -> None:
    await session.execute(
        text(
            "SELECT set_config('app.current_org', :org, true),"
            "       set_config('app.current_user', :usr, true),"
            "       set_config('app.current_project', :prj, true),"
            "       set_config('app.current_actor_kind', :ak, true),"
            "       set_config('app.current_actor_subject', :asubj, true),"
            "       set_config('app.current_role', :role, true)"
        ),
        {
            "org": captured[0],
            "usr": captured[1],
            "prj": captured[2],
            "ak": captured[3],
            "asubj": captured[4],
            "role": captured[5],
        },
    )


async def tenant_checkpoint(session: AsyncSession) -> None:
    """Commit the current tenant transaction and re-arm the GUCs in a fresh one.

    The durability primitive of the two-phase transmit (ADR-0046): phase 1
    calls this to make the fiscal identifiers (numero, ProgressivoInvio,
    NomeFile, frozen XML) durable BEFORE the SdI dispatch, releasing the row
    locks; the caller keeps using the same session afterwards.

    The GUCs are ``set_config(..., is_local => true)`` = transaction-local,
    so they die with the commit; this helper captures every tenant GUC
    (including ``app.current_role``, pinned to ``member`` by the public-API
    issuer-key path) and restores it as the FIRST statement of the next
    transaction -- RLS never sees a statement without its tenant context.

    The task/note search-dirty flush hooks run before the commit, exactly as
    at the end of ``tenant_session``, so an FTS blob never lags the source
    row it indexes. CAVEAT: only the ``app.current_*`` GUC set is captured;
    do not checkpoint inside a ``with_actor`` or ``kg_allow_erase`` window
    (any other transaction-local GUC is silently dropped).
    """
    from mycelium_core.services.note_search import flush_note_search_dirty
    from mycelium_core.services.task_search import flush_task_search_dirty

    captured = await _capture_gucs(session)
    await flush_task_search_dirty(session)
    await flush_note_search_dirty(session)
    await session.commit()
    await _rearm_gucs(session, captured)


async def tenant_rollback(session: AsyncSession) -> None:
    """Roll back the current tenant transaction and re-arm the GUCs in a
    fresh one, so the caller can keep using the session (mirror of
    ``tenant_checkpoint`` for the abort path, e.g. the SdI-notification
    redelivery dedupe that rolls back a duplicate ingest).

    The GUCs are re-armed from the values stashed at ``tenant_session`` open
    (an aborted transaction cannot be queried), so a mid-request
    ``app.current_role`` pin or ``with_actor`` override is NOT restored --
    use only on paths that set neither (the system ingest paths)."""
    captured = session.info.get("tenant_gucs")
    await session.rollback()
    if captured is not None:
        await _rearm_gucs(session, captured)


@asynccontextmanager
async def admin_session(*, actor_kind: ActorKind = "system") -> AsyncIterator[AsyncSession]:
    """A no-tenant session: sets the actor kind but never ``app.current_org``.

    Used by the worker to enumerate workspaces before fanning out into a
    per-org ``tenant_session`` (and by bootstrap/migrations/tests). Under
    RLS this session is fail-closed for org-scoped tables EXCEPT the
    enumeration tables (``organizations``, ``memberships``,
    ``google_calendar_subscriptions``), which carry a ``FOR SELECT`` policy
    that opens to a system session with no current org (migration 0029).
    So a ``system`` caller can list every workspace and resolve its owner,
    yet still sees nothing in tasks/notifications/etc. until it narrows to
    a single org via ``tenant_session``.

    ``actor_kind`` defaults to ``system`` so audit rows from this path are
    attributed to a system actor and the enumeration policies apply;
    callers can override (e.g. a CLI acting for a specific operator), but a
    non-system kind sees no org-scoped rows at all (fail-closed).
    """
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.current_actor_kind', :ak, true),"
                    "       set_config('app.current_actor_subject', '', true)"
                ),
                {"ak": actor_kind},
            )
            yield session


@asynccontextmanager
async def with_actor(
    session: AsyncSession,
    *,
    actor_kind: ActorKind,
    actor_subject_id: str | None = None,
) -> AsyncIterator[None]:
    """Temporarily shift the GUC ``actor_kind`` + ``actor_subject``
    within an already-open session.

    Use case: ``services/agent_runtime.start_run`` opens nothing of
    its own (the caller hands it a tenant session), yet the audit
    rows it produces should read ``actor_kind='agent_run'`` rather
    than the caller's kind. The contextmanager saves the current
    GUC values, sets the new ones, and restores them on exit so the
    rest of the caller's transaction is unaffected.

    Because ``set_config(..., true)`` is transaction-local, the
    restore-on-exit pattern is safe: we never leak across connections,
    we only narrow a window inside one transaction.
    """
    saved = (
        await session.execute(
            text(
                "SELECT current_setting('app.current_actor_kind', true),"
                "       current_setting('app.current_actor_subject', true)"
            )
        )
    ).one()
    saved_kind = saved[0] or "human_direct"
    saved_subj = saved[1] or ""
    await session.execute(
        text(
            "SELECT set_config('app.current_actor_kind', :ak, true),"
            "       set_config('app.current_actor_subject', :asubj, true)"
        ),
        {"ak": actor_kind, "asubj": actor_subject_id or ""},
    )
    try:
        yield
    finally:
        await session.execute(
            text(
                "SELECT set_config('app.current_actor_kind', :ak, true),"
                "       set_config('app.current_actor_subject', :asubj, true)"
            ),
            {"ak": saved_kind, "asubj": saved_subj},
        )


@asynccontextmanager
async def as_tenant(
    session: AsyncSession,
    *,
    org_id: str | uuid.UUID,
    user_id: str | uuid.UUID,
) -> AsyncIterator[None]:
    """Temporarily act as a tenant inside an already-open session.

    Sibling of ``with_actor`` next door, and the same mechanism: the
    GUCs are ``set_config(..., is_local => true)``, so this narrows a
    window inside one transaction and leaks across nothing.

    Use case, and the reason it is not merely a convenience: the device
    authorization grant's collection step runs on an UNAUTHENTICATED
    request. A device polling with its own code carries no session, so
    the request has no tenant of its own, yet what it collects must be
    minted as the person who approved it, in the workspace they approved
    it in. The alternative shapes are both worse. Opening a second,
    tenant-scoped session would put the claim and the mint in different
    transactions, and the whole point of that pair is that a failure in
    the mint rolls the claim back. Relaxing the row-level policy on
    ``ai_assistants`` so an untenanted insert could pass would trade a
    narrow window for a standing hole.

    The caller is responsible for having established, from stored state
    rather than from the request, WHICH tenant this is: here, the
    approver recorded on the row when they answered.
    """
    saved = (
        await session.execute(
            text(
                "SELECT current_setting('app.current_org', true),"
                "       current_setting('app.current_user', true)"
            )
        )
    ).one()
    await session.execute(
        text(
            "SELECT set_config('app.current_org', :org, true),"
            "       set_config('app.current_user', :usr, true)"
        ),
        {"org": str(org_id), "usr": str(user_id)},
    )
    try:
        yield
    finally:
        await session.execute(
            text(
                "SELECT set_config('app.current_org', :org, true),"
                "       set_config('app.current_user', :usr, true)"
            ),
            {"org": saved[0] or "", "usr": saved[1] or ""},
        )
