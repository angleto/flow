"""Adjudication framework persistence (docs/adr/0027).

An ``Adjudication`` is the process of reaching a single decision on a
question, regardless of the strategy used (single-shot, debate,
quorum, contract-net, human escalation, composition thereof).

An ``AdjudicationStep`` is one event in that process with a polymorphic
``kind`` (``turn``, ``vote``, ``score``, ``escalation``, ``synthesis``,
``intervention``, ``tool_call``). The same table backs the UI timeline,
the audit log and the convergence detector; a strategy populates only
the kinds it produces.

Org-scoped + RLS like every tenant table (docs/adr/0002, 0007). The
adjudication carries ``VersionMixin`` for optimistic concurrency on
status/outcome transitions; the step is append-only (no version).
"""

from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.embed_dims import EMBED_DIM
from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class AdjudicationStatus(enum.StrEnum):
    running = "running"
    resolved = "resolved"
    # Reached a known irreducible state: the strategy yielded a
    # ``HumanInLoop`` step or escalated upstream. The outcome row is
    # still written (decision may be partial); a separate resolve API
    # closes the row.
    escalated = "escalated"
    # Hard stop: budget cap, capability missing at runtime, internal
    # error. Distinct from escalated (which is a normal control-flow
    # exit), so callers can tell a safety stop from an expected pause.
    aborted = "aborted"


class AdjudicationStepKind(enum.StrEnum):
    # A natural-language contribution from an agent (debate turn,
    # single-shot answer). Carries the embedding when an embedder is
    # available.
    turn = "turn"
    # A discrete ballot in a quorum/threshold strategy.
    vote = "vote"
    # A numeric signal produced by the strategy (e.g. coherence energy
    # for a round, agreement ratio, change-mind delta).
    score = "score"
    # Strategy explicitly escalated this decision (HumanInLoop, or a
    # composition fallback reaching the escalation tail).
    escalation = "escalation"
    # Judge or strategy synthesis (final or per-round).
    synthesis = "synthesis"
    # Human intervention recorded against the timeline (approval,
    # override, resolve-escalation).
    intervention = "intervention"
    # A tool call made by an agent during the adjudication; mirrors
    # the bounded loop in ``services/agent_runtime.py``.
    tool_call = "tool_call"


class Adjudication(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "adjudications"

    # Optional: not every adjudication is anchored to a task (a policy
    # decision, a scheduler pick, a one-off question).
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Free-text question the strategy is arbitrating on. Truncated by
    # the service layer if needed; full payload (with structured
    # context) goes into ``context_json``.
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    # Registered strategy id and its per-invocation config. The id is a
    # free-form string by design: out-of-tree strategies plug in via
    # entry-points and their ids are not known at compile time.
    strategy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    strategy_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    status: Mapped[AdjudicationStatus] = mapped_column(
        SAEnum(
            AdjudicationStatus,
            name="adjudication_status",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )

    # ``decision``, ``residual_dissent``, ``escalated`` all packed in
    # outcome_json for forward-compat; confidence is denormalised so
    # listing/filtering by confidence does not require JSONB indexing.
    outcome_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)

    cost_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cost_wall_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The user who started this adjudication; the strategy runs under
    # this actor's tenant_session (RLS + role checks).
    created_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )


class AdjudicationStep(OrgScopedMixin, Base):
    """Append-only event log for one adjudication.

    No version mixin: steps are written once and never updated. A
    correction is a new step (``kind=intervention`` typically), not an
    in-place edit.
    """

    __tablename__ = "adjudication_steps"
    __table_args__ = (
        UniqueConstraint(
            "adjudication_id",
            "step_no",
            name="uq_adjudication_steps_adjudication_id_step_no",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    adjudication_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("adjudications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[AdjudicationStepKind] = mapped_column(
        SAEnum(
            AdjudicationStepKind,
            name="adjudication_step_kind",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Free-form agent identifier. May be a registered Executor UUID, an
    # internal label like ``judge`` or ``user:<uid>``; not a FK so the
    # framework is not coupled to one executor model.
    agent_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
