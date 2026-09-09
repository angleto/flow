"""Multi-part notes (parent task c0459c4b, design note 2d228758).

A note is a list of ordered markdown blocks; the previous
``notes.transcript`` is a single implicit part (ord=0) until Phase 6
of the rollout (task 1cd8bc0a) drops the column entirely. The
``NotePart`` model owns the body; ``NotePartUIState`` owns the
per-user collapse state synced cross-device.

Order is the natural axis: every part carries an integer ``ord``,
unique within a note (DEFERRABLE so the SPA can shuffle the whole
sequence in one transaction). ``lang`` is a free-form ISO 639-1
hint (NULL = unspecified) that lights up the side-by-side EN/IT
layout when exactly two parts disagree on language. ``merged_from_note_id``
records provenance after a ``POST /notes/merge`` so the audit trail
survives the source-note soft delete.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class NotePart(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "note_part"
    __table_args__ = (
        # DEFERRABLE INITIALLY DEFERRED so a single reorder transaction
        # can swap two ords without forcing the client to step through
        # a "next free" scratchpad ord. Enforced at COMMIT time.
        UniqueConstraint(
            "note_id",
            "ord",
            name="uq_note_part_note_id_ord",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Integer, sparse OK. The service layer normalises (0, 1, 2, ...)
    # on every reorder so reading by ord still gives a tidy sequence.
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    # Optional user-facing label for the part. NULL = no label set;
    # the SPA falls back to the first non-empty line of ``body`` so
    # the historical "untitled blocks" UX still works for legacy data.
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # ISO 639-1 (e.g. "en", "it") or longer subtag ("pt-BR"). NULL =
    # unspecified / mixed; the SPA can't yet route retrieval by lang
    # in v1, so a NULL is harmless.
    lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Who wrote this block (an Identity: a user or an ai_assistant,
    # ADR-0028). Migration 0010. A task's work note is where several
    # agents append while the work is still running, and the first
    # question the next one asks is not what the note says but which
    # blocks are somebody else's. NULL for every part written before
    # the column existed, and for anything the writer chose not to
    # attribute: inventing an author would be worse than a null.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Provenance after merge_notes: the source note's id stays here
    # so an audit reader can trace where the body originated even
    # after the source note is soft-deleted.
    merged_from_note_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )


class NotePartTrash(UUIDPKMixin, OrgScopedMixin, Base):
    """A trashed part, waiting to be restored or purged (migration 0089).

    The restorable half of part deletion. ``id`` is the ORIGINAL part's
    id (supplied by the service, not defaulted) so a restore puts the
    same part back rather than a copy: ids referenced elsewhere resolve
    again after the round trip.

    No ``VersionMixin``: a trashed part is not editable, and the version
    the row carried at trash time is preserved verbatim in
    ``part_version`` so restoring resumes the sequence instead of
    resetting it -- an ``expected_version`` captured before the trash
    must still lose after the restore.

    Lives in its own table rather than as a ``deleted_at`` flag on
    ``note_part`` because ``uq_note_part_note_id_ord`` has to stay
    DEFERRABLE for the insert-at-ord shift, and Postgres has no partial
    deferrable UNIQUE constraint to exclude tombstones from it.
    """

    __tablename__ = "note_part_trash"

    note_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The ord held at trash time. Restore aims for it and shifts the
    # survivors aside if the slot was taken in the meantime.
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    merged_from_note_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    part_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    trashed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # No FK: the actor row can be purged while the entry is still
    # restorable, and the attribution is audit sugar.
    trashed_by: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)


class NotePartUIState(Base):
    """Per-user collapse state. No row = expanded; default behaviour
    materialised lazily on the first toggle. The PK is (user_id,
    part_id) so a user only ever has one state per part; the FK
    cascade on ``part_id`` cleans up when the part disappears."""

    __tablename__ = "note_part_ui_state"
    __table_args__ = (PrimaryKeyConstraint("user_id", "part_id", name="pk_note_part_ui_state"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    part_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("note_part.id", ondelete="CASCADE"),
        nullable=False,
    )
    collapsed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
