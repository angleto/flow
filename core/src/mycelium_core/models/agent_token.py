"""Agent tokens: long-lived bearer credentials for MCP / external automation.

See migration 0056 for the rationale and the SECURITY DEFINER
``authenticate_agent_token`` helper that lets the verifier find a row
without a tenant GUC.

Storage discipline: the raw value is returned to the operator exactly
once at create time; only its SHA-256 digest (plus a short non-secret
``prefix`` for UI disambiguation) lives in the DB. Lookup at verify
time is O(1) on ``token_hash``.
"""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class WorkspaceBinding(enum.StrEnum):
    """Which workspaces a credential may act in.

    ``workspace``: the one it was minted for, and no other. The default,
    and what every credential minted before this column existed is: the
    CLI stores a workspace beside each token and refuses to switch, and
    the server enforces the same in ``_confine_agent_token``.

    ``account``: every workspace its holder belongs to, decided per
    request from the workspace the request names. It is NOT a wider
    grant. The holder's own membership in that workspace still
    authorizes each operation, so the credential is a delegation of what
    the person can already do there and follows them into a workspace
    they join and out of one they leave. What it does widen is the blast
    radius of the secret itself, and that is written down where the
    reader approves it and in ``docs/extension.md``.
    """

    workspace = "workspace"
    account = "account"


class AgentToken(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "agent_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Human-readable label ("Claude Desktop", "Cron uploader", ...).
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # First chars of the raw value (e.g. ``mycelium_at_AbCdEfGh``); not a
    # secret -- shown in the UI so an operator can identify which
    # rotation of a long-lived credential is which.
    prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    # ``sha256(raw_value.encode("utf-8"))``. UNIQUE in the migration.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Capability bucket. ``mcp`` for v1.1; future buckets (e.g.
    # ``webhook``, ``api``) live in the same table without a schema
    # change.
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="mcp")
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bind to an ``ai_assistants`` row (migration 0059). NULL for legacy
    # bare tokens minted before the assistant flow existed; they keep
    # working with full MCP surface (no scope filter). The UI only mints
    # new tokens via the assistant lifecycle.
    assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_assistants.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Decided by the service at mint time from what the credential may
    # do, never by the caller's label. ``org_id`` above stays the
    # workspace it was minted in either way: it is the home this
    # credential belongs to and the tenant the MCP surface reads, which
    # is why an account-bound credential changes the REST door only.
    workspace_binding: Mapped[WorkspaceBinding] = mapped_column(
        SAEnum(WorkspaceBinding, name="workspace_binding", native_enum=True, create_type=False),
        nullable=False,
        default=WorkspaceBinding.workspace,
        server_default=WorkspaceBinding.workspace.value,
    )


__all__ = ["AgentToken", "WorkspaceBinding"]
