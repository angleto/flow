"""Two permissions were each doing two jobs. These assert the split held.

``workflows:write`` meant "advance one task" AND "create, edit and delete
the state machine every task in the workspace runs on". ``tags:write``
meant "file this task into a project that exists" AND "invent, rename and
rescope the vocabulary every entity carries". A client that needed the
small power had to be granted the large one, which is not a permission
system, it is a hint.

Both splits ship a TRANSITIONAL any-of set so an assistant granted the
wide key before the split keeps working. The risk that creates is that
the split becomes decorative: everything still works with the wide key,
so nothing ever moves off it, and the narrow key is never granted. The
sets carry a removal date; these tests are what makes collapsing them
safe, because they pin what the narrow key must and must not reach.

No database: these are assertions about the two maps and the two gates.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from mycelium_api.deps import require_agent_scope
from mycelium_api.main import app
from mycelium_api.route_scopes import (
    META,
    ROUTE_SCOPES,
    TAG_ASSIGN_ANY,
    TASK_STATE_ANY,
    scope_permits,
)
from mycelium_core.errors import ForbiddenError
from mycelium_core.mcp_scopes import VALID_SCOPE_KEYS
from mycelium_mcp.tool_scopes import required_keys

# What each narrow key exists to reach.
ADVANCE_A_TASK = ("POST", "/tasks/{task_id}/state")
FILE_A_TASK = [
    ("POST", "/tasks/{task_id}/tags"),
    ("DELETE", "/tasks/{task_id}/tags/{tag_id}"),
]

# What it must NOT reach: the wide power it was carved out of.
REDEFINE_THE_WORKFLOW = [
    ("POST", "/workflows"),
    ("PATCH", "/workflows/{workflow_id}"),
    ("DELETE", "/workflows/{workflow_id}"),
    ("POST", "/workflows/{workflow_id}/default"),
    ("PATCH", "/projects/{project_tag_id}/workflow"),
]
REDEFINE_THE_TAXONOMY = [
    ("POST", "/clients"),
    ("PATCH", "/clients/{tag_id}"),
    ("POST", "/projects"),
    ("PATCH", "/projects/{tag_id}"),
    ("POST", "/tags"),
    ("PATCH", "/tags/{tag_id}"),
    ("PUT", "/tags/{tag_id}/scope"),
]


def test_the_new_keys_are_real_catalogue_entries() -> None:
    # A route gated on a key nobody can be granted is unreachable, which
    # is a subtler outage than a 403.
    assert {"tasks:state", "tags:assign"} <= VALID_SCOPE_KEYS


@pytest.mark.parametrize("route", [ADVANCE_A_TASK])
def test_advancing_a_task_no_longer_costs_the_power_to_delete_workflows(
    route: tuple[str, str],
) -> None:
    assert scope_permits(*route, ["tasks:state"])
    for wide in REDEFINE_THE_WORKFLOW:
        assert not scope_permits(*wide, ["tasks:state"]), wide


@pytest.mark.parametrize("route", FILE_A_TASK)
def test_filing_a_task_no_longer_costs_the_power_to_rename_a_client(
    route: tuple[str, str],
) -> None:
    assert scope_permits(*route, ["tags:assign"])
    for wide in REDEFINE_THE_TAXONOMY:
        assert not scope_permits(*wide, ["tags:assign"]), wide


def test_a_grant_made_before_the_split_still_works() -> None:
    # The whole reason the sets are any-of. Remove this guarantee and the
    # split is a breaking change for every assistant already out there.
    assert scope_permits(*ADVANCE_A_TASK, ["workflows:write"])
    for route in FILE_A_TASK:
        assert scope_permits(*route, ["tags:write"]), route


def test_neither_key_lets_anything_else_through() -> None:
    # The narrow keys are new, so nothing but their own routes may answer
    # to them -- otherwise the split widened something by accident.
    for key, allowed in (
        ("tasks:state", {ADVANCE_A_TASK}),
        ("tags:assign", set(FILE_A_TASK)),
    ):
        reached = {
            (method, path)
            for (method, path), required in ROUTE_SCOPES.items()
            if isinstance(required, (str, frozenset)) and scope_permits(method, path, [key])
        }
        assert reached == allowed, f"{key} reaches {sorted(reached - allowed)}"


def test_the_403_asks_for_the_narrow_key_not_the_wide_one() -> None:
    """A transitional set exists so an old WIDE grant keeps working. If the
    refusal named the wide key, every caller that hit it would go and ask
    for the wide key -- pushing grants in the exact direction the split was
    made to reverse. ``require_agent_scope`` picks the lowest sorted member;
    that this IS the narrow key is a property of how the sets are named, so
    it is asserted rather than assumed."""
    for any_of, narrow in ((TASK_STATE_ANY, "tasks:state"), (TAG_ASSIGN_ANY, "tags:assign")):
        with pytest.raises(ForbiddenError) as caught:
            require_agent_scope({"assistant_scope": ["tasks:read"]}, any_of)
        assert caught.value.params["scope"] == narrow

    # And holding either member is enough, which is what "any of" means.
    for any_of in (TASK_STATE_ANY, TAG_ASSIGN_ANY):
        for held in sorted(any_of):
            require_agent_scope({"assistant_scope": [held]}, any_of)


def test_a_human_or_bare_token_is_untouched_by_any_of() -> None:
    # ``assistant_scope`` None means a human session or a bare agent
    # token: the gate must stay a no-op, or the split becomes an outage
    # for everyone who is not a scoped assistant.
    require_agent_scope({}, TAG_ASSIGN_ANY)
    require_agent_scope({"assistant_scope": None}, TASK_STATE_ANY)


def test_rest_and_mcp_split_the_same_way() -> None:
    """The same operation must cost the same thing on both surfaces, or the
    cheaper one is the bypass. This is the pair that would drift first: the
    split has to be made twice, in two files, in one change."""
    for route, tool in (
        (ADVANCE_A_TASK, "set_task_state"),
        (("POST", "/tasks/{task_id}/tags"), "add_task_tag"),
        (("DELETE", "/tasks/{task_id}/tags/{tag_id}"), "remove_task_tag"),
    ):
        assert ROUTE_SCOPES[route] == required_keys(tool), f"{route} and MCP {tool} disagree"

    # The two MCP tools with no single REST twin (the REST equivalent is a
    # PATCH body field, gated in the handler) split the same way.
    for tool in ("move_task_to_project", "set_task_client"):
        assert required_keys(tool) == TAG_ASSIGN_ANY, tool


def test_meta_is_callable_under_any_scope_including_none_granted() -> None:
    """A client cannot ask what it may do if asking is itself gated. The
    scope list on a real assistant can be edited in Settings after the
    credential was minted, so a client that hardcodes what it was given
    ends up offering controls the server refuses -- advertising a
    capability that does not exist."""
    for granted in ([], ["tasks:read"], ["workflows:write"]):
        assert scope_permits("GET", "/agent/self", granted), granted
        # Same circularity one question earlier: a credential that may
        # act in several workspaces cannot name one in a header before
        # it knows which ones it may name.
        assert scope_permits("GET", "/agent/workspaces", granted), granted

    # And it is still authenticated and still narrow: it is not a way to
    # reach the account row, which stays fenced off.
    assert not scope_permits("GET", "/auth/me", ["tasks:read"])
    assert not scope_permits("PATCH", "/auth/me", ["tasks:read"])
    assert not scope_permits("GET", "/workspaces", ["tasks:read"])


def test_meta_is_two_routes_and_stays_two() -> None:
    """META is a hole by construction, so it is enumerated. Growing the
    set is a decision, not a diff nobody reads.

    It grew by one, deliberately, when a credential became able to act in
    more than one workspace: ``GET /agent/workspaces`` says which ones,
    and it has to be askable before the caller can put a workspace in a
    header. It stays narrow in the way the fence below describes -- it
    answers about the CALLER's own workspaces, never about the account
    row, and a confined credential still gets exactly the one it was
    minted for."""
    meta_routes = {route for route, required in ROUTE_SCOPES.items() if required is META}
    assert meta_routes == {("GET", "/agent/self"), ("GET", "/agent/workspaces")}


# --------------------------------------------------------------------------
# Live, over the real app: the map is only half the guarantee.
# --------------------------------------------------------------------------


async def _signup_owner(c: AsyncClient) -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": "SPLIT",
            },
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def _assistant(c: AsyncClient, owner: dict[str, str], scope: list[str]) -> dict[str, str]:
    created = (
        await c.post(
            "/ai-assistants",
            headers=owner,
            json={"label": "Ext", "handle": f"e{uuid.uuid4().hex[:8]}", "scope": scope},
        )
    ).json()
    return {
        "Authorization": f"Bearer {created['raw_secret']}",
        "X-Workspace-Id": owner["X-Workspace-Id"],
    }


async def test_agent_self_answers_a_scoped_credential_over_http() -> None:
    """The grant the browser extension actually asks for, end to end. It
    must be able to learn its own scope from the server -- the whole point
    is that it stops hardcoding what it was minted with."""
    granted = ["tasks:read", "tasks:state", "tags:assign", "search:read"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup_owner(c)
        headers = await _assistant(c, owner, granted)

        res = await c.get("/agent/self", headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()

        assert sorted(body["scope"]) == sorted(granted)
        assert body["workspace"]["id"] == owner["X-Workspace-Id"]
        assert body["workspace"]["name"] == "SPLIT"
        assert body["identity"]["kind"] == "ai_assistant"
        assert body["token"]["prefix"]

        # It says what the CREDENTIAL is, never who the person is. An
        # email reaching an assistant here would make this a way around
        # the fence on /auth/me rather than a replacement for whoami.
        assert "email" not in res.text
        assert "is_admin" not in res.text

        # The fence itself is intact for the same credential.
        assert (await c.get("/auth/me", headers=headers)).status_code == 403
        assert (await c.get("/workspaces", headers=headers)).status_code == 403


async def test_agent_self_answers_a_credential_granted_nothing() -> None:
    # The case the META sentinel exists for: with an empty scope the
    # client still has to be able to find out that it may do nothing,
    # rather than guessing from a 403 on some unrelated route.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup_owner(c)
        headers = await _assistant(c, owner, [])
        res = await c.get("/agent/self", headers=headers)
        assert res.status_code == 200, res.text
        # An empty list, not null: null means "no restriction" and would
        # tell the client the opposite of the truth.
        assert res.json()["scope"] == []


async def test_a_human_session_reads_no_restriction() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        owner = await _signup_owner(c)
        body = (await c.get("/agent/self", headers=owner)).json()
        assert body["scope"] is None
        assert body["workspace"]["role"] == "owner"
