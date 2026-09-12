"""The fleet embedding dims, as the LIVE columns actually declare them.

``mycelium_core.embed_dims`` is the single source every embedder and every
model reads, but a migration cannot import it: a migration is a historical
record and must keep meaning what it meant when it ran, so the DDL spells
the number out. That leaves exactly one seam where the constant and the
schema can drift apart, and drift there is silent in the worst direction:
the code coerces every vector to the constant, the column keeps its own
width, and the failure surfaces as a driver error on the next write with
nothing naming the cause. This asserts the agreement instead.

Read back from a live database rather than from the migration source,
because a migration that ran is the only evidence that counts: a hand-edited
column, a squashed revision or a partition created outside the migration all
produce a schema the source does not describe.

``memory_blobs`` is PARTITION BY HASH (org_id), so the parent's declaration
is not the whole answer -- a partition attached with a different width would
accept writes the parent's type check never sees. Every partition is checked
too, by the same rule.

Runs on the sync (owner) engine, like ``test_migrations.py`` and the other
structure gates; assumes the test DB is at ``alembic upgrade head``.
"""

from __future__ import annotations

import os
import re

import pytest
import sqlalchemy as sa

from mycelium_core.embed_dims import EMBED_DIM, EMBED_DIM_HOSTED

#: ``(table, column, expected type name, expected dim)``. The type name is
#: part of the expectation because ``vector`` and ``halfvec`` differ in the
#: bytes per component and in the HNSW dimension ceiling, so a column that
#: silently changed kind while keeping its width is still a defect.
EXPECTED = (
    ("memory_blobs", "embedding", "vector", EMBED_DIM),
    ("memory_blobs", "embedding_hosted", "halfvec", EMBED_DIM_HOSTED),
    ("adjudication_steps", "embedding", "vector", EMBED_DIM),
)

#: ``format_type`` renders a pgvector column as ``vector(1024)``. Parsed
#: rather than read from ``atttypmod`` directly: the encoding of the modifier
#: is pgvector's private business and has no promise attached to it, while
#: the rendered form is what every other tool in the ecosystem shows.
_TYPE = re.compile(r"^(?P<name>[a-z_]+)\((?P<dim>\d+)\)$")


def _engine() -> sa.Engine:
    sync_url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    if not sync_url:
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC not set")
    return sa.create_engine(sync_url, future=True)


def _declared(conn: sa.Connection, table: str, column: str) -> list[tuple[str, str]]:
    """``(relation, rendered type)`` for ``column`` on ``table`` AND on every
    partition of it. Ordinary tables yield one row."""
    rows = conn.execute(
        sa.text(
            """
            WITH target AS (
                SELECT c.oid
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = :table AND n.nspname = 'public'
            ),
            family AS (
                SELECT oid FROM target
                UNION
                SELECT i.inhrelid FROM pg_inherits i JOIN target t ON i.inhparent = t.oid
            )
            SELECT c.relname, format_type(a.atttypid, a.atttypmod)
            FROM family f
            JOIN pg_class c ON c.oid = f.oid
            JOIN pg_attribute a ON a.attrelid = f.oid
            WHERE a.attname = :column AND NOT a.attisdropped AND a.attnum > 0
            ORDER BY c.relname
            """
        ),
        {"table": table, "column": column},
    ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


@pytest.mark.parametrize(("table", "column", "type_name", "dim"), EXPECTED)
def test_column_matches_the_fleet_constant(
    table: str, column: str, type_name: str, dim: int
) -> None:
    engine = _engine()
    with engine.connect() as conn:
        declared = _declared(conn, table, column)

    assert declared, (
        f"{table}.{column} does not exist in the live schema. Either the "
        f"migration that creates it has not run, or the column was renamed "
        f"without updating mycelium_core.embed_dims and this gate."
    )
    for relation, rendered in declared:
        m = _TYPE.match(rendered)
        assert m is not None, f"{relation}.{column} is {rendered!r}, not a dimensioned vector type"
        assert m.group("name") == type_name, (
            f"{relation}.{column} is {rendered!r} but the fleet expects {type_name}: "
            f"vector and halfvec differ in storage width and in the HNSW dimension ceiling."
        )
        assert int(m.group("dim")) == dim, (
            f"{relation}.{column} is {rendered!r} while mycelium_core.embed_dims declares "
            f"{dim}. Every embedder coerces to the constant, so this drift makes every "
            f"write to this column fail at the driver. Changing the fleet dim is a "
            f"drop+rebuild of the column plus a re-embedding of the corpus (ADR-0030): "
            f"the migration and the constant move together or not at all."
        )


def test_every_partition_of_memory_blobs_was_checked() -> None:
    """The parametrised gate above asserts what it FINDS, so a query that
    returned only the parent would pass while saying nothing about the
    partitions. ``memory_blobs`` is hash-partitioned, so it must come back
    with more than the parent relation."""
    engine = _engine()
    with engine.connect() as conn:
        declared = _declared(conn, "memory_blobs", "embedding")
        partitioned = conn.execute(
            sa.text(
                "SELECT c.relkind = 'p' FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = 'memory_blobs' AND n.nspname = 'public'"
            )
        ).scalar_one()

    assert partitioned, "memory_blobs is no longer partitioned: this gate's premise changed"
    assert len(declared) > 1, (
        "memory_blobs is declared partitioned but no partition carries the "
        "embedding column: the family query is not reaching them, so the dim "
        "gate above is only checking the parent."
    )
