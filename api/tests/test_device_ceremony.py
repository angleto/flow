"""The whole ceremony, over HTTP, in the order it actually happens.

The two halves are already covered apart: the service in
``core/tests/test_device_authorization_service.py``, and the extension's
side in ``extension/tests/linking.test.ts`` against a fake server. Both
were green while the mechanism they replaced was broken, which is the
argument for this file. The defect that started this work lived in a JOIN
-- a guard that dropped an address and a form that ignored it, each
defensible alone -- and the join is the thing neither half can assert.

So this drives the real routes, in sequence, as the three parties do:

    a device with nothing          POST /auth/device/authorize
    a person with a session        GET  /auth/device/pending
                                   POST /auth/device/approve
    the device, still asking       POST /auth/device/token

and then uses what came out, because a credential that cannot be used is
not a credential. The last step is the one a mock cannot fake: the secret
this ceremony produced authenticates against an ordinary endpoint, at the
authority the person had, and is refused past it.

What this file does NOT cover, said plainly: the extension's own code does
not run here. What it sends and reads is held to the same contract by the
compiler -- ``linking.ts`` consumes the generated schema types rather than
a hand-written copy -- which is a stronger guarantee than a test and a
different one from this.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.mcp_scopes import EXTENSION_SCOPES


async def _signup(c: AsyncClient, name: str = "Studio") -> dict[str, str]:
    email = f"{uuid.uuid4().hex[:10]}@example.test"
    su = (
        await c.post(
            "/auth/signup",
            json={"email": email, "password": "pw-strong-123", "workspace_name": name},
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
    }


async def _open(c: AsyncClient) -> dict[str, str]:
    opened = await c.post("/auth/device/authorize", json={"client": "extension"})
    assert opened.status_code == 200, opened.text
    return opened.json()


async def test_the_whole_ceremony_and_the_credential_it_produces() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        person = await _signup(c)
        workspace = person["X-Workspace-Id"]

        # 1. The device asks, holding nothing. No Authorization header,
        #    and that is the point: this is who is calling.
        opened = await _open(c)
        assert opened["user_code"] and opened["device_code"]
        assert opened["device_code"] != opened["user_code"]
        # A PATH, never an absolute URL: the device knows which deployment
        # it talks to, and a response that could name a host would be able
        # to send its holder somewhere else.
        assert opened["verification_path"].startswith("/")
        assert "://" not in opened["verification_path"]

        # 2. Asking again before anybody answers is NOT success. A 2xx
        #    here is the bug that ships: every HTTP client reads one as
        #    "it worked", and a client that stores an empty answer
        #    believes itself connected.
        waiting = await c.post("/auth/device/token", json={"device_code": opened["device_code"]})
        assert waiting.status_code == 400, waiting.text
        assert waiting.json()["code"] == "auth.device_pending"

        # 3. The person looks it up by the SHORT code and is shown what to
        #    decide about, including the code to compare.
        pending = await c.get(
            "/auth/device/pending",
            headers=person,
            params={"user_code": opened["user_code"]},
        )
        assert pending.status_code == 200, pending.text
        shown = pending.json()
        assert shown["user_code"] == opened["user_code"]
        assert shown["client"] == "extension"
        # The grant is the SERVER's, so the screen cannot disclose one
        # thing while the mint does another.
        assert shown["scope"] == list(EXTENSION_SCOPES)
        # And the long code is nowhere in what a person is shown.
        assert opened["device_code"] not in pending.text

        # 4. They approve. This mints NOTHING -- asserted below by what
        #    the collection produces, and here by the fact that the row
        #    the settings page lists does not exist yet.
        before = await c.get("/ai-assistants", headers=person)
        assert before.status_code == 200
        assert [a for a in before.json() if a["provider"] == "mycelium-extension"] == []

        approved = await c.post(
            "/auth/device/approve", headers=person, json={"user_code": opened["user_code"]}
        )
        assert approved.status_code == 204, approved.text

        # 5. The device, still asking, collects. The credential comes into
        #    existence here.
        collected = await c.post("/auth/device/token", json={"device_code": opened["device_code"]})
        assert collected.status_code == 200, collected.text
        granted = collected.json()
        assert granted["secret"].startswith("mycelium_at_")
        assert granted["workspace_id"] == workspace
        assert granted["workspace_name"]
        assert granted["scope"] == list(EXTENSION_SCOPES)
        assert granted["expires_at"] is not None

        # 6. And it WORKS, which is the half no mock can stand in for.
        panel = {"Authorization": f"Bearer {granted['secret']}", "X-Workspace-Id": workspace}
        tasks = await c.get("/tasks", headers=panel)
        assert tasks.status_code == 200, tasks.text

        # 7. At the authority of the person, and no further. Reading the
        #    account row is HUMAN_ONLY: a scoped credential is refused
        #    there however it was obtained.
        me = await c.get("/auth/me", headers=panel)
        assert me.status_code == 403, me.text

        # 8. The person now sees the browser in their list, with the
        #    credential the collection produced.
        after = await c.get("/ai-assistants", headers=person)
        rows = [a for a in after.json() if a["provider"] == "mycelium-extension"]
        assert len(rows) == 1
        assert rows[0]["id"] == granted["assistant_id"]


async def test_a_device_code_collects_once_and_the_replay_produces_nothing() -> None:
    """The claim and the mint are one transaction, so a second collection
    is not a second credential. Asserted over HTTP because that is where
    a retry actually arrives: a device whose first answer was lost asks
    again, and must not walk away with two secrets."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        person = await _signup(c)
        opened = await _open(c)
        await c.post(
            "/auth/device/approve", headers=person, json={"user_code": opened["user_code"]}
        )

        first = await c.post("/auth/device/token", json={"device_code": opened["device_code"]})
        assert first.status_code == 200, first.text
        replay = await c.post("/auth/device/token", json={"device_code": opened["device_code"]})
        assert replay.status_code == 404, replay.text

        rows = (await c.get("/ai-assistants", headers=person)).json()
        assert len([a for a in rows if a["provider"] == "mycelium-extension"]) == 1


async def test_a_refusal_reaches_the_device_and_leaves_nothing_behind() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        person = await _signup(c)
        opened = await _open(c)

        denied = await c.post(
            "/auth/device/deny", headers=person, json={"user_code": opened["user_code"]}
        )
        assert denied.status_code == 204, denied.text

        # Told, rather than left to time out: the device stops asking
        # instead of spinning until the request lapses.
        after = await c.post("/auth/device/token", json={"device_code": opened["device_code"]})
        assert after.status_code == 400, after.text
        assert after.json()["code"] == "auth.device_denied"

        rows = (await c.get("/ai-assistants", headers=person)).json()
        assert [a for a in rows if a["provider"] == "mycelium-extension"] == []


async def test_answering_a_request_needs_a_person_and_the_credential_cannot_do_it() -> None:
    """The fence that makes the unauthenticated half safe.

    A scoped credential that could approve a device could open a request
    of its own and collect one, which is a laundering step out of its own
    scope. Both answering routes are HUMAN_ONLY for that reason, and this
    asserts it with a credential obtained through the ceremony itself --
    the most convincing holder available."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        person = await _signup(c)
        workspace = person["X-Workspace-Id"]
        opened = await _open(c)
        await c.post(
            "/auth/device/approve", headers=person, json={"user_code": opened["user_code"]}
        )
        granted = (
            await c.post("/auth/device/token", json={"device_code": opened["device_code"]})
        ).json()
        panel = {"Authorization": f"Bearer {granted['secret']}", "X-Workspace-Id": workspace}

        # It can open a request: that route is public and grants nothing.
        second = await _open(c)

        # It cannot see one, and cannot answer one.
        looked = await c.get(
            "/auth/device/pending", headers=panel, params={"user_code": second["user_code"]}
        )
        assert looked.status_code == 403, looked.text
        approved = await c.post(
            "/auth/device/approve", headers=panel, json={"user_code": second["user_code"]}
        )
        assert approved.status_code == 403, approved.text

        # And nothing was minted by trying.
        rows = (await c.get("/ai-assistants", headers=person)).json()
        assert len([a for a in rows if a["provider"] == "mycelium-extension"]) == 1


async def test_an_unanswered_request_is_absent_rather_than_described() -> None:
    """A short code that matches nothing open is reported the same way
    whether it never existed or was already answered. The distinction is
    only useful to somebody sweeping the space."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        person = await _signup(c)
        never = await c.get(
            "/auth/device/pending", headers=person, params={"user_code": "AAAA-AAAA"}
        )
        assert never.status_code == 404, never.text

        opened = await _open(c)
        await c.post(
            "/auth/device/approve", headers=person, json={"user_code": opened["user_code"]}
        )
        answered = await c.get(
            "/auth/device/pending", headers=person, params={"user_code": opened["user_code"]}
        )
        assert answered.status_code == never.status_code
        assert answered.json()["code"] == never.json()["code"]


async def test_abandoning_requests_is_bounded_and_completing_them_is_not() -> None:
    """The limit counts what is WAITING, not what has happened.

    Written because the first version counted every request an origin had
    opened in the window, so a completed ceremony consumed budget for ten
    minutes afterwards. That punishes using the feature, and it punishes
    a shared address -- an office, a household, a VPN exit are one network
    origin -- by charging it for its successes. It also took this suite
    down on its second consecutive run, which is how it was found."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        person = await _signup(c)
        # Several ceremonies, carried through. None of these should leave
        # anything behind that counts against the next one.
        for _ in range(4):
            opened = await _open(c)
            await c.post(
                "/auth/device/approve",
                headers=person,
                json={"user_code": opened["user_code"]},
            )
            collected = await c.post(
                "/auth/device/token", json={"device_code": opened["device_code"]}
            )
            assert collected.status_code == 200, collected.text

        # And one more still opens, which is the assertion: the budget was
        # never spent.
        again = await c.post("/auth/device/authorize", json={"client": "extension"})
        assert again.status_code == 200, again.text
