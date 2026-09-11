"""An open request to let a device in, approved from a session that is
already signed in.

The device authorization grant (RFC 8628), reduced to what a first-party
surface needs. A device with no keyboard worth typing a password on opens
a request, shows a short code, and waits; the person approves in the
application they are already inside; the device collects what it was
granted.

Three properties decide the shape of this table, and each one is a defect
in the mechanism it replaces:

**The request lives here, not in a URL.** The browser extension's previous
handshake carried its request in the query string of the page it opened,
so any redirect between that URL and the person could destroy it -- and
the login redirect did, which is how a connection that was working became
"the extension is broken". A row keyed by a code the device is holding
cannot be lost by a navigation.

**Two codes, because they do different jobs.** ``user_code`` is read aloud
by a human and compared against what the device is showing, so it is short
and drawn from an alphabet with no letter that can be mistaken for a
digit; on its own it collects nothing. ``device_code_hash`` is the only
thing that can collect, it is never shown, and only its digest is stored
-- the same discipline as ``agent_tokens`` and ``refresh_tokens``, so
reading this table gives nobody a credential.

**Nothing is minted until it is collected.** Approval records WHO approved
and WHERE; the credential itself is created in the transaction that marks
this row redeemed. The mechanism this replaces minted first and handed
over second, so every refused handover left a live credential nobody held.
Here that state has no representation: no approved-but-uncollected secret
exists to be orphaned, and no secret is ever at rest between the two
halves.

Pre-tenant, like ``refresh_tokens`` next door: at the moment a request is
opened there is no user and no workspace yet, so there is no tenant to
scope it to and no row-level policy that could be written. What protects
it is that ``device_code_hash`` is unguessable and every read of this
table is by digest.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, TimestampMixin, UUIDPKMixin


class DeviceAuthorization(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "device_authorizations"

    # SHA-256 hex of the long secret the device holds. The only key that
    # collects anything, and the only one this table is ever read by in
    # the collection path.
    device_code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # What the person compares, in the display grouping ("K7QP-3MTX").
    # Unique across OPEN requests, enforced by a partial index in the
    # migration rather than a plain constraint: a code must be free to be
    # issued again once the request that held it is finished.
    user_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Which of our own surfaces opened this. One value today
    # ("extension"); named rather than assumed so the approval screen can
    # say what is asking, and so a second surface does not have to
    # discover that this column was needed.
    client: Mapped[str] = mapped_column(String(32), nullable=False)

    expires_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # The three outcomes, each with its moment. Kept apart rather than
    # collapsed into a status enum: the questions asked of this row are
    # "has anybody answered", "was it a yes", and "has it been collected",
    # and a timestamp answers each one while also saying when.
    approved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    denied_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    redeemed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Who answered, yes or no. Named for the ANSWER rather than for the
    # approval because a refusal has an author too, and a denial nobody
    # can attribute is the one an audit cannot use.
    answered_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    # The workspace the person was in when they approved. Meaningful
    # only alongside ``approved_at``: a refusal is not made in a
    # workspace, it is made about a request.
    approved_org_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )

    # What the collection produced, so an approval can be traced to the
    # credential it created without joining on time. NULL before
    # collection, and NULL forever on a request nobody collected.
    assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_assistants.id", ondelete="SET NULL"),
        nullable=True,
    )

    # For the approval screen: "asking since", and from where. The
    # address is the one the server saw, never one the client declared.
    # It is here because a person deciding whether to approve is deciding
    # about a machine, and "a request opened four seconds ago from this
    # address" is the evidence they have.
    origin_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    def is_open(self, now: datetime.datetime) -> bool:
        """Still waiting for an answer. Not a status column, because the
        answer depends on the clock as much as on the row."""
        return (
            self.approved_at is None
            and self.denied_at is None
            and self.redeemed_at is None
            and self.expires_at > now
        )


__all__ = ["DeviceAuthorization"]
