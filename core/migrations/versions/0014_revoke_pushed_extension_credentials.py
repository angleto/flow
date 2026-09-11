"""End the credentials the old extension handshake pushed into browsers.

The clean cut that goes with removing that handshake. Migration 0013 added
the device authorization grant beside it; this release deletes the page
that minted a credential and pushed it over ``externally_connectable``,
and the extension that received one that way. What is left behind is the
credentials themselves, and a credential outliving the mechanism that
issued it is the problem, not a leftover detail:

- they were minted with a 365-day life, so without this they would be
  usable for most of a year by an extension build that can no longer be
  produced;
- they were pushed rather than collected, so nothing recorded that the
  holder actually received one -- a refused handover left a live
  credential nobody held, which is one of the three defects the new flow
  exists to make unrepresentable;
- and the alternative, letting both mechanisms run until the last one
  lapses, means maintaining and reasoning about two authentication paths
  for a year to save one two-click ceremony.

So they are revoked, and everyone using the extension connects once more.
An interruption chosen deliberately, announced, and cheap: the new
ceremony is the one this release ships.

REVOKED, NOT DELETED. ``revoked_at`` is what the verifier reads
(migration 0012's ``authenticate_agent_token`` refuses on it), and the
assistant row stays so the person still sees what they had in the list of
browsers and can tell the difference between "this was ended" and "this
was never there". Deleting would also cascade the device-authorization
rows that recorded who approved what.

Scoped by PROVIDER, which is the only thing that distinguishes these rows:
``mycelium-extension`` is written by the server on the extension path and
nowhere else, so nothing minted for an MCP client or a cron integration is
touched. Already-revoked rows keep their original ``revoked_at`` -- the
guard is in the WHERE, so re-running this rewrites no history.

Idempotent and non-transactional in effect: running it twice revokes
nothing the second time.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE agent_tokens AS t
           SET revoked_at = now(), updated_at = now()
          FROM ai_assistants AS a
         WHERE a.id = t.assistant_id
           AND a.provider = 'mycelium-extension'
           AND t.revoked_at IS NULL
        """
    )
    # The assistant row is what the settings page lists, and a row that
    # still reads as active while its credential is dead would tell the
    # reader they are connected when they are not. Deactivated rather than
    # deleted: it is the record of a browser that WAS connected, and the
    # person recognises it when they reconnect.
    op.execute(
        """
        UPDATE ai_assistants
           SET is_active = false, updated_at = now()
         WHERE provider = 'mycelium-extension'
           AND is_active
        """
    )


def downgrade() -> None:
    # Deliberately empty, and this is a decision rather than laziness.
    #
    # Un-revoking cannot be correct: this migration does not record which
    # rows it changed, so a reversal would have to resurrect every
    # extension credential ever revoked, including the ones a person
    # revoked on purpose because they lost a machine. Restoring access to
    # a browser somebody deliberately cut off is a worse failure than
    # having no downgrade.
    #
    # The way back is forward: connect again, which takes two clicks.
    pass
