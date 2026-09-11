"""The verbs the store canonicalises are the verbs the surface merges.

Two files hold this list: ``models.note_link``, where the service decides
which kinds get their endpoints sorted before the row is written, and
``web/src/shared/noteLinks.ts``, where the panels decide which kinds are
rendered as one neighbour list rather than as two directions. They are in
different languages and different packages, and neither can import the
other.

Drift here is silent, because nothing breaks: the panel keeps rendering.
A kind the server canonicalises and the client still treats as directional
is shown on whichever of its two notes happens to sort lower by id and on
neither the other -- which is the defect this pair was written after, an
undirected ``related`` edge visible from one endpoint only. The opposite
direction splits a real direction into a single list and loses which note
grew from which.

So the agreement is asserted as equality, on both lists: the set of kinds
and the subset that is undirected.
"""

from __future__ import annotations

import pathlib
import re

from mycelium_core.models.note_link import (
    NOTE_NOTE_LINK_KINDS,
    NOTE_NOTE_LINK_UNDIRECTED_KINDS,
)

_SHARED = pathlib.Path(__file__).resolve().parents[2] / "web" / "src" / "shared" / "noteLinks.ts"


def _list_in_web(name: str) -> set[str]:
    source = _SHARED.read_text(encoding="utf-8")
    match = re.search(
        rf"export const {name}: readonly NoteLinkKind\[\] = \[(.*?)\]",
        source,
        re.DOTALL,
    )
    assert match is not None, f"{name} not found in {_SHARED}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_the_web_knows_the_same_four_verbs() -> None:
    assert _list_in_web("NOTE_LINK_KINDS") == set(NOTE_NOTE_LINK_KINDS)


def test_the_web_merges_exactly_the_kinds_the_service_canonicalises() -> None:
    assert _list_in_web("UNDIRECTED_NOTE_LINK_KINDS") == set(NOTE_NOTE_LINK_UNDIRECTED_KINDS)


def test_the_undirected_kinds_are_kinds() -> None:
    """A typo in either file would make the subset check above pass on a
    name no verb answers to, which reads as "nothing is undirected"."""
    assert NOTE_NOTE_LINK_UNDIRECTED_KINDS <= NOTE_NOTE_LINK_KINDS
