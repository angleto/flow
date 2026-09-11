"""What the browser extension asks for, against what a member may grant.

Two lists that must stay in a relation without becoming one list. The
relation is ``EXTENSION_SCOPES <= SELF_SERVICE_SCOPES``: the extension
asks for things any member may grant themselves, which is what lets a
plain member connect their own browser without an owner.

Aliasing them would satisfy the relation and destroy the meaning, because
they answer different questions. ``SELF_SERVICE_SCOPES`` is a ceiling on
what self-service may ever cover; ``EXTENSION_SCOPES`` is one surface's
request. If the second were defined as the first, widening the ceiling for
some future reason would widen the extension's grant the same day, in a
change whose author was thinking about something else entirely.

So the relation is asserted, and the two lists are allowed to differ.
"""

from __future__ import annotations

from mycelium_core.mcp_scopes import (
    EXTENSION_SCOPES,
    SELF_SERVICE_SCOPES,
    VALID_SCOPE_KEYS,
)


def test_every_extension_scope_is_a_real_scope() -> None:
    unknown = sorted(set(EXTENSION_SCOPES) - set(VALID_SCOPE_KEYS))
    assert unknown == [], f"not in the catalogue: {unknown}"


def test_the_extension_asks_for_nothing_beyond_what_a_member_may_grant() -> None:
    """The threshold follows the capability: a credential whose scope is
    a subset of this set is minted at member level, and one that exceeds
    it is owner-gated. An extension scope outside the set would refuse
    every member on their own workspace."""
    over = sorted(set(EXTENSION_SCOPES) - SELF_SERVICE_SCOPES)
    assert over == [], f"beyond self-service, would be owner-gated: {over}"


def test_the_two_wide_keys_stay_out() -> None:
    """Named individually rather than asserted as a count, so that
    re-adding one is a failure that says which and why, and so that
    adding an unrelated scope does not fail this test for no reason."""
    assert "workflows:write" not in EXTENSION_SCOPES, (
        "would let the panel delete the state machine every task runs on; "
        "advancing one task is tasks:state"
    )
    assert "tags:write" not in EXTENSION_SCOPES, (
        "would let the panel invent, rename and rescope the taxonomy; "
        "filing into an existing client or project is tags:assign"
    )


def test_the_list_has_no_duplicates() -> None:
    assert len(EXTENSION_SCOPES) == len(set(EXTENSION_SCOPES))
