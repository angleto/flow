"""Say which assistants this system can actually run.

``identities.kind`` has one value, ``ai_assistant``, for two different
things: a client that authenticates here and executes somewhere else
(Claude Desktop, Cursor, a custom MCP client), and an assistant the
dispatch loop can drive itself. Nothing in the schema separated them.
``ExecutorKind`` is ``human|llm_agent`` only and ``Executor.user_id``
is a FK to ``users``, so there is no row anywhere that ties an executor
to the assistant identity a task is addressed to.

The consequence was a one-way trap. A task an external client creates
is auto-assigned to that client's identity, three sites read
``kind == ai_assistant`` and route it to the llm pool, and it queues
for an execution that cannot happen: the queue held 210 requests
against zero runs, all pointing at the single ``llm_agent`` executor
because ``_eligible_agents`` filters on capability tags alone and that
executor has none.

``external`` is the server default because it is the truth for every
row that exists and for every row ``create_assistant`` writes: the
service mints a secret an operator pastes into a client running on
their own machine. ``internal`` is a declaration somebody makes, not a
state a row falls into. So there is no backfill -- the default is
already correct everywhere -- and no data is rewritten.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE assistant_runtime AS ENUM ('internal', 'external')")
    # ADD COLUMN with a non-volatile default is catalogue-only on
    # PostgreSQL 11+: no table rewrite, no lock held for the row count.
    op.add_column(
        "ai_assistants",
        sa.Column(
            "runtime",
            postgresql.ENUM("internal", "external", name="assistant_runtime", create_type=False),
            nullable=False,
            server_default="external",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_assistants", "runtime")
    op.execute("DROP TYPE assistant_runtime")
