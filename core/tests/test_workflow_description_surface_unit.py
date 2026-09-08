"""The workflow serialisers carry what the model stores.

No session and no database: these are the pure dict-builders the MCP tools
return, so what they drop can be asserted directly. That matters here
because the field they dropped had been in the schema since migration 0065
and the model said, in a comment, what it was for:

    "surfaced to MCP agents so they can reason about a task's workflow
     without inferring from name"

    "an agent knows what 'in_review' or 'blocked' means for THIS workflow
     without guessing from the state name"

Both promises were storage-only. The write path had it too -- ``StateSpec``
and ``StateEdit`` both carry ``description``, and the service's
``create_workflow`` takes one -- so the only thing missing was six lines in
two dict literals, in the one layer whose whole job is to tell an agent what
it is looking at.

What it cost, 2026-09-08: a session closing three tasks tried
``todo -> verify``, was refused by the transition table, and had nothing
anywhere that said what ``verify`` is for or who moves a task into it. It
picked ``done`` because the transition existed and said so. A name is a
label; only a description is a contract.

These are regression tests, not coverage: each asserts a specific field is
present, because the failure mode is a field quietly going missing again
in a serialiser nobody re-reads.
"""

from __future__ import annotations

import uuid

from mycelium_core.models.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
)
from mycelium_mcp.server import _allowed_next, _state, _workflow


def _wf(description: str | None) -> WorkflowDefinition:
    return WorkflowDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="engineering",
        description=description,
        is_default=True,
        version=3,
    )


def _st(description: str | None, *, hidden: bool = False) -> WorkflowState:
    return WorkflowState(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        name="verify",
        ord=2,
        is_initial=False,
        is_terminal=False,
        is_hidden=hidden,
        description=description,
        version=1,
    )


def test_a_workflow_carries_its_description() -> None:
    out = _workflow(_wf("What this org means by shipping."))

    assert out["description"] == "What this org means by shipping."


def test_a_state_carries_its_description() -> None:
    out = _state(_st("Author is done; a second pair of eyes has not looked yet."))

    assert out["description"] == "Author is done; a second pair of eyes has not looked yet."


def test_a_state_says_whether_the_board_hides_it() -> None:
    """Dropped alongside the description and worth its own assertion: an
    agent listing a board should know a state exists but is off screen,
    rather than reporting a column the human cannot see."""
    assert _state(_st(None, hidden=True))["is_hidden"] is True
    assert _state(_st(None))["is_hidden"] is False


def test_an_undescribed_state_says_null_rather_than_omitting_the_key() -> None:
    """The distinction the whole fix rests on. A missing key reads as "this
    surface does not carry descriptions" and sends the agent off to guess
    from the name; an explicit null reads as "nobody has written this one
    down", which is a fact the agent can report instead of paper over."""
    out = _state(_st(None))

    assert "description" in out
    assert out["description"] is None


def test_the_serialisers_expose_every_field_a_caller_reasons_from() -> None:
    """One assertion over the whole shape, so a future field added to the
    model and forgotten here fails loudly instead of being invisible for
    another release."""
    wf_keys = set(_workflow(_wf("x")))
    st_keys = set(_state(_st("y")))

    assert wf_keys == {"id", "name", "description", "is_default", "version"}
    assert st_keys == {
        "id",
        "name",
        "ord",
        "is_initial",
        "is_terminal",
        "is_hidden",
        "description",
    }


def _tr(from_id: uuid.UUID, to_id: uuid.UUID) -> WorkflowTransition:
    return WorkflowTransition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        from_state_id=from_id,
        to_state_id=to_id,
    )


def _named(name: str) -> WorkflowState:
    st = _st(None)
    st.name = name
    return st


class TestWhereATaskCanGo:
    """``_allowed_next``, which is the only reasoning ``task_workflow`` does.

    It is a module-level function for the reason these tests exist: the first
    version of that tool called an import the module did not have, and the
    whole suite passed, because a tool body reached only through a session and
    a tenant is a body no unit test runs. The linter caught it, which is luck
    rather than coverage.
    """

    def test_only_the_states_one_transition_away(self) -> None:
        todo, doing, done = _named("todo"), _named("doing"), _named("done")
        states = [todo, doing, done]
        transitions = [_tr(todo.id, doing.id), _tr(doing.id, done.id)]

        assert [s["name"] for s in _allowed_next(states, transitions, todo)] == ["doing"]

    def test_the_board_order_and_not_the_transition_order(self) -> None:
        """A caller renders this list; a human reads a board down its columns.
        Ordering by the transition table would put ``done`` before ``doing``
        whenever the rows happened to be written that way."""
        todo, doing, done = _named("todo"), _named("doing"), _named("done")
        states = [todo, doing, done]
        transitions = [_tr(todo.id, done.id), _tr(todo.id, doing.id)]

        assert [s["name"] for s in _allowed_next(states, transitions, todo)] == [
            "doing",
            "done",
        ]

    def test_a_state_with_no_way_out_says_so(self) -> None:
        todo, done = _named("todo"), _named("done")

        assert _allowed_next([todo, done], [_tr(todo.id, done.id)], done) == []

    def test_no_current_state_is_not_the_same_as_no_way_out(self) -> None:
        """Both are the empty list here, and the caller tells them apart by
        ``current_state`` being null. Asserted so that a later version cannot
        quietly start guessing a state for a task that has none."""
        todo, done = _named("todo"), _named("done")

        assert _allowed_next([todo, done], [_tr(todo.id, done.id)], None) == []

    def test_a_transition_to_a_state_of_another_workflow_is_dropped(self) -> None:
        """The states list is the perimeter. A dangling ``to_state_id`` used to
        be a KeyError waiting in a dict lookup; it is now simply not reachable,
        which is what a caller can act on."""
        todo = _named("todo")
        elsewhere = uuid.uuid4()

        assert _allowed_next([todo], [_tr(todo.id, elsewhere)], todo) == []
