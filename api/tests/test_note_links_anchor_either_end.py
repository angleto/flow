"""An edge is the same edge from either of its two notes.

The store writes a note↔note edge as an ordered pair, and for the
undirected kinds that order is a canonicalisation: the service sorts the
two ids and writes the smaller one as parent, so ``related`` edges land
with the anchor as child exactly half the time, by id comparison.

The routes used to take the parent from the path, which made the anchor
the parent by construction. Creating "this note grew from that one" was
then a 400, and removing an edge the GET had just listed was a 404
whenever the canonical order put the anchor second -- the asymmetry a
user meets as "the link shows on that note but not on this one".

So both endpoints travel explicitly and the path id is only required to
be one of them. These tests pin that from both sides of the same pair,
choosing the endpoints by sort order so the canonicalisation is exercised
in the direction that used to fail rather than by luck of the uuid4 draw.
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123"},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _make_note(c: AsyncClient, h: dict[str, str], title: str) -> str:
    r = await c.post(
        "/notes",
        headers=h,
        json={"kind": "text", "title": title, "text": f"body of {title}"},
    )
    assert r.status_code == 200, r.text
    return str(r.json()["id"])


async def _two_notes_sorted(c: AsyncClient, h: dict[str, str]) -> tuple[str, str]:
    """Return (low, high) by id string: the order the service imposes on
    an undirected pair, so ``high`` is the endpoint stored as child."""
    a = await _make_note(c, h, "alpha")
    b = await _make_note(c, h, "beta")
    return (a, b) if a < b else (b, a)


async def _links(c: AsyncClient, h: dict[str, str], note_id: str) -> dict[str, Any]:
    r = await c.get(f"/notes/{note_id}/links", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_an_undirected_link_is_listed_from_both_of_its_notes() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        low, high = await _two_notes_sorted(c, h)

        r = await c.post(
            f"/notes/{high}/links",
            headers=h,
            json={"parent_note_id": high, "child_note_id": low, "kind": "related"},
        )
        assert r.status_code == 200, r.text
        # Canonicalised: the smaller id is the parent, whatever was sent.
        assert r.json()["parent_note_id"] == low
        assert r.json()["child_note_id"] == high

        from_low = await _links(c, h, low)
        from_high = await _links(c, h, high)
        assert [x["child_note_id"] for x in from_low["outgoing"]] == [high]
        assert [x["parent_note_id"] for x in from_high["incoming"]] == [low]
        # The edge is one edge: each note sees it exactly once, in one of
        # the two orientation buckets.
        assert len(from_low["outgoing"]) + len(from_low["incoming"]) == 1
        assert len(from_high["outgoing"]) + len(from_high["incoming"]) == 1


async def test_an_undirected_link_is_removable_from_the_endpoint_stored_as_child() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        low, high = await _two_notes_sorted(c, h)
        await c.post(
            f"/notes/{low}/links",
            headers=h,
            json={"parent_note_id": low, "child_note_id": high, "kind": "related"},
        )

        r = await c.delete(
            f"/notes/{high}/links",
            headers=h,
            params={"parent_note_id": low, "child_note_id": high, "kind": "related"},
        )
        assert r.status_code == 204, r.text
        for endpoint in (low, high):
            seen = await _links(c, h, endpoint)
            assert seen["outgoing"] == []
            assert seen["incoming"] == []


async def test_a_directional_link_can_be_created_from_the_child_side() -> None:
    """The claim "this note grew from that one" is the same edge as
    "that note sprouted this one", stated from the other end. The panel offers it as
    a swap, and the route must accept the anchor as the child."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        origin = await _make_note(c, h, "origin")
        derived = await _make_note(c, h, "derived")

        r = await c.post(
            f"/notes/{derived}/links",
            headers=h,
            json={
                "parent_note_id": origin,
                "child_note_id": derived,
                "kind": "hypha_of",
            },
        )
        assert r.status_code == 200, r.text
        # Directional: the order stands, it is not canonicalised.
        assert r.json()["parent_note_id"] == origin
        assert r.json()["child_note_id"] == derived

        r = await c.delete(
            f"/notes/{derived}/links",
            headers=h,
            params={
                "parent_note_id": origin,
                "child_note_id": derived,
                "kind": "hypha_of",
            },
        )
        assert r.status_code == 204, r.text


async def test_the_anchor_must_be_one_of_the_two_endpoints() -> None:
    """The path id is not decorative: it is the note the caller has open,
    and an edge between two other notes is not addressable through it."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        a = await _make_note(c, h, "alpha")
        b = await _make_note(c, h, "beta")
        outsider = await _make_note(c, h, "outsider")

        r = await c.post(
            f"/notes/{outsider}/links",
            headers=h,
            json={"parent_note_id": a, "child_note_id": b, "kind": "related"},
        )
        assert r.status_code == 400, r.text

        await c.post(
            f"/notes/{a}/links",
            headers=h,
            json={"parent_note_id": a, "child_note_id": b, "kind": "related"},
        )
        r = await c.delete(
            f"/notes/{outsider}/links",
            headers=h,
            params={"parent_note_id": a, "child_note_id": b, "kind": "related"},
        )
        assert r.status_code == 400, r.text
