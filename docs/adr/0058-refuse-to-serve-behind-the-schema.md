# ADR-0058: Refuse to serve behind the schema, serve ahead of it

Status: Accepted (2026-09-09)
Relates to: ADR-0015 (owner and runtime roles kept apart), ADR-0002
(tenant invariants in the schema, RLS as the primary defense), migration
`0011` (`SELECT` on `alembic_version` for the runtime role).

## Context

Migrations are a separate, gated deploy step: the migrate Job runs
`alembic upgrade head`, and only then do the app Deployments roll. That
ordering is a convention held by a runbook, and nothing in the running
system checked that it had been honoured.

A deploy that skipped the step produced a service that started
normally, passed both probes, served every request that did not touch
the changed table, and failed on the ones that did. The symptom is a
scattered set of runtime errors, arriving minutes to hours after the
deploy, attributed to whatever endpoint happened to surface first. The
cause is one line long and was knowable at startup.

The 2.3.15 rollout is what made this concrete: it carried migration
`0010`, the migrate step was run by hand ahead of the rollout, and
nothing would have complained if it had not been.

## Decision

Every service that talks to the database compares, in its startup path,
the revision recorded in `alembic_version` against the head of the
migration chain shipped inside its own image, and refuses to start when
the database is behind it. The refusal names the revision found, the
revision expected, and every revision between them, because that list is
the fix and a refusal that does not carry it sends the operator reading
code instead of running a command.

Three services, three entry points: the API lifespan, the SdI inbound
lifespan, and the worker before its job loops start.

**The comparison is asymmetric, and the asymmetry is the decision.** A
database BEHIND the code is refused. A database AHEAD of the code --
at a revision the build does not know -- is served, with a warning. Two
ordinary situations are the reason:

- between the migrate Job finishing and the last old pod being replaced,
  every pod still serving is behind the database. An eviction in that
  window must not fail to restart.
- a rollback to the previous image deliberately runs old code against
  the newer schema. That is the moment refusing to start would cost the
  most, and the moment an operator can least afford to debug the guard
  rather than the incident.

The check reads on the connection the service actually serves with, as
`mycelium_app`. The baseline grants tables to that role one at a time
and `alembic_version` was never on the list, so migration `0011` adds
it, read-only. What the role gains is one opaque revision string.

## Consequences

- A deploy that skips the migration step now fails loudly, at startup,
  on the new pods, while the old pods keep serving. The rollout stalls
  instead of half-completing.
- The migrate step is no longer optional in practice, only in principle.
  Any migration-bearing release must run it before the roll, which is
  what the runbook already said.
- A pod cannot start against a database it cannot read `alembic_version`
  from. That is a new dependency of startup on one grant, which is why
  the grant is asserted by a test running as the runtime role rather
  than assumed.
- The refusal is a distinct exception type, so a future deploy path can
  match on it rather than on message text.

## Alternatives rejected

**Strict equality.** Simpler to state and to test, and it would have
made both situations above an outage: every deploy window and every
rollback. The value of the check is in the skipped-migration case, which
is entirely on the "behind" side, so the strictness buys nothing it does
not also cost.

**Checking on first use, per code path.** Later, only on paths that get
exercised, and reporting as a query failure rather than as a startup
verdict: the failure mode this exists to replace.

**Giving the serving process the owner DSN** so it could read
`alembic_version` without a grant. It trades a much larger privilege for
the same string, against the whole point of ADR-0015.

**Writing the expected revision as a constant in the code**, kept in
step by a drift test. One more artifact to keep in agreement, where the
migration chain in the image already knows the answer.

## What it does not cover

It compares a recorded revision, not the shape of the schema: a
hand-edited column that leaves `alembic_version` alone passes. It cannot
distinguish a genuinely newer database from a foreign one, since both
look like a revision the build does not know. And an ahead database is
served without any claim that the newer schema is still compatible --
an expand-and-contract window that drops a column this build reads
passes the check and fails at the query.
