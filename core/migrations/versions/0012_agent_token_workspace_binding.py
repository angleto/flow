"""A credential says which workspaces it may act in.

``agent_tokens.org_id`` has always named the workspace a credential was
minted for, and ``_confine_agent_token`` refuses the credential anywhere
else. That is right for the CLI and for an MCP assistant, and wrong for
the browser panel: a person moves between their workspaces on one login,
and a panel that has to be re-connected per workspace is a panel that
holds one secret per workspace for no gain in what it may do.

So the binding becomes a property OF THE CREDENTIAL rather than an
assumption about all of them. ``workspace`` is what every existing row
is and what the column defaults to, so nothing changes for anything
minted before this. ``account`` reaches every workspace its holder
belongs to -- and reaches it as the holder: the workspace still arrives
per request, the holder's membership there still authorizes every
operation, and a workspace they are not a member of is refused exactly
as it is for a session. What widens is the reach of the secret, not the
authority it carries.

The value is decided by the service from what the credential may do (a
scope subset of ``SELF_SERVICE_SCOPES``), never by a label the caller
chooses, and it is read at authentication time rather than looked up
afterwards: a credential's tenancy is part of authenticating it, and a
second query would be a second place for the answer to come from.

``DROP`` + ``CREATE`` and not ``CREATE OR REPLACE``: the OUT parameter
list IS the function's return type, so replacing it in place is refused
by PostgreSQL once a column is added to it. The drop discards the
function's ACL with it -- ``REVOKE ALL ... FROM PUBLIC`` /
``GRANT ALL ... TO mycelium_app``, which the baseline installs and
which production depends on (a missing grant surfaces there as
``permission denied for function authenticate_agent_token`` while dev
and CI keep working on PostgreSQL's default PUBLIC execute). Both are
re-issued below, in the same statement block, so the function cannot
exist without them.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The function as 0002 left it, with one OUT column added and one field
# in the SELECT and the assignment block. Reproduced whole rather than
# patched, because a plpgsql body is not composable and the only way to
# read what this function does is to read one of these.
_FN = """
CREATE FUNCTION public.authenticate_agent_token(
    p_hash bytea,
    OUT out_token_id uuid,
    OUT out_user_id uuid,
    OUT out_org_id uuid,
    OUT out_scope text,
    OUT out_assistant_id uuid,
    OUT out_assistant_scope jsonb,
    OUT out_assistant_active boolean,
    OUT out_workspace_binding text
) RETURNS SETOF record
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $fn$
    DECLARE
      v_id uuid;
      v_user uuid;
      v_org uuid;
      v_scope text;
      v_expires timestamptz;
      v_revoked timestamptz;
      v_assistant_id uuid;
      v_assistant_scope jsonb;
      v_assistant_active boolean;
      v_user_active boolean;
      v_binding text;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      SELECT t.id, t.user_id, t.org_id, t.scope, t.expires_at, t.revoked_at,
             t.assistant_id, a.scope, a.is_active, u.is_active,
             t.workspace_binding::text
        INTO v_id, v_user, v_org, v_scope, v_expires, v_revoked,
             v_assistant_id, v_assistant_scope, v_assistant_active, v_user_active,
             v_binding
        FROM agent_tokens t
        LEFT JOIN ai_assistants a ON a.id = t.assistant_id
        LEFT JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = p_hash;

      IF v_id IS NULL
         OR v_revoked IS NOT NULL
         OR (v_expires IS NOT NULL AND v_expires <= now())
         OR v_user_active IS DISTINCT FROM true
         OR (v_assistant_id IS NOT NULL AND v_assistant_active IS DISTINCT FROM true) THEN
        PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
        PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
        RETURN;
      END IF;

      PERFORM set_config('app.current_org', v_org::text, true);
      PERFORM set_config('app.current_user', v_user::text, true);
      UPDATE agent_tokens SET last_used_at = now(), updated_at = now()
        WHERE id = v_id;
      PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
      PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);

      out_token_id := v_id;
      out_user_id := v_user;
      out_org_id := v_org;
      out_scope := v_scope;
      out_assistant_id := v_assistant_id;
      out_assistant_scope := v_assistant_scope;
      out_assistant_active := v_assistant_active;
      -- Never NULL: the column is NOT NULL with a default, and a
      -- credential whose tenancy could not be read must not authenticate
      -- as though it were unconfined.
      out_workspace_binding := coalesce(v_binding, 'workspace');
      RETURN NEXT;
    END
    $fn$;
"""

# 0002's body, byte for byte, for the downgrade: the same function
# without the tenancy column.
_FN_WITHOUT_BINDING = """
CREATE FUNCTION public.authenticate_agent_token(
    p_hash bytea,
    OUT out_token_id uuid,
    OUT out_user_id uuid,
    OUT out_org_id uuid,
    OUT out_scope text,
    OUT out_assistant_id uuid,
    OUT out_assistant_scope jsonb,
    OUT out_assistant_active boolean
) RETURNS SETOF record
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $fn$
    DECLARE
      v_id uuid;
      v_user uuid;
      v_org uuid;
      v_scope text;
      v_expires timestamptz;
      v_revoked timestamptz;
      v_assistant_id uuid;
      v_assistant_scope jsonb;
      v_assistant_active boolean;
      v_user_active boolean;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      SELECT t.id, t.user_id, t.org_id, t.scope, t.expires_at, t.revoked_at,
             t.assistant_id, a.scope, a.is_active, u.is_active
        INTO v_id, v_user, v_org, v_scope, v_expires, v_revoked,
             v_assistant_id, v_assistant_scope, v_assistant_active, v_user_active
        FROM agent_tokens t
        LEFT JOIN ai_assistants a ON a.id = t.assistant_id
        LEFT JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = p_hash;

      IF v_id IS NULL
         OR v_revoked IS NOT NULL
         OR (v_expires IS NOT NULL AND v_expires <= now())
         OR v_user_active IS DISTINCT FROM true
         OR (v_assistant_id IS NOT NULL AND v_assistant_active IS DISTINCT FROM true) THEN
        PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
        PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
        RETURN;
      END IF;

      PERFORM set_config('app.current_org', v_org::text, true);
      PERFORM set_config('app.current_user', v_user::text, true);
      UPDATE agent_tokens SET last_used_at = now(), updated_at = now()
        WHERE id = v_id;
      PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
      PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);

      out_token_id := v_id;
      out_user_id := v_user;
      out_org_id := v_org;
      out_scope := v_scope;
      out_assistant_id := v_assistant_id;
      out_assistant_scope := v_assistant_scope;
      out_assistant_active := v_assistant_active;
      RETURN NEXT;
    END
    $fn$;
"""

# The pair the baseline installs, re-issued after every drop. Written
# with the input signature only: OUT parameters are not part of a
# function's identity, so this names the same function whatever its
# return columns are.
_ACL = """
REVOKE ALL ON FUNCTION public.authenticate_agent_token(bytea) FROM PUBLIC;
GRANT ALL ON FUNCTION public.authenticate_agent_token(bytea) TO mycelium_app;
"""


def upgrade() -> None:
    op.execute("CREATE TYPE workspace_binding AS ENUM ('workspace', 'account')")
    # ADD COLUMN with a non-volatile default is catalogue-only on
    # PostgreSQL 11+: no rewrite, and every existing credential reads
    # back as what it has always been.
    op.add_column(
        "agent_tokens",
        sa.Column(
            "workspace_binding",
            postgresql.ENUM("workspace", "account", name="workspace_binding", create_type=False),
            nullable=False,
            server_default="workspace",
        ),
    )
    op.execute("DROP FUNCTION public.authenticate_agent_token(bytea)")
    op.execute(_FN)
    op.execute(_ACL)


def downgrade() -> None:
    op.execute("DROP FUNCTION public.authenticate_agent_token(bytea)")
    op.execute(_FN_WITHOUT_BINDING)
    op.execute(_ACL)
    op.drop_column("agent_tokens", "workspace_binding")
    op.execute("DROP TYPE workspace_binding")
