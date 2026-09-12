"""Garden health sensors (ADR-0035).

v1 computes ``accept_rate_classify`` from ``classification_feedback``;
the other sensors declare their floor but return ``value=None`` + a
reason until their data source lands (never a faked number). Plus the
daily-snapshot persistence the nightly worker tick uses.
"""

from __future__ import annotations

import datetime
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select, update

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _fake_ai import FakeLLM  # noqa: E402

from mycelium_core.ai_providers import set_llm_override  # noqa: E402
from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.embed_dims import EMBED_DIM  # noqa: E402
from mycelium_core.models.activity_log import ActivityLog  # noqa: E402
from mycelium_core.models.classification_feedback import ClassificationFeedback  # noqa: E402
from mycelium_core.models.memory_blob import MemoryBlob  # noqa: E402
from mycelium_core.models.note import Note, NoteKind  # noqa: E402
from mycelium_core.models.note_tag import NoteTag  # noqa: E402
from mycelium_core.models.tag import TagKind  # noqa: E402
from mycelium_core.services import decomposition as decomp  # noqa: E402
from mycelium_core.services import garden_health as health_svc  # noqa: E402
from mycelium_core.services import note_links, taxonomy  # noqa: E402
from mycelium_core.services import notes as notes_svc  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402


@pytest.fixture
def _wire_llm() -> Iterator[None]:
    set_llm_override(FakeLLM)
    try:
        yield
    finally:
        set_llm_override(None)


async def _org_user() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="GH",
        )
    return a.org_id, a.user_id


def _fb(org: uuid.UUID, user: uuid.UUID, action: str) -> ClassificationFeedback:
    return ClassificationFeedback(
        org_id=org,
        user_id=user,
        node_id=uuid.uuid4(),
        suggestion_type="tag",
        suggestion_value={"tag_id": str(uuid.uuid4())},
        action=action,
        model_version="garden-classify-v1",
    )


def _archive_row(
    org: uuid.UUID, user: uuid.UUID, ts: datetime.datetime | None = None
) -> ActivityLog:
    """A note-archive audit row (append-only log; the what-changed
    timeline derives 'big corpus edits' from these). ``ts`` pins the day
    so a burst aggregates into one group deterministically."""
    return ActivityLog(
        org_id=org,
        actor_id=user,
        actor_kind="human_direct",
        entity="note",
        entity_id=uuid.uuid4(),
        action="archive",
        ts=ts,
    )


async def test_accept_rate_counts_human_decisions_excludes_auto() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        for _ in range(3):
            s.add(_fb(org, user, "accept"))
        s.add(_fb(org, user, "reject"))
        # ``auto`` is the worker's system promotion: it must NOT count as
        # a human decision in the denominator.
        s.add(_fb(org, user, "auto"))
        await s.flush()
        health = await health_svc.compute_health(s, org_id=org)
    m = health.accept_rate_classify_7d
    assert m.floor == health_svc.ACCEPT_RATE_FLOOR
    assert m.value == 0.75  # 3 accept / (3 accept + 1 reject); auto excluded
    assert m.reason is None


async def test_accept_rate_override_counts_as_accepted() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        s.add(_fb(org, user, "accept"))
        s.add(_fb(org, user, "override"))
        s.add(_fb(org, user, "reject"))
        await s.flush()
        health = await health_svc.compute_health(s, org_id=org)
    # accept + override = 2 accepted of 3 decided.
    assert health.accept_rate_classify_7d.value == round(2 / 3, 4)


async def test_accept_rate_none_when_no_decisions() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        health = await health_svc.compute_health(s, org_id=org)
    m = health.accept_rate_classify_30d
    assert m.value is None
    assert m.reason  # a non-empty explanation, not a faked 0.0
    assert m.floor == health_svc.ACCEPT_RATE_FLOOR


async def test_unimplemented_sensors_declare_reason_not_fake_value() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        health = await health_svc.compute_health(s, org_id=org)
    assert health.fungal_lag.value is None and health.fungal_lag.reason
    assert health.leiden_modularity.value is None and health.leiden_modularity.reason
    assert health.time_to_first_link.value is None
    assert health.tag_entropy_local.value is None
    assert health.tag_entropy_local.floor == health_svc.TAG_ENTROPY_FLOOR


async def test_persist_and_read_snapshot() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        s.add(_fb(org, user, "accept"))
        await s.flush()
        await health_svc.persist_snapshot(s, org_id=org)
        snaps = await health_svc.recent_snapshots(s, org_id=org, days=30)
    assert len(snaps) == 1
    metrics = snaps[0].metrics
    assert "accept_rate_classify_7d" in metrics
    assert metrics["accept_rate_classify_7d"]["value"] == 1.0


async def test_persist_snapshot_idempotent_per_day() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        await health_svc.persist_snapshot(s, org_id=org)
        await health_svc.persist_snapshot(s, org_id=org)  # upsert on (org, day)
        snaps = await health_svc.recent_snapshots(s, org_id=org)
    assert len(snaps) == 1


async def _make_note(s: object, org: uuid.UUID, user: uuid.UUID, title: str) -> Note:
    return await notes_svc.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body {title}",
    )


async def _generic_tag(s: object, org: uuid.UUID, user: uuid.UUID, name: str) -> uuid.UUID:
    tag = await taxonomy.create_tag(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=TagKind.generic,
        name=name,
    )
    return tag.id


async def test_time_to_first_link_present_when_linked_none_otherwise() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        # No links yet -> no reading (not a faked 0).
        empty = await health_svc.compute_health(s, org_id=org)
        assert empty.time_to_first_link.value is None
        assert empty.time_to_first_link.reason
        a = await _make_note(s, org, user, "a")
        b = await _make_note(s, org, user, "b")
        await note_links.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=a.id, child_note_id=b.id, kind="related"
        )
        health = await health_svc.compute_health(s, org_id=org)
    m = health.time_to_first_link
    assert m.value is not None and m.value >= 0  # link created after the notes


async def _trash(s: object, org: uuid.UUID, user: uuid.UUID, note_id: uuid.UUID) -> None:
    """Bin a note through the service (version read fresh: linking does
    not bump it, but nothing here depends on that)."""
    ver = (await s.execute(select(Note.version).where(Note.id == note_id))).scalar_one()  # type: ignore[attr-defined]
    await notes_svc.soft_delete_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        note_id=note_id,
        expected_version=ver,
    )


async def test_time_to_first_link_ignores_an_edge_whose_other_end_is_binned() -> None:
    """ADR-0043 D1 at both ends (task 02f8f7c7): an edge into the bin is
    not an edge -- the weave builder drops it -- so it cannot be the
    moment a live note was absorbed."""
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "a")
        b = await _make_note(s, org, user, "b")
        await note_links.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=a.id, child_note_id=b.id, kind="related"
        )
        assert (await health_svc.compute_health(s, org_id=org)).time_to_first_link.value is not None
        await _trash(s, org, user, b.id)
        health = await health_svc.compute_health(s, org_id=org)
    # a is still live and still linkless: no reading, not a stale one.
    assert health.time_to_first_link.value is None
    assert health.time_to_first_link.reason


async def test_time_to_first_link_ignores_an_edge_into_an_unapproved_proposal() -> None:
    """The same at the review gate: a 'proposed' node is not a node yet,
    so linking to it is not absorption into the mycelium."""
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "a")
        b = await _make_note(s, org, user, "b")
        await note_links.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=a.id, child_note_id=b.id, kind="related"
        )
        await s.execute(update(Note).where(Note.id == b.id).values(review_state="proposed"))
        health = await health_svc.compute_health(s, org_id=org)
    assert health.time_to_first_link.value is None


async def test_tag_entropy_of_two_distinct_neighbour_tags_is_one_bit() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        center = await _make_note(s, org, user, "center")
        n1 = await _make_note(s, org, user, "n1")
        n2 = await _make_note(s, org, user, "n2")
        s.add(NoteTag(org_id=org, note_id=n1.id, tag_id=await _generic_tag(s, org, user, "alpha")))
        s.add(NoteTag(org_id=org, note_id=n2.id, tag_id=await _generic_tag(s, org, user, "beta")))
        await s.flush()
        for child in (n1, n2):
            await note_links.link_notes(
                s,
                org_id=org,
                actor_id=user,
                parent_note_id=center.id,
                child_note_id=child.id,
                kind="related",
            )
        health = await health_svc.compute_health(s, org_id=org)
    # center's neighbourhood holds two distinct tags, each once -> H = 1 bit
    # (n1/n2 only see the center, which carries no generic tag).
    assert health.tag_entropy_local.value == 1.0
    assert health.tag_entropy_local.floor == health_svc.TAG_ENTROPY_FLOOR


async def test_density_delta_positive_when_links_added_after_old_notes() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "old-a")
        b = await _make_note(s, org, user, "new-b")
        # Backdate note A past the 7d cutoff so it counts in "then"; the
        # link is created now, so links_then = 0.
        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=10)
        await s.execute(update(Note).where(Note.id == a.id).values(created_at=old))
        await note_links.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=a.id, child_note_id=b.id, kind="related"
        )
        health = await health_svc.compute_health(s, org_id=org)
    # now: 2 notes / 1 link = 0.5 ; then: 1 note / 0 links = 0.0 ; delta 0.5.
    assert health.density_delta_7d.value == 0.5


async def test_density_delta_does_not_rise_when_a_linked_node_is_binned() -> None:
    """The reported failure (task 02f8f7c7): the denominator lost the
    binned note while the numerator kept its link, so throwing something
    away -- what ``reject_node`` does to a rejected proposal -- reported a
    THICKENING mycelium. Numerator and denominator now read the same
    perimeter, so the edge leaves with its endpoint."""
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "old-a")
        b = await _make_note(s, org, user, "new-b")
        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=10)
        await s.execute(update(Note).where(Note.id == a.id).values(created_at=old))
        await note_links.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=a.id, child_note_id=b.id, kind="related"
        )
        assert (await health_svc.compute_health(s, org_id=org)).density_delta_7d.value == 0.5
        await _trash(s, org, user, b.id)
        health = await health_svc.compute_health(s, org_id=org)
    # now: 1 effective note / 0 effective links = 0.0 ; then: 1 / 0 = 0.0.
    # Before the fix: 1.0 - 0.0 = 1.0, a rise caused by a deletion.
    assert health.density_delta_7d.value == 0.0


async def test_density_delta_excludes_unapproved_proposals_from_both_terms() -> None:
    """A proposal awaiting review is neither a node nor an edge: it must
    not move the density in either direction while it sits in the inbox."""
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        a = await _make_note(s, org, user, "old-a")
        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=10)
        await s.execute(update(Note).where(Note.id == a.id).values(created_at=old))
        b = await _make_note(s, org, user, "proposed-b")
        await note_links.link_notes(
            s, org_id=org, actor_id=user, parent_note_id=a.id, child_note_id=b.id, kind="related"
        )
        await s.execute(update(Note).where(Note.id == b.id).values(review_state="proposed"))
        health = await health_svc.compute_health(s, org_id=org)
    # Without the review leg: 2 notes / 1 link now -> 0.5 out of thin air.
    assert health.density_delta_7d.value == 0.0


async def test_recall_at_k_is_blocked_with_reason() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        health = await health_svc.compute_health(s, org_id=org)
    assert health.recall_at_k.value is None
    assert health.recall_at_k.reason


async def test_fungal_lag_measures_archive_to_distillation(_wire_llm: None) -> None:
    """WS-C6: fungal_lag is the real median seconds from a source's first
    archiving to its first distillation -- not the old hardcoded 'blocked'
    constant. The activity log is append-only, so the controlled backdated
    archive event is inserted directly (identical in shape to archive_note's
    row) to assert a known positive interval."""
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        source = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="finished",
            text="a finished thought worth distilling into reusable atoms",
        )
        await s.flush()
        archived_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)
        s.add(
            ActivityLog(
                org_id=org,
                actor_id=user,
                actor_kind="human_direct",
                entity="note",
                entity_id=source.id,
                action="archive",
                ts=archived_at,
            )
        )
        await s.flush()
        await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        health = await health_svc.compute_health(s, org_id=org)
    assert health.fungal_lag.reason is None
    assert health.fungal_lag.value is not None
    # ~2h between the backdated archive and the just-now distillation.
    assert health.fungal_lag.value >= 7000


async def test_fungal_lag_reason_when_distillation_from_live_source(_wire_llm: None) -> None:
    """A distillation whose source was never archived contributes no
    archive->distill interval: value None with the specific reason (NOT the
    generic 'no distillations yet', which is reserved for a truly empty
    corpus)."""
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        source = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="live",
            text="a non-trivial body the LLM will distill on the first pass",
        )
        await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        health = await health_svc.compute_health(s, org_id=org)
    assert health.fungal_lag.value is None
    assert health.fungal_lag.reason == health_svc._NO_ARCHIVED_DISTILLATIONS


async def test_fungal_lag_ignores_a_distillation_awaiting_review(_wire_llm: None) -> None:
    """An un-approved distillation is a PROPOSAL of humus, not humus
    produced: it must not report decomposition that nobody accepted (task
    02f8f7c7). With no other distillation the sensor reads 'none yet'."""
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        source = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="finished",
            text="a finished thought worth distilling into reusable atoms",
        )
        await s.flush()
        s.add(
            ActivityLog(
                org_id=org,
                actor_id=user,
                actor_kind="human_direct",
                entity="note",
                entity_id=source.id,
                action="archive",
                ts=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2),
            )
        )
        await s.flush()
        await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        assert (await health_svc.compute_health(s, org_id=org)).fungal_lag.value is not None
        await s.execute(
            update(Note)
            .where(Note.org_id == org, Note.humus_kind == "distillation")
            .values(review_state="proposed")
        )
        health = await health_svc.compute_health(s, org_id=org)
    assert health.fungal_lag.value is None
    assert health.fungal_lag.reason == health_svc._NO_DISTILLATIONS


async def test_fungal_lag_when_the_source_is_binned_reports_no_archived_source(
    _wire_llm: None,
) -> None:
    """The source left the mycelium: there is no interval to measure, and
    the humus atom it produced is still there -- so the reason is 'none
    from an archived source', never 'no distillations yet'."""
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        source = await notes_svc.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="finished",
            text="a finished thought worth distilling into reusable atoms",
        )
        await s.flush()
        s.add(
            ActivityLog(
                org_id=org,
                actor_id=user,
                actor_kind="human_direct",
                entity="note",
                entity_id=source.id,
                action="archive",
                ts=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2),
            )
        )
        await s.flush()
        await decomp.distill_note(s, org_id=org, actor_id=user, note_id=source.id)
        assert (await health_svc.compute_health(s, org_id=org)).fungal_lag.value is not None
        await _trash(s, org, user, source.id)
        health = await health_svc.compute_health(s, org_id=org)
    assert health.fungal_lag.value is None
    assert health.fungal_lag.reason == health_svc._NO_ARCHIVED_DISTILLATIONS


# --- embedding coverage sensor (WS-A, task 38ac2bc0) ---


def _blob(org: uuid.UUID, *, text: str | None, embedded: bool) -> MemoryBlob:
    """A memory blob row. ``embedded`` populates the local dense vector (and a
    real ``model_id``); otherwise it stays keyword-only (embedding NULL,
    model_id 'none') -- the state the WS-A backfill exists to drain."""
    return MemoryBlob(
        org_id=org,
        text=text,
        embedding=[0.0] * EMBED_DIM if embedded else None,
        model_id="bge-m3" if embedded else "none",
    )


async def test_embedding_coverage_fraction_over_embeddable_blobs() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        s.add(_blob(org, text="alpha", embedded=True))
        s.add(_blob(org, text="beta", embedded=True))
        s.add(_blob(org, text="gamma", embedded=False))
        # Blank-body blob: the backfill skips it forever (txt.strip() empty),
        # so it must be excluded from the denominator -- otherwise coverage
        # could never reach 1.0 and the floor alert would false-fire.
        s.add(_blob(org, text="   ", embedded=False))
        await s.flush()
        health = await health_svc.compute_health(s, org_id=org)
    m = health.embedding_coverage
    assert m.value == round(2 / 3, 4)  # 2 embedded of 3 embeddable; blank excluded
    assert m.floor == health_svc.EMBEDDING_COVERAGE_FLOOR
    assert m.reason is None


async def test_embedding_coverage_none_when_nothing_embeddable() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        # Only a blank blob exists: nothing the backfill could embed, so the
        # sensor reports "no signal" (None + reason), never a faked 0%.
        s.add(_blob(org, text="", embedded=False))
        await s.flush()
        health = await health_svc.compute_health(s, org_id=org)
    m = health.embedding_coverage
    assert m.value is None
    assert m.reason == health_svc._NO_EMBEDDABLE
    assert m.floor == health_svc.EMBEDDING_COVERAGE_FLOOR


# --- "what changed" timeline (ADR-0035 §84, task d0bada67) ---


async def test_events_empty_on_fresh_workspace() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        events = await health_svc.recent_events(s, org_id=org)
    assert events == []


async def test_events_classifier_version_one_entry_per_version() -> None:
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        # Two feedback rows, same model_version -> a single bump event
        # (first appearance), not one per row.
        s.add(_fb(org, user, "accept"))
        s.add(_fb(org, user, "reject"))
        await s.flush()
        events = await health_svc.recent_events(s, org_id=org)
    versions = [e for e in events if e.kind == "classifier_version"]
    assert len(versions) == 1
    assert versions[0].detail == {"version": "garden-classify-v1"}


async def test_events_corpus_edit_at_threshold_not_below() -> None:
    org, user = await _org_user()
    ts = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
    async with tenant_session(str(org), str(user)) as s:
        # One short of the threshold -> no corpus_edit (everyday churn,
        # not a deliberate batch).
        for _ in range(health_svc.BULK_EDIT_THRESHOLD - 1):
            s.add(_archive_row(org, user, ts=ts))
        await s.flush()
        below = await health_svc.recent_events(s, org_id=org)
        assert [e for e in below if e.kind == "corpus_edit"] == []
        # Reaching the threshold surfaces exactly one (day, action) entry.
        s.add(_archive_row(org, user, ts=ts))
        await s.flush()
        at = await health_svc.recent_events(s, org_id=org)
    edits = [e for e in at if e.kind == "corpus_edit"]
    assert len(edits) == 1
    assert edits[0].detail == {"action": "archive", "count": health_svc.BULK_EDIT_THRESHOLD}


async def test_events_corpus_edit_from_real_note_creates() -> None:
    """End-to-end: a burst of real create_note calls feeds the timeline
    through the production audit path, not just hand-inserted rows."""
    org, user = await _org_user()
    async with tenant_session(str(org), str(user)) as s:
        for i in range(health_svc.BULK_EDIT_THRESHOLD):
            await _make_note(s, org, user, f"n{i}")
        events = await health_svc.recent_events(s, org_id=org)
    creates = [e for e in events if e.kind == "corpus_edit" and e.detail["action"] == "create"]
    assert len(creates) == 1
    assert creates[0].detail["count"] >= health_svc.BULK_EDIT_THRESHOLD


async def test_events_sorted_newest_first() -> None:
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    older = now - datetime.timedelta(days=5)
    newer = now - datetime.timedelta(days=1)
    async with tenant_session(str(org), str(user)) as s:
        for ts in (older, newer):
            for _ in range(health_svc.BULK_EDIT_THRESHOLD):
                s.add(_archive_row(org, user, ts=ts))
        await s.flush()
        events = await health_svc.recent_events(s, org_id=org)
    edits = [e for e in events if e.kind == "corpus_edit"]
    assert len(edits) == 2
    assert edits[0].at > edits[1].at  # newest first


async def test_events_window_excludes_out_of_range() -> None:
    org, user = await _org_user()
    now = datetime.datetime.now(datetime.UTC)
    async with tenant_session(str(org), str(user)) as s:
        # A bulk archive 2 days ago (inside a 7-day window).
        for _ in range(health_svc.BULK_EDIT_THRESHOLD):
            s.add(_archive_row(org, user, ts=now - datetime.timedelta(days=2)))
        # Classifier feedback whose first appearance is 30 days ago
        # (outside the 7-day window).
        s.add(_fb(org, user, "accept"))
        await s.flush()
        await s.execute(
            update(ClassificationFeedback)
            .where(ClassificationFeedback.org_id == org)
            .values(ts=now - datetime.timedelta(days=30))
        )
        await s.flush()
        windowed = await health_svc.recent_events(s, org_id=org, days=7)
    kinds = {e.kind for e in windowed}
    assert "corpus_edit" in kinds  # 2 days ago -> inside the window
    assert "classifier_version" not in kinds  # 30 days ago -> excluded
