"""The set the server will self-service is the set the panel asks for.

Two files hold this list: ``mcp_scopes.SELF_SERVICE_SCOPES``, which
decides who may mint such a credential and whether it may reach the whole
account, and ``web/src/shared/extension.ts``, which is what the connect
page actually requests. They are in different languages and different
packages, and neither can import the other.

Drift is silent in both directions and neither is harmless. A key added
on the client and missing here turns every connect into a refusal, for a
reason the page cannot explain. A key added here and missing there widens
what a member may grant themselves with nothing on any screen saying so,
which is the direction that matters: this set is a security threshold, not
a convenience list.

So the agreement is asserted rather than assumed, and asserted as
equality: a subset check would let the server's set grow unnoticed, which
is exactly the growth nobody would see.
"""

from __future__ import annotations

import pathlib
import re

from mycelium_core.mcp_scopes import SELF_SERVICE_SCOPES, VALID_SCOPE_KEYS

_SHARED = pathlib.Path(__file__).resolve().parents[2] / "web" / "src" / "shared" / "extension.ts"


def _scopes_the_panel_asks_for() -> set[str]:
    source = _SHARED.read_text(encoding="utf-8")
    match = re.search(
        r"export const EXTENSION_SCOPES: readonly string\[\] = \[(.*?)\]",
        source,
        re.DOTALL,
    )
    assert match is not None, f"EXTENSION_SCOPES not found in {_SHARED}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_the_panel_asks_for_exactly_what_the_server_self_services() -> None:
    assert _scopes_the_panel_asks_for() == set(SELF_SERVICE_SCOPES)


def test_every_self_service_key_is_a_real_scope() -> None:
    """A key that is not in the catalogue would be refused at validation,
    so a typo here reads as "this credential may do less than it says"
    rather than as an error."""
    assert SELF_SERVICE_SCOPES <= VALID_SCOPE_KEYS


def test_the_only_danger_key_a_member_may_self_grant_is_the_one_the_panel_needs() -> None:
    """One key in this set is catalogued ``danger``, and enumerating it is
    the point of this test.

    ``attachments:write`` is danger because the bytes leave the workspace
    boundary when they are read back. It is also the whole of "file the
    page you are on", which is the capability the panel exists for, so
    dropping it would leave a self-service credential that cannot do the
    thing the product tells people to install it for.

    Every OTHER danger key -- the ones that spend credits or destroy data
    -- stays owner-granted. Pinned as an exact set rather than waived: a
    second danger key arriving in this list fails here, and someone has to
    write down why, as this does."""
    from mycelium_core.mcp_scopes import SCOPE_CATALOG

    danger = {s.key for s in SCOPE_CATALOG if s.category == "danger"}
    assert SELF_SERVICE_SCOPES & danger == {"attachments:write"}
