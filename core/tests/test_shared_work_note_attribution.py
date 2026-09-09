"""Two agents on one task's work note, end to end.

The unit tests beside this one pin that the serialisers carry
``created_by``; this one pins that the value written is the right one
and that the operation it exists for actually works against the
database.

The operation: a second worker arrives at a task already in progress,
and wants the blocks that appeared since it last looked, and whose they
are, WITHOUT reading the ones it already has. That is what makes a work
note usable both as a scratchpad that outlives a compacted session and
as the surface several agents append to while the work is still running.

The trap this catches, and it is the one that broke twelve tests when
the column first went in: ``created_by`` is an Identity (ADR-0028), not
the ``users`` row that ``actor_id`` names. Writing the actor straight
into the column is a foreign key violation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import set_embedder_override
from mycelium_core.models.identity import Identity
from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_part import NotePart
from mycelium_core.services import identities as identities_svc
from mycelium_core.services import note_parts as parts_svc
from mycelium_core.services import notes as nt
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup
from mycelium_core.services.memberships import add_member


@pytest.fixture
def _embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="SHAREDWN")
    return r.org_id, r.user_id


async def _identity_of(org: uuid.UUID, user: uuid.UUID) -> uuid.UUID | None:
    async with admin_session() as s:
        return (
            await s.execute(
                select(Identity.id).where(Identity.org_id == org, Identity.user_id == user)
            )
        ).scalar_one_or_none()


async def _with_identity(org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    """Give the actor an Identity in this org and return its id.

    ``signup`` does not mint one, which is worth knowing rather than
    working around: an actor without an identity writes a part with a
    NULL author, and that is the designed behaviour -- attribution is
    worth recording and is not worth refusing a write for. These tests
    are about the attributed case, so they create the row explicitly.
    """
    async with tenant_session(str(org), str(user)) as s:
        ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=user)
        return ident.id


async def _member_of(org: uuid.UUID, owner: uuid.UUID) -> uuid.UUID:
    """A second person in the SAME org, added the way the product does."""
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="Other")
    async with admin_session() as s:
        from mycelium_core.models.user import User as UserModel

        email = (
            (await s.execute(select(UserModel).where(UserModel.id == r.user_id))).scalar_one().email
        )
    async with tenant_session(str(org), str(owner)) as s:
        await add_member(s, org_id=org, actor_id=owner, email=email, role="member")
    return r.user_id


async def _parts(org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> list[NotePart]:
    async with tenant_session(str(org), str(user)) as s:
        return await parts_svc.list_parts(s, org_id=org, note_id=note_id)


async def test_a_block_records_the_identity_that_wrote_it(_embedder: None) -> None:
    """Not the ``users`` id. The FK is to ``identities`` and the
    resolution is what the service has to do; asserting the value rather
    than merely that the insert succeeded is what tells the two apart."""
    org, user = await _org()
    identity = await _with_identity(org, user)

    async with tenant_session(str(org), str(user)) as s:
        note = await nt.create_note(s, org_id=org, actor_id=user, kind=NoteKind.text, text="first")
        note_id = note.id
        await parts_svc.create_part(s, org_id=org, actor_id=user, note_id=note_id, body="second")

    parts = await _parts(org, user, note_id)
    assert [p.body for p in parts] == ["first", "second"]
    written = next(p for p in parts if p.body == "second")
    assert written.created_by == identity
    assert written.created_by != user


async def test_a_second_worker_can_pick_out_what_is_new_and_whose(_embedder: None) -> None:
    """The whole point, run against the database.

    Two members of one org append to the same task's work note. The
    second one asks the question it actually has -- what arrived since I
    last looked, and which of it is not mine -- and answers it from the
    part metadata alone.
    """
    org, alice = await _org()
    bob = await _member_of(org, alice)
    alice_identity = await _with_identity(org, alice)
    bob_identity = await _with_identity(org, bob)

    async with tenant_session(str(org), str(alice)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=alice, title="brutta: a piece")
        task_id = task.id
    async with tenant_session(str(org), str(alice)) as s:
        note = await nt.get_or_create_work_note(s, org_id=org, actor_id=alice, task_id=task_id)
        note_id = note.id
        await parts_svc.create_part(
            s, org_id=org, actor_id=alice, note_id=note_id, body="alice one"
        )

    seen = max(p.created_at for p in await _parts(org, alice, note_id))

    async with tenant_session(str(org), str(bob)) as s:
        await parts_svc.create_part(s, org_id=org, actor_id=bob, note_id=note_id, body="bob one")
        await parts_svc.create_part(s, org_id=org, actor_id=bob, note_id=note_id, body="bob two")

    parts = await _parts(org, alice, note_id)
    fresh = [p for p in parts if p.created_at > seen]

    assert [p.body for p in fresh] == ["bob one", "bob two"]
    assert {p.created_by for p in fresh} == {bob_identity}
    assert [p.body for p in parts if p.created_by == alice_identity] == ["alice one"]


async def test_the_work_note_is_the_same_note_on_every_call(_embedder: None) -> None:
    """``get_or_create_work_note`` is what makes the task id enough: an
    agent that only knows the task finds the same surface the others are
    writing on, without being told a note id."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="brutta: idempotent")
        task_id = task.id

    async with tenant_session(str(org), str(user)) as s:
        first = await nt.get_or_create_work_note(s, org_id=org, actor_id=user, task_id=task_id)
        first_id = first.id
    async with tenant_session(str(org), str(user)) as s:
        second = await nt.get_or_create_work_note(s, org_id=org, actor_id=user, task_id=task_id)
        assert second.id == first_id
