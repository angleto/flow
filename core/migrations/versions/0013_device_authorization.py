"""Let a device be approved from a session, instead of being handed a
secret by a web page.

The browser extension used to receive its credential over
``externally_connectable``: it opened the settings page with a nonce in
the query string, the page minted a credential and pushed it back through
Chrome's messaging. Three defects came with that shape, and they are the
reason for this table.

The request lived in a URL, so anything that redirected between that URL
and the person destroyed it. The login redirect did exactly that, which is
how "press Connect" became "the extension is broken": you were sent to the
login form, the form landed you on your notes, and the extension waited
for an approval nobody could give any more.

The credential was minted before anyone knew the extension would take it,
so every refused handover left a live credential that nobody held and
nobody knew to revoke.

And the mechanism required a web page to hold the right to send messages
to an extension, which is a standing outward surface bought to save a few
seconds of waiting.

The device authorization grant (RFC 8628) has none of the three. The
request lives in this table, keyed by a code the DEVICE holds, so no
navigation can lose it. The credential is minted in the transaction that
marks the row redeemed, so approved-but-uncollected has no representation.
And nothing has to be able to talk to the extension: it asks, on its own
schedule, until there is an answer.

Two codes and two shapes of storage. ``user_code`` is compared by a human
against what the extension is showing, so it is short, drawn from an
alphabet with no character that can be read as another, and it collects
nothing on its own -- it is stored in clear because it is not a secret.
``device_code_hash`` is the only thing that collects, and only its SHA-256
is here, as for ``agent_tokens`` and ``refresh_tokens``: whoever can read
this table cannot use what they read.

Pre-tenant and therefore without row-level security, like
``refresh_tokens``: when a request is opened there is no user and no
workspace yet, so there is no tenant to scope a policy to. What stands in
its place is that every read on the collection path is by digest of an
unguessable value, and that the approval path reads by ``user_code`` only
for a caller who is already authenticated.

The unique index on ``user_code`` is PARTIAL, over open requests only. A
short code drawn from a small alphabet has to be reusable once the request
holding it is finished, and a plain unique constraint would slowly consume
the space and then start failing to open requests at all.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_authorizations",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("device_code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_code", sa.String(length=16), nullable=False),
        sa.Column("client", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("denied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_by_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("approved_org_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("assistant_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("origin_ip", sa.String(length=45), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_device_authorizations"),
        sa.UniqueConstraint("device_code_hash", name="uq_device_authorizations_device_code_hash"),
        # Whoever answered, and the workspace they approved in, disappear with
        # the account or the workspace: a spent request is a record of
        # something that happened to them, and it has no meaning without
        # them. The assistant is SET NULL instead -- revoking the
        # credential must not erase the approval that produced it, which
        # is the one thing an audit would come here to read.
        sa.ForeignKeyConstraint(
            ["answered_by_id"],
            ["users.id"],
            name="fk_device_authorizations_answered_by_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approved_org_id"],
            ["organizations.id"],
            name="fk_device_authorizations_approved_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["ai_assistants.id"],
            name="fk_device_authorizations_assistant_id_ai_assistants",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_device_authorizations_user_code",
        "device_authorizations",
        ["user_code"],
    )
    # Only one OPEN request may hold a given short code at a time. Open
    # is exactly "nobody has answered and it has not been collected";
    # expiry is deliberately NOT part of the predicate, because an index
    # cannot depend on the clock -- the service checks that, and an
    # expired row keeping its code until it is swept is harmless.
    op.create_index(
        "uq_device_authorizations_open_user_code",
        "device_authorizations",
        ["user_code"],
        unique=True,
        postgresql_where=sa.text(
            "approved_at IS NULL AND denied_at IS NULL AND redeemed_at IS NULL"
        ),
    )
    op.create_index(
        "ix_device_authorizations_expires_at",
        "device_authorizations",
        ["expires_at"],
    )
    # Same grant as refresh_tokens: the application role reads and writes
    # its own rows, and no row-level policy exists to be granted around
    # because this table has no tenant.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.device_authorizations TO mycelium_app"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON TABLE public.device_authorizations FROM mycelium_app")
    op.drop_index("ix_device_authorizations_expires_at", table_name="device_authorizations")
    op.drop_index("uq_device_authorizations_open_user_code", table_name="device_authorizations")
    op.drop_index("ix_device_authorizations_user_code", table_name="device_authorizations")
    op.drop_table("device_authorizations")
