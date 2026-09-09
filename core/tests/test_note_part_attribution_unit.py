"""A shared work note has to say who wrote each block, and when.

No session and no database: these are the pure dict-builders the MCP
note tools return, so what they drop can be asserted directly.

Why it matters, and it is two uses at once. A task's work note is the
scratch surface for work in progress -- the rough copy that outlives a
session whose context gets compacted away -- and it is also where
several agents append while the work is still running. Both ask the
same question of a part first, and neither is "what does it say":

  * resuming after compaction: which blocks arrived since I last looked?
  * a second agent joining: which of these are somebody else's?

``NotePart`` has carried ``created_at``/``updated_at`` since it was
written, and ``created_by`` since migration 0010. Both projections
dropped all three, so the only way to answer either question was to
pull every body into context -- which is exactly the cost the outline
exists to avoid.

These are regression tests, not coverage: each asserts a specific field
survives the projection, because the failure mode is a field going
quietly missing again in a serialiser nobody re-reads.
"""

from __future__ import annotations

import datetime
import uuid

from mycelium_core.models.note_part import NotePart
from mycelium_mcp.server import _note_part, _note_part_outline

_T0 = datetime.datetime(2026, 9, 9, 8, 0, tzinfo=datetime.UTC)
_T1 = datetime.datetime(2026, 9, 9, 9, 30, tzinfo=datetime.UTC)


def _part(
    *,
    body: str = "a block",
    author: uuid.UUID | None = None,
    created: datetime.datetime | None = _T0,
    updated: datetime.datetime | None = _T0,
) -> NotePart:
    p = NotePart(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        note_id=uuid.uuid4(),
        ord=0,
        title=None,
        body=body,
        lang="it",
        created_by=author,
        version=1,
    )
    p.created_at = created
    p.updated_at = updated
    return p


class TestTheFullProjection:
    def test_a_part_says_who_wrote_it(self) -> None:
        author = uuid.uuid4()

        assert _note_part(_part(author=author))["created_by"] == str(author)

    def test_a_part_says_when_it_was_written_and_last_touched(self) -> None:
        out = _note_part(_part(created=_T0, updated=_T1))

        assert out["created_at"] == _T0.isoformat()
        assert out["updated_at"] == _T1.isoformat()

    def test_an_unattributed_part_says_null_rather_than_omitting_the_key(self) -> None:
        """The distinction the fix rests on. A missing key reads as "this
        surface does not carry authorship" and sends the reader off to
        guess; an explicit null reads as "nobody recorded one", which is
        the truth for every block written before migration 0010."""
        out = _note_part(_part(author=None))

        assert "created_by" in out
        assert out["created_by"] is None


class TestTheOutline:
    """The outline is the one that has to carry these, because it is the
    projection a caller uses precisely when it does NOT want the bodies."""

    def test_the_outline_carries_authorship_without_the_body(self) -> None:
        author = uuid.uuid4()
        out = _note_part_outline(_part(body="x" * 5000, author=author, created=_T1))

        assert out["created_by"] == str(author)
        assert out["created_at"] == _T1.isoformat()
        assert "body" not in out
        assert out["bytes"] == 5000

    def test_an_agent_can_pick_out_what_is_new_from_the_outline_alone(self) -> None:
        """The operation both uses perform first, done here the way a
        caller would: sort by ``created_at``, keep what is newer than the
        last block already seen. No body is fetched to do it."""
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        seen = _T0
        outline = [
            _note_part_outline(_part(body="old", author=mine, created=_T0)),
            _note_part_outline(_part(body="new", author=theirs, created=_T1)),
        ]

        fresh = [p for p in outline if p["created_at"] > seen.isoformat()]

        assert [p["head"] for p in fresh] == ["new"]
        assert fresh[0]["created_by"] == str(theirs)

    def test_and_can_tell_its_own_blocks_from_another_agent_s(self) -> None:
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        outline = [
            _note_part_outline(_part(body="mine", author=mine)),
            _note_part_outline(_part(body="theirs", author=theirs)),
        ]

        assert [p["head"] for p in outline if p["created_by"] != str(mine)] == ["theirs"]


def test_neither_projection_has_lost_a_field_it_used_to_carry() -> None:
    """One assertion over each whole shape, so a future field added and
    forgotten fails loudly instead of being invisible for a release --
    and so this change cannot have dropped something on the way in."""
    full = set(_note_part(_part()))
    outline = set(_note_part_outline(_part()))

    assert full == {
        "id",
        "note_id",
        "ord",
        "body",
        "lang",
        "merged_from_note_id",
        "version",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert outline == {
        "id",
        "note_id",
        "ord",
        "title",
        "lang",
        "bytes",
        "head",
        "version",
        "created_by",
        "created_at",
        "updated_at",
    }
