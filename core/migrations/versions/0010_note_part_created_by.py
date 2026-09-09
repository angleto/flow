"""Say which agent wrote a block, so several can share one note.

A task's work note is the natural place for work in progress: it is
created on demand, there is exactly one per task, it is linked to the
task, and its parts can be appended without reading the note back. Two
uses have arrived for it at once -- a scratchpad that survives a
session's context being compacted away, and a surface several agents
append to while the work is still running -- and both need the same
thing from a part that the schema does not record: who wrote it.

``NotePart`` already carries ``created_at`` and ``updated_at`` from
``TimestampMixin``, so "when" was always there (though the MCP
serialisers dropped it, which is a separate fix in the same change).
"Who" was not stored at all. Without it a reader can order the blocks
and cannot attribute them, which is precisely the question a second
agent asks first: not what is in this note, but what did the others
put there.

Nullable with no backfill, because that is the truth: for every part
written before this migration nobody recorded an author, and inventing
one would be worse than a null. ``ON DELETE SET NULL`` matches
``note_task_link.created_by`` and ``note_link.created_by``: an identity
can be removed, and the block it wrote stays and stops naming it.

ADD COLUMN of a nullable column with no default is catalogue-only on
PostgreSQL: no table rewrite.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "note_part",
        sa.Column(
            "created_by",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("identities.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("note_part", "created_by")
