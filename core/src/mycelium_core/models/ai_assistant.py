"""AI assistant: per-user identity an external AI tool (Claude Desktop,
Cursor, a custom MCP client) authenticates with against Mycelium.

Pattern mirrored from bitvision_phoenix's ``ai_assistants`` table: each
assistant is owned by a workspace user, carries a free-form label,
optional provider / model_id / notes (informational), a JSONB ``scope``
list (the MCP tool-permissions surface), and an ``is_active`` flag.
The actual bearer credential lives in ``agent_tokens`` rows linked
back via ``assistant_id`` — rotating the secret mints a new token row
and revokes the old one, the assistant row stays put so historical
attribution survives.

See migration 0059 for the SECURITY DEFINER function that joins
``agent_tokens`` to ``ai_assistants`` on authenticate.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class AssistantRuntime(enum.StrEnum):
    """Who runs the assistant: this system, or something outside it.

    ``identities.kind`` says WHAT a principal is (a user, an
    ai_assistant) and one value of it has been carrying two meanings:

    - ``external``: an MCP client that authenticates here and executes
      elsewhere -- Claude Desktop, Cursor, a custom client. This system
      cannot start it, because it does not run in this process.
    - ``internal``: an assistant the dispatch loop can actually drive,
      by resolving a provider and stepping it under a budget.

    The distinction is not derivable from anything already stored.
    ``ExecutorKind`` is ``human|llm_agent`` only and ``Executor.user_id``
    is a FK to ``users``, so no row links an executor to the assistant
    identity a task is addressed to: the scheduler, the dispatcher and
    ``start_run`` all read ``kind == ai_assistant`` and conclude "the llm
    pool owns this". A task an external client wrote for a person is
    then queued for an execution that can never happen.

    Lives beside the model rather than in its own module (the way
    ``IndexScope`` does) because one table carries it; the module split
    there exists only because two model files import that enum.
    """

    internal = "internal"
    external = "external"


class AiAssistant(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "ai_assistants"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    # Human-readable assignee handle (migration 0060). Workspace-
    # scoped uniqueness via a partial unique index. Seed default is
    # the empty sentinel; the service derives a slug from ``label``
    # on next mutation.
    handle: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSONB list of scope strings (e.g. ['tasks:read', 'time:write']);
    # the MCP gate filters @mcp.tool calls against this set. Empty list
    # = the assistant can call ZERO tools (deny-all default).
    scope: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    # Server default ``external``, and that is the decision rather than a
    # fallback: every existing row, and every row ``create_assistant``
    # writes, is a credential an operator pastes into a client that runs
    # somewhere else. An ``internal`` assistant is one somebody declares.
    runtime: Mapped[AssistantRuntime] = mapped_column(
        SAEnum(AssistantRuntime, name="assistant_runtime", native_enum=True, create_type=False),
        nullable=False,
        default=AssistantRuntime.external,
        server_default=AssistantRuntime.external.value,
    )

    def scope_list(self) -> list[str]:
        """Defensive coercion: JSONB returns whatever was stored — list
        of str in the normal case, but a misbehaving SQL UPDATE could
        leave a dict / scalar. Anything that isn't ``list[str]`` is
        treated as no scopes (deny-all), matching the gate's safe
        default."""
        v: Any = self.scope
        if not isinstance(v, list):
            return []
        return [str(x) for x in v]


__all__ = ["AiAssistant", "AssistantRuntime"]
