"""Let the runtime role read which schema revision it is serving against.

The services now refuse to start when the database is behind the
migrations they ship (``mycelium_core.schema_revision``). That check has
to run on the connection the service will actually serve with -- the
``mycelium_app`` role -- because a check that passes on a credential the
service does not use is not a check of anything.

``alembic_version`` is created by Alembic as the owner, and the baseline
grants tables to ``mycelium_app`` one by one (an allowlist: a table
nobody granted is unreadable, which is the posture and not an oversight).
``alembic_version`` was never on that list, so the runtime role could not
read it and the startup check would have failed as "permission denied"
rather than as a verdict about the schema.

This adds the one entry, read-only. What the role gains is a single
opaque revision string; it is not tenant data, it grants no write and no
further reach, and the alternative was handing the serving process the
owner DSN to run a check with, which trades a much larger privilege for
the same string (docs/adr/0015 exists to keep those roles apart).

Grants are cluster-level, not database-level, so this is written as a
``DO`` block guarded on the role existing: a developer database created
without ``bootstrap_roles.sql`` has no ``mycelium_app``, and a migration
that hard-fails there would make the role optional in one place and
mandatory in another.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mycelium_app') THEN
            GRANT SELECT ON TABLE public.alembic_version TO mycelium_app;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mycelium_app') THEN
            REVOKE SELECT ON TABLE public.alembic_version FROM mycelium_app;
          END IF;
        END $$;
        """
    )
