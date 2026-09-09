"""A credential the browser panel holds follows its holder between
workspaces, and carries no authority of its own.

The panel is one surface over an account, not one per workspace: a person
moves between their workspaces on a single login and expects the thing
sitting over their browser to move with them. Making them connect once
per workspace would have put one long-lived secret per workspace in the
same Chrome profile, for no gain in what any of them may do.

So this credential is bound to the ACCOUNT, and the whole of the design
is in what that does not change. The workspace still arrives per request.
The holder's membership there is still resolved per request and still
refuses a workspace they do not belong to. The effective role is still
clamped to that membership, so the credential is a guest where its holder
is a guest and cannot become more by asking. The scope list still applies
on top. What widens is the reach of the secret, and nothing else -- which
is the thing the consent screen has to say, and does.

The sibling file ``test_agent_token_confinement.py`` asserts the other
half: a credential that did NOT ask for this is refused outside the
workspace it was minted for, exactly as before.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app

# The fixed, narrow set the browser panel asks for. Anything wider is an
# assistant, and an assistant is owner-gated and workspace-bound.
PANEL_SCOPE = [
    "tasks:read",
    "tasks:write",
    "tasks:state",
    "notes:read",
    "notes:write",
    "tags:read",
    "tags:assign",
    "workflows:read",
    "search:read",
    "search:write",
    "attachments:write",
]


async def _signup(c: AsyncClient, name: str) -> tuple[dict[str, str], str]:
    email = f"{uuid.uuid4().hex[:10]}@example.test"
    su = (
        await c.post(
            "/auth/signup",
            json={"email": email, "password": "pw-strong-123", "workspace_name": name},
        )
    ).json()
    headers = {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }
    return headers, email


async def _second_workspace(c: AsyncClient, owner: dict[str, str]) -> str:
    created = await c.post("/workspaces", headers=owner, json={"name": "Second"})
    assert created.status_code in (200, 201), created.text
    return str(created.json()["id"])


async def _connect_panel(
    c: AsyncClient,
    headers: dict[str, str],
    *,
    scope: list[str] | None = None,
    binding: str = "account",
) -> tuple[int, str]:
    created = await c.post(
        "/ai-assistants",
        headers=headers,
        json={
            "label": "Browser",
            "provider": "mycelium-extension",
            "scope": scope if scope is not None else PANEL_SCOPE,
            "workspace_binding": binding,
        },
    )
    if created.status_code not in (200, 201):
        return created.status_code, created.text
    return created.status_code, str(created.json()["raw_secret"])


async def test_the_panel_credential_follows_its_holder_into_their_other_workspace() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner, _ = await _signup(c, "First")
        first = owner["X-Workspace-Id"]
        second = await _second_workspace(c, owner)
        status, secret = await _connect_panel(c, owner)
        assert status in (200, 201), secret

        headers = {"Authorization": f"Bearer {secret}"}
        here = await c.get("/tasks", headers={**headers, "X-Workspace-Id": first})
        assert here.status_code == 200, here.text
        there = await c.get("/tasks", headers={**headers, "X-Workspace-Id": second})
        assert there.status_code == 200, there.text


async def test_it_is_refused_in_a_workspace_its_holder_does_not_belong_to() -> None:
    """The reach is the HOLDER's, not the credential's. A workspace they
    were never added to is refused for the same reason their own session
    would be: there is no membership to authorize against."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        mine, _ = await _signup(c, "Mine")
        stranger, _ = await _signup(c, "Theirs")
        status, secret = await _connect_panel(c, mine)
        assert status in (200, 201), secret

        res = await c.get(
            "/tasks",
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Workspace-Id": stranger["X-Workspace-Id"],
            },
        )
        assert res.status_code == 403, res.text


async def test_it_is_a_guest_where_its_holder_is_a_guest() -> None:
    """The one that would make this a privilege escalation if it failed.

    The holder owns one workspace and was invited into another as a
    guest. The same credential must be able to read there and must not be
    able to write, because the person cannot: the effective role is
    clamped to the membership of the workspace the request names, never
    to the one the credential was minted in."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        mine, my_email = await _signup(c, "Mine")
        host, _ = await _signup(c, "Host")
        added = await c.post(
            "/workspaces/me/members",
            headers=host,
            json={"email": my_email, "role": "guest"},
        )
        assert added.status_code in (200, 201), added.text
        theirs = host["X-Workspace-Id"]

        status, secret = await _connect_panel(c, mine)
        assert status in (200, 201), secret
        headers = {"Authorization": f"Bearer {secret}", "X-Workspace-Id": theirs}

        # In its own workspace the holder is the owner, so the credential
        # writes there within its scope.
        wrote_here = await c.post(
            "/tasks",
            headers={"Authorization": f"Bearer {secret}", "X-Workspace-Id": mine["X-Workspace-Id"]},
            json={"title": "mine"},
        )
        assert wrote_here.status_code in (200, 201), wrote_here.text

        # As a guest elsewhere it may not, and the refusal is about the
        # role rather than about the credential.
        wrote_there = await c.post("/tasks", headers=headers, json={"title": "theirs"})
        assert wrote_there.status_code == 403, wrote_there.text


async def test_the_scope_list_still_applies_in_every_workspace() -> None:
    """Reaching further is not doing more. A route outside the panel's
    scope is refused wherever it is called."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner, _ = await _signup(c, "First")
        second = await _second_workspace(c, owner)
        status, secret = await _connect_panel(c, owner)
        assert status in (200, 201), secret

        for workspace in (owner["X-Workspace-Id"], second):
            res = await c.get(
                "/ai-assistants",
                headers={"Authorization": f"Bearer {secret}", "X-Workspace-Id": workspace},
            )
            # HUMAN_ONLY: a credential never reads the list of credentials.
            assert res.status_code == 403, res.text


async def test_account_reach_is_refused_for_anything_wider_than_the_panel() -> None:
    """The reach is offered for the narrow set and no other. Asking for
    account reach with a wider scope is refused outright rather than
    quietly narrowed: a client told "connected" would otherwise discover
    the truth one workspace later."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner, _ = await _signup(c, "First")
        status, body = await _connect_panel(
            c, owner, scope=[*PANEL_SCOPE, "tags:write"], binding="account"
        )
        assert status == 400, body
        assert "ai_assistant.binding_too_wide" in body
        # The refusal names the key that made it too wide, not the field
        # it was asked through: what the caller has to change is the
        # scope, and an error that says "workspace_binding" sends them to
        # edit the one thing that was right.
        assert "tags:write" in body


async def test_a_member_may_connect_their_own_browser() -> None:
    """The page has always said installing the panel is not an
    administrative act while the server refused anyone but the owner.

    Worse in practice than it reads: the SPA acts as ``member`` by
    default and switches up only on request, so the owner met the refusal
    too, on their own workspace, with no hint that a role lever was the
    thing in the way."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        host, _ = await _signup(c, "Host")
        joiner, joiner_email = await _signup(c, "Joiner")
        added = await c.post(
            "/workspaces/me/members",
            headers=host,
            json={"email": joiner_email, "role": "member"},
        )
        assert added.status_code in (200, 201), added.text

        # As a plain member of somebody else's workspace, with no role
        # header at all -- which is what the SPA sends by default.
        member_headers = {
            "Authorization": joiner["Authorization"],
            "X-Workspace-Id": host["X-Workspace-Id"],
        }
        status, secret = await _connect_panel(c, member_headers)
        assert status in (200, 201), secret

        # And a wider credential is still the owner's to create.
        wider, body = await _connect_panel(
            c, member_headers, scope=[*PANEL_SCOPE, "tags:write"], binding="workspace"
        )
        assert wider == 403, body


async def test_a_credential_can_ask_which_workspaces_it_may_act_in() -> None:
    """It has to be able to: a credential that reaches several workspaces
    cannot put one in the header before it knows which ones exist for it.
    A confined credential gets the one it was minted for, so the same
    call answers both shapes."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner, _ = await _signup(c, "First")
        first = owner["X-Workspace-Id"]
        second = await _second_workspace(c, owner)

        status, panel = await _connect_panel(c, owner)
        assert status in (200, 201), panel
        res = await c.get("/agent/workspaces", headers={"Authorization": f"Bearer {panel}"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["binding"] == "account"
        assert {w["id"] for w in body["workspaces"]} == {first, second}

        status, confined = await _connect_panel(c, owner, binding="workspace")
        assert status in (200, 201), confined
        res = await c.get("/agent/workspaces", headers={"Authorization": f"Bearer {confined}"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["binding"] == "workspace"
        assert [w["id"] for w in body["workspaces"]] == [first]
