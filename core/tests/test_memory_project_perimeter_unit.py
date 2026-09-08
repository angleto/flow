"""Pure-function unit tests for the retrieval perimeter predicate.

No session and no database: ``_project_pred`` returns a SQLAlchemy clause,
so the three perimeters can be told apart by compiling them. That matters
here more than usual, because the defect these tests pin was invisible in
every DB-backed test that passed a project id -- which was all of them.

The incident, measured 2026-09-07. A gold set of twenty "what did we decide
about X and why" questions was run through the unified search with no
project, because somebody asking that question does not know which project
holds the answer. It found three. The split is the whole diagnosis:

  * tasks  3 of 6  -- task blobs are stored with ``project_id=NULL``, and
                      the NULL perimeter is exactly what an unscoped search
                      searched, so they were always reachable;
  * notes  0 of 14 -- a note blob carries its note's project, and no
                      unscoped search could reach one.

Not ranking and not embeddings. One predicate, in which ``None`` meant "the
blobs belonging to no project" while every caller omitting the argument
meant "anywhere". ``ANY_PROJECT`` is the third case that was missing.
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from mycelium_core.services.memory import ANY_PROJECT, _project_pred


def _sql(clause) -> str:  # type: ignore[no-untyped-def]
    """The predicate as PostgreSQL would see it, values inlined."""
    return str(
        clause.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_a_named_project_restricts_to_that_project() -> None:
    project = uuid.uuid4()
    sql = _sql(_project_pred(project))

    assert str(project) in sql
    assert "IS NULL" not in sql


def test_none_still_means_the_null_perimeter() -> None:
    """``None`` keeps its meaning, and that is deliberate rather than left
    alone. Task blobs live at the NULL perimeter on purpose so they stay
    org-wide, and three services mirror this reading in their own
    docstrings. Widening ``None`` would have repaired the note branch by
    breaking the task branch."""
    sql = _sql(_project_pred(None))

    assert "project_id IS NULL" in sql


def test_any_project_applies_no_perimeter_clause_at_all() -> None:
    """The case that did not exist. A caller asking across projects gets a
    predicate that constrains nothing, so org scoping -- applied by the
    caller -- is what bounds the search. A project is a retrieval perimeter
    and never an access boundary, so this cannot surface a blob the caller
    could not already read by id."""
    sql = _sql(_project_pred(ANY_PROJECT))

    assert "project_id" not in sql
    assert sql.strip().lower() in {"true", "1 = 1"}


def test_the_three_perimeters_are_three_different_clauses() -> None:
    """The assertion the measurement is really about: before ANY_PROJECT
    existed, "this project" and "no project" were the only two things the
    API could say, and a question that names no project was answered as if
    it had named the absence of one."""
    project = uuid.uuid4()
    clauses = {
        _sql(_project_pred(project)),
        _sql(_project_pred(None)),
        _sql(_project_pred(ANY_PROJECT)),
    }

    assert len(clauses) == 3


def test_any_project_is_not_confusable_with_none() -> None:
    """Guards the call sites, which choose with ``if project_id is None``.
    A sentinel that compared equal to None, or that was falsy in a way an
    ``if`` could swallow, would reintroduce the defect silently."""
    assert ANY_PROJECT is not None
    assert (ANY_PROJECT is None) is False
    assert repr(ANY_PROJECT) == "ANY_PROJECT"
