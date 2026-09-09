"""The startup refusal on an unexpected schema.

Two halves, deliberately split. The verdict is a pure function of a
recorded revision string and the chain this build ships, so it is tested
exhaustively with no database. The one thing that genuinely needs a
database is whether the RUNTIME role can read ``alembic_version`` at all
-- the grant migration 0011 exists only for that, and a check the serving
credential cannot execute is not a check.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

from mycelium_core.schema_revision import (
    SchemaRevisionError,
    classify,
    expected_revision,
    known_revisions,
)


def _base_revision() -> str:
    """The oldest revision in the chain, walked rather than sorted."""
    from mycelium_core.schema_revision import _scripts

    return list(_scripts().walk_revisions())[-1].revision


def test_head_matches_the_shipped_chain() -> None:
    """The expected revision is read from the migrations, never written
    by hand, so it follows every new migration on its own."""
    head = expected_revision()
    assert head in known_revisions()
    # Every other revision in the chain is an ancestor, so the head is
    # the only one that is not "behind".
    assert classify(head) is None


def test_a_database_at_head_serves() -> None:
    assert classify(expected_revision()) is None


def test_every_revision_short_of_the_head_is_refused() -> None:
    """Not just the one before it: any ancestor means the migrate step
    did not finish, and each must refuse."""
    head = expected_revision()
    behind = known_revisions() - {head}
    assert behind, "the chain must have more than a single revision"
    for revision in behind:
        assert classify(revision) is not None, f"{revision} is behind {head} and must refuse"


def test_the_refusal_names_the_gap_it_found() -> None:
    """The message is the instruction: it must carry the revision found,
    the revision expected, and every revision between them.

    The base is derived from the chain rather than from sorting the
    revision ids, which would quietly assume that lexical order is chain
    order -- true of ``0001``..``0011`` and not a property of Alembic.
    """
    head = expected_revision()
    base = _base_revision()
    problem = classify(base)
    assert problem is not None
    assert base in problem
    assert head in problem
    # Every revision the operator still has to apply is named.
    for revision in known_revisions() - {base}:
        assert revision in problem, f"{revision} missing from the refusal"
    assert "alembic upgrade head" in problem


def test_an_empty_database_is_refused() -> None:
    problem = classify(None)
    assert problem is not None
    assert expected_revision() in problem
    assert "alembic upgrade head" in problem


def test_a_database_ahead_serves() -> None:
    """The asymmetry that makes a rollback and the migrate-then-roll
    window survivable: a revision this build does not know is a NEWER
    build's, and refusing there would turn every deploy window into an
    outage. If this ever flips to a refusal it must be a decision, not a
    drift, which is why it is asserted rather than left implicit."""
    assert classify("9999_from_a_newer_build") is None


def test_the_refusal_is_its_own_exception_type() -> None:
    """So a caller can tell a schema refusal from any other startup
    failure, and so the deploy path can match on it."""
    assert issubclass(SchemaRevisionError, RuntimeError)


def test_the_runtime_role_can_read_the_revision() -> None:
    """The grant in migration 0011, asserted on the credential the
    services actually serve with.

    Without it the startup check fails as "permission denied" instead of
    as a verdict about the schema, which is a worse failure than the one
    it replaced: it sends the operator to the role configuration rather
    than to the migration step.
    """
    app_url = os.environ.get("MYCELIUM_DATABASE_URL")
    if not app_url:
        pytest.skip("MYCELIUM_DATABASE_URL not set")
    # The async URL with the sync driver: this test only needs one
    # SELECT, and the async engine would drag the app's pool wiring in.
    sync_url = app_url.replace("+asyncpg", "+psycopg")
    engine = sa.create_engine(sync_url, future=True)
    try:
        with engine.connect() as conn:
            role = conn.execute(sa.text("SELECT current_user")).scalar_one()
            recorded = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()
    assert role == "mycelium_app", "this must run on the runtime role, not the owner"
    assert recorded == expected_revision()


def test_the_database_gate_is_armed_in_the_pipeline() -> None:
    """The test above can skip itself when the database URL is absent.
    This asserts the pipeline sets it, so a green run means the grant was
    really exercised rather than skipped.
    """
    import pathlib

    workflow = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    assert workflow.is_file(), f"{workflow} not found"
    text = workflow.read_text()
    assert "MYCELIUM_DATABASE_URL:" in text, "the pipeline must set MYCELIUM_DATABASE_URL"
    assert "mycelium_app" in text, "the pipeline must run the app suite on the runtime role"
