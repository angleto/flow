"""Refuse to serve against a schema this build does not expect.

Migrations are a separate, gated deploy step (the migrate Job runs
``alembic upgrade head`` before the app rolls). Nothing checked that the
step had actually run. A deploy that skipped it produced a service that
started, served, and then failed on whichever request first touched the
changed table -- a scattered set of runtime errors nobody attributes to
the deploy, hours after it. This turns that into one line at startup
naming the revision that is missing, which is the whole point: the
message is the instruction.

**Behind is refused, ahead is not.** The database being AHEAD of this
build is not a fault, it is the ordinary state twice in every release:

- between the migrate Job finishing and the last old pod being replaced,
  the pods still serving are one revision behind the database, and an
  eviction in that window must not fail to restart;
- a rollback to the previous image runs old code against the newer
  schema on purpose, and that is exactly when refusing to start would
  cost the most.

So the refusal is asymmetric, and the asymmetry is the design: a
database that has not caught up with the code is broken, a database the
code has not caught up with is a deploy in progress. Rejected: strict
equality, which is simpler to state and would have made both cases above
an outage.

What it does not cover: it compares a recorded revision, not the shape of
the schema, so a hand-edited column that leaves ``alembic_version`` alone
passes. It cannot tell a genuinely-newer database from a foreign one
(both look like "a revision this build does not know"), and it says
nothing about whether an ahead schema is actually compatible with this
code -- an expand-and-contract window that drops a column this build
still reads will pass here and fail at the query.
"""

from __future__ import annotations

import logging
import pathlib
from functools import lru_cache

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from mycelium_core.db import get_engine

_log = logging.getLogger("mycelium.schema")


# PostgreSQL SQLSTATE 42501. Matched on the code rather than on the message,
# which is localised by the server's lc_messages.
_INSUFFICIENT_PRIVILEGE = "42501"


def _is_insufficient_privilege(exc: BaseException) -> bool:
    """True iff ``exc`` is PostgreSQL refusing the read for lack of a grant.

    Walks the ``__cause__`` chain rather than reading ``exc.orig``
    directly: the driver exception that carries ``sqlstate`` (asyncpg and
    psycopg both expose it, per ``db._is_transient_connect_error``) sits
    under a SQLAlchemy wrapper here, and how many layers deep is a
    dialect and version detail this must not encode.
    """
    seen: BaseException | None = exc
    # Bounded rather than "until None": a chain is not supposed to cycle,
    # and a startup check is not the place to find out that one does.
    for _ in range(8):
        if seen is None:
            break
        if getattr(seen, "sqlstate", None) == _INSUFFICIENT_PRIVILEGE:
            return True
        seen = getattr(seen, "orig", None) or seen.__cause__
    return False


class SchemaRevisionError(RuntimeError):
    """The database is not at a revision this build can serve against."""


@lru_cache(maxsize=1)
def _scripts() -> ScriptDirectory:
    """The migration chain shipped inside this build.

    Resolved the way ``core/alembic.ini`` resolves it (``script_location
    = %(here)s/migrations``, sibling of ``src/``), from the installed
    package rather than the current working directory: the API, the
    worker and the SdI service all start from different ones.
    """
    here = pathlib.Path(__file__).resolve().parents[2] / "migrations"
    if not here.is_dir():
        # Fail closed. A check that cannot find what it checks against
        # must not report success -- that is the failure mode this file
        # exists to remove, reintroduced one level up.
        raise SchemaRevisionError(
            f"migration scripts not found at {here}: cannot verify the schema revision. "
            "The image must ship core/migrations beside core/src."
        )
    return ScriptDirectory(str(here))


def expected_revision() -> str:
    """The head of this build's migration chain."""
    head = _scripts().get_current_head()
    if head is None:
        raise SchemaRevisionError("the migration chain in this build has no head revision")
    return head


def known_revisions() -> frozenset[str]:
    """Every revision this build's chain contains, head to base."""
    return frozenset(script.revision for script in _scripts().walk_revisions())


def classify(db_revision: str | None) -> str | None:
    """The refusal message for ``db_revision``, or None to serve.

    Pure, so the decision is testable without a database. ``db_revision``
    is what ``alembic_version`` holds; None means the table is absent or
    empty, i.e. no schema has ever been applied here.
    """
    head = expected_revision()
    if db_revision == head:
        return None
    if db_revision is None:
        return (
            f"the database has no schema revision recorded and this build expects {head}. "
            "Run the migration step (alembic upgrade head) against it before serving."
        )
    if db_revision not in known_revisions():
        # Ahead: migrated by a build newer than this one. See the module
        # docstring -- this is the migrate-then-roll window and the
        # rollback, both of which must keep serving.
        _log.warning(
            "database is at revision %s, which this build (head %s) does not know: "
            "assuming a newer schema and serving anyway",
            db_revision,
            head,
        )
        return None
    # Behind: this build knows the revision, so it is one of ours and it
    # is not the head. Name every revision between it and the head, in
    # the order they have to be applied, because that list IS the fix.
    # ``iterate_revisions`` is exclusive of the lower bound and yields
    # head-first, which is the range an upgrade would apply, reversed.
    missing = [script.revision for script in _scripts().iterate_revisions(head, db_revision)]
    missing.reverse()
    return (
        f"the database is at schema revision {db_revision} and this build expects {head}. "
        f"Missing: {', '.join(missing)}. "
        "Run the migration step (alembic upgrade head) against it before serving."
    )


async def read_database_revision() -> str | None:
    """What ``alembic_version`` holds, or None if there is no schema.

    Read on the serving connection, as the runtime role, deliberately:
    the check has to speak for the credential the service will actually
    query with. Migration 0011 grants that role SELECT on this one table
    for exactly this reason.
    """
    async with get_engine().connect() as conn:
        # Ask whether the table exists BEFORE selecting from it rather
        # than catching the failure: in PostgreSQL a failed statement
        # aborts the transaction, so the probe would have to run on a
        # connection that can no longer answer it. Asking first also
        # separates "no schema at all" from "cannot read the schema",
        # which are different faults with different fixes.
        exists = await conn.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
        if not exists:
            return None
        try:
            recorded = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            return str(recorded) if recorded is not None else None
        except ProgrammingError as exc:
            if not _is_insufficient_privilege(exc):
                raise
            # The grant lands in revision 0011, so a role that cannot
            # read this table is serving a database from before it --
            # which is behind any build carrying this check. Say that
            # rather than surfacing "permission denied", which would
            # send the operator to the role configuration instead of to
            # the migration step that actually fixes it.
            raise SchemaRevisionError(
                f"cannot read the schema revision as the runtime role, and this build "
                f"expects {expected_revision()}. The database is behind revision 0011, "
                "which grants that read. Run the migration step (alembic upgrade head) "
                "against it before serving."
            ) from exc


async def verify_schema_revision() -> None:
    """Raise unless this build can serve against the database's schema.

    Call from the startup path, before the process begins serving.
    """
    problem = classify(await read_database_revision())
    if problem is not None:
        raise SchemaRevisionError(problem)
    _log.info("schema revision %s matches this build", expected_revision())
