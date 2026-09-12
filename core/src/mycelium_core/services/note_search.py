"""Note search: keep one memory blob per note PART in sync with its body.

The note-part analogue of ``services.task_search``. Decision 2026-06-09
(task 9fc94327): notes become first-class hits in the existing memory
pipeline (``memory_blobs``: FTS generated + pgvector + RRF) without a
parallel index, indexed PER PART so each part re-embeds independently.
The binding is 1:1 via ``note_part_index_pointer``; the blob text is the
part's ``title || body``.

Why per-part (not per-note): a long note is several parts; editing one
part should not re-embed the whole note, and the chunked-append path
(``note_parts.append_to_part``) targets a single part. One blob per part
keeps the re-embed unit aligned with the edit unit.

Mutation tracking
-----------------
Sync SQLAlchemy event listeners on ``NotePart`` cover the ORM paths
(``create_part`` and ``_upsert_part_zero`` used by create_note /
update_note / transcribe -- all ``session.add`` / attribute-set + flush).
The Core-update paths (``append_to_part`` / ``prepend_to_part`` /
``replace_in_part`` / ``update_part`` go through ``optimistic_update``,
and ``delete_part`` through a Core ``delete``) bypass mapper events, so
those choke points call :func:`mark_note_part_dirty` /
:func:`mark_note_part_deleted` explicitly. The async
:func:`flush_note_search_dirty` drains the accumulated ids and upserts
the blobs; it is called from ``db.tenant_session`` just before commit
(same chokepoint as the task index).

content_hash, embedder timeout, keyword-only fallback: identical
contract to ``task_search`` (a 2 s embed cap degrades to keyword-only;
the generic ``embedding_migration`` worker re-embeds NULL-vector blobs
later, off the write path).

Project scoping: a part blob inherits the note's project (via
``notes.project_tag_for_note``), matching the existing note-blob scoping
(voice transcripts already landed project-scoped on the "note" channel).
This preserves per-project search isolation -- it is NOT widened to the
org-wide surface the task index uses.

Index scope: a note at ``index_scope='none'`` is not indexed at all. The
scope is read per part in the resync, before the content_hash
short-circuit, and fanned out over the note's parts by
``services.notes.update_note`` -- no mapper listener is registered on
``Note``, so the flip itself has to say so.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, object_session

from mycelium_core.embed_dims import EMBED_DIM
from mycelium_core.embedder import Embedder, EmbedResult, EmbedSide, get_embedder
from mycelium_core.models.index_scope import IndexScope
from mycelium_core.models.memory_blob import BlobSource, MemoryBlob
from mycelium_core.models.note import Note
from mycelium_core.models.note_part import NotePart
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.services.fts_language import detect_fts_language
from mycelium_core.services.memory import erase_blobs_for_sources

logger = logging.getLogger(__name__)

_DIRTY_KEY = "note_search_dirty"
_DELETED_KEY = "note_search_deleted"
_EMBED_TIMEOUT_S = 2.0
_NO_EMBED_MODEL = "none"


# ---------------------------------------------------------------- listeners


def _record_dirty(session: Session | None, part_id: uuid.UUID | None) -> None:
    if session is None or part_id is None:
        return
    session.info.setdefault(_DIRTY_KEY, set()).add(part_id)


def _record_deleted(session: Session | None, part_id: uuid.UUID | None) -> None:
    if session is None or part_id is None:
        return
    session.info.setdefault(_DELETED_KEY, set()).add(part_id)


def mark_note_part_dirty(session: AsyncSession, part_id: uuid.UUID) -> None:
    """Mark a note part for re-index at commit time.

    The mapper listeners below cover the ORM paths (``create_part``,
    ``_upsert_part_zero``), but ``optimistic_update`` / Core
    ``update`` bypass mapper-level events (the SQLAlchemy docs note this:
    mapper events fire only for the unit-of-work flush). The part-mutation
    choke points in ``services.note_parts`` call this so the resync still
    fires on commit. Cheap and idempotent (dedup via a ``set``; nothing
    hits the DB here)."""
    _record_dirty(session.sync_session, part_id)


def mark_note_part_deleted(session: AsyncSession, part_id: uuid.UUID) -> None:
    """Mark a note part as deleted so its blob is dropped on commit.

    ``delete_part`` issues a Core ``DELETE`` (no after_delete event), so
    it calls this to schedule the blob cleanup."""
    _record_deleted(session.sync_session, part_id)


# The decorator side-effects are what matters; the function names are
# deliberately unused references (pyright can't see the registration).


@event.listens_for(NotePart, "after_insert")
def _part_after_insert(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: NotePart
) -> None:
    _record_dirty(object_session(target), target.id)


@event.listens_for(NotePart, "after_update")
def _part_after_update(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: NotePart
) -> None:
    # Enqueue unconditionally; the cheap content_hash compare in the
    # flush short-circuits ord/metadata-only updates that don't change
    # the searchable text.
    _record_dirty(object_session(target), target.id)


@event.listens_for(NotePart, "after_delete")
def _part_after_delete(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: NotePart
) -> None:
    _record_deleted(object_session(target), target.id)


# ---------------------------------------------------------------- rendering


def render_part_for_search(part: NotePart) -> str:
    """Title (if any) then body. The title is the user-facing label;
    including it lets a search match a part by its heading even when the
    body is terse."""
    title = (part.title or "").strip()
    body = part.body or ""
    if title:
        return f"{title}\n\n{body}".strip()
    return body.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- embedder helper


@dataclass
class _TimeoutEmbedder:
    """Wrap an Embedder so a slow encode degrades to keyword-only rather
    than blocking commit (same contract as task_search)."""

    inner: Embedder
    timeout: float

    async def embed(self, text: str, *, side: EmbedSide) -> EmbedResult:
        return await asyncio.wait_for(self.inner.embed(text, side=side), timeout=self.timeout)


async def _safe_embed(
    embedder: _TimeoutEmbedder, text_body: str
) -> tuple[list[float] | None, str, int]:
    """Best-effort embed. On timeout or any embedder failure return a
    keyword-only result; the FTS branch still covers the blob and the
    ``embedding_migration`` worker re-embeds it later."""
    try:
        result = await embedder.embed(text_body, side=EmbedSide.document)
    except TimeoutError:
        return None, _NO_EMBED_MODEL, 0
    except Exception as exc:
        logger.debug("note-search embed failed: %s", exc)
        return None, _NO_EMBED_MODEL, 0
    if not result.vector or len(result.vector) != EMBED_DIM:
        return None, _NO_EMBED_MODEL, 0
    return list(result.vector), result.model_id, result.tokens


# ---------------------------------------------------------------- flush


async def flush_note_search_dirty(session: AsyncSession) -> None:
    """Process the dirty/deleted sets accumulated by the listeners.

    Called by ``db.tenant_session`` right before commit, so the resync is
    in the same transaction as the part mutation (FTS visible atomically;
    semantic vector best-effort within the 2 s timeout, recovered async
    otherwise). Idempotent and re-entry safe (pops the sets). Robust to a
    partially-rolled-back outer transaction (skip; the embedding backfill
    reconciles later)."""
    from sqlalchemy.exc import InvalidRequestError

    info = session.sync_session.info
    deleted_ids: set[uuid.UUID] = info.pop(_DELETED_KEY, set())
    dirty_ids: set[uuid.UUID] = info.pop(_DIRTY_KEY, set())
    dirty_ids -= deleted_ids
    if not deleted_ids and not dirty_ids:
        return
    from mycelium_core.db import index_maintenance_scope

    try:
        # Org-scoped maintenance, not the caller's project perimeter: see
        # ``db.index_maintenance_scope``. Inside the ``try`` and after the
        # early return, so an aborted transaction with nothing pending still
        # emits no SQL.
        async with index_maintenance_scope(session):
            for part_id in deleted_ids:
                await _delete_part_blob(session, part_id)
            for part_id in dirty_ids:
                await _resync_part_blob(session, part_id)
    except InvalidRequestError as exc:
        logger.warning(
            "note-search resync skipped (session unusable, likely partial savepoint rollback): %s",
            exc,
        )


# ---------------------------------------------------------------- delete


async def delete_part_index_now(session: AsyncSession, part_id: uuid.UUID) -> None:
    """Drop a part's search blob immediately (inline, not deferred).

    Call this BEFORE a hard ``DELETE`` of the part row: the pointer's
    ``part_id`` FK is ``ON DELETE CASCADE``, so once the part row goes the
    pointer is gone and a deferred :func:`flush_note_search_dirty` can no
    longer resolve the blob to delete (it would orphan the blob).
    Deleting the blob here cascades the pointer (blob->pointer FK), so the
    subsequent part delete finds nothing left to cascade."""
    await _delete_part_blob(session, part_id)


async def _drop_part_blobs(session: AsyncSession, part_id: uuid.UUID) -> None:
    """Erase every blob this part is the source of, by provenance.

    Not ``_delete_part_blob``: that one resolves through the pointer,
    whose primary key is ``part_id``, so it names exactly one blob. A
    part owns N blobs as soon as the indexer chunks a long one, and this
    branch has to keep holding then without being rewritten -- and it
    does, because provenance is the relation that is already 1:N.

    What it does not cover: a blob claimed by a second source survives,
    since ``erase_blobs_for_sources`` drops a blob only once nothing
    else points at it. Consolidation copies each member's provenance
    onto the merged concept, so the concept IS reached here -- this
    part's provenance row on it goes -- but the concept survives on the
    other members' rows, still carrying the merged text. ``index_scope``
    is an opt-out from indexing a row, not a retraction of everything
    ever derived from it.
    """
    await erase_blobs_for_sources(session, sources=[("note_part", str(part_id))])


async def _delete_part_blob(session: AsyncSession, part_id: uuid.UUID) -> None:
    """Remove the blob owned by this part (pointer cascades). Org-blind
    on purpose: the pointer carries ``org_id`` and RLS scopes the SELECT
    to the current tenant."""
    row = (
        await session.execute(
            select(NotePartIndexPointer.blob_id, NotePartIndexPointer.org_id).where(
                NotePartIndexPointer.part_id == part_id
            )
        )
    ).one_or_none()
    if row is None:
        return
    blob_id, org_id = row
    await session.execute(
        delete(MemoryBlob).where(MemoryBlob.id == blob_id, MemoryBlob.org_id == org_id)
    )


# ---------------------------------------------------------------- resync


async def _load_part(
    session: AsyncSession, part_id: uuid.UUID
) -> tuple[NotePart, IndexScope] | None:
    """The part, plus the index scope of the note that owns it.

    The scope lives on ``notes`` while the indexed unit is the part, so
    the resync has to read across the two. It rides along in the load
    the resync already pays rather than costing a SELECT of its own:
    this path runs on every commit that touches a part, including the
    cheap hash-unchanged one.

    LEFT rather than INNER as a belt on a brace, not because the case is
    expected: ``note_part.note_id`` is NOT NULL and cascades, so a part
    does not outlive its note (the same fact the backfill sweep below
    relies on to use an inner join). If one ever did, LEFT keeps it in
    the resync -- where the ``pointer is None`` and part-gone paths can
    still clean up -- instead of dropping it silently. A missing note
    reads as ``org``, the default: a part with no note to answer for it
    is not evidence that someone asked for the opt-out.
    """
    row = (
        await session.execute(
            select(NotePart, Note.index_scope)
            .outerjoin(Note, Note.id == NotePart.note_id)
            .where(NotePart.id == part_id)
        )
    ).one_or_none()
    if row is None:
        return None
    part, scope = row
    return part, IndexScope(scope) if scope is not None else IndexScope.org


async def _resync_part_blob(session: AsyncSession, part_id: uuid.UUID) -> None:
    """UPSERT one blob for the part + maintain the pointer.

    Three paths, gated on (a) pointer existence and (b) content hash:
      - no pointer: INSERT blob (channel='note', source=note_part) + pointer
      - pointer + same hash: skip (cheap path, no embed)
      - pointer + new hash: UPDATE blob text+embedding, UPDATE hash
    """
    loaded = await _load_part(session, part_id)
    if loaded is None:
        # Part is gone (hard delete that didn't go through after_delete,
        # e.g. a note-cascade DELETE). Clean the pointer/blob too.
        await _delete_part_blob(session, part_id)
        return
    part, index_scope = loaded
    if index_scope == IndexScope.none:
        # Before the content_hash short-circuit below, deliberately. A scope
        # flip leaves the rendered text identical, so the hash is unchanged
        # and a guard placed after the short-circuit would never run on a
        # part that is already indexed -- which is the whole remedy case.
        # Delete rather than skip, or the stale blob stays retrievable.
        await _drop_part_blobs(session, part_id)
        return
    text_body = render_part_for_search(part)
    new_hash = content_hash(text_body)

    pointer = (
        await session.execute(
            select(NotePartIndexPointer).where(NotePartIndexPointer.part_id == part_id)
        )
    ).scalar_one_or_none()

    if pointer is not None and pointer.content_hash == new_hash:
        # Text unchanged. ``merge_notes`` reparents a part (new note_id)
        # without touching its body; refresh the pointer/blob ownership so
        # the hit still resolves to -- and is project-scoped to -- the new
        # note, without a needless re-embed.
        if pointer.note_id != part.note_id:
            await _refresh_pointer_note(session=session, pointer=pointer, part=part)
        return

    embedder = _TimeoutEmbedder(get_embedder(), _EMBED_TIMEOUT_S)
    vector, model_id, _tokens = await _safe_embed(embedder, text_body)

    if pointer is None:
        await _create_blob_and_pointer(
            session=session,
            part=part,
            text_body=text_body,
            content_hash_value=new_hash,
            vector=vector,
            model_id=model_id,
        )
    else:
        await _update_blob_and_pointer(
            session=session,
            pointer=pointer,
            part=part,
            text_body=text_body,
            content_hash_value=new_hash,
            vector=vector,
            model_id=model_id,
        )


async def _create_blob_and_pointer(
    *,
    session: AsyncSession,
    part: NotePart,
    text_body: str,
    content_hash_value: str,
    vector: list[float] | None,
    model_id: str,
) -> None:
    """Insert path. The blob carries channel_key='note' via the
    ``memory_channel`` tag attached below; ``project_id`` is the note's
    project (per-project scoping, see module docstring)."""
    from mycelium_core.services.notes import project_tag_for_note

    project_id = await project_tag_for_note(session, note_id=part.note_id)
    now = dt.datetime.now(tz=dt.UTC)
    blob = MemoryBlob(
        org_id=part.org_id,
        project_id=project_id,
        namespace="note",
        tier="hot",
        text=text_body,
        fts_language=detect_fts_language(text_body),
        embedding=vector,
        model_id=model_id,
        dim=EMBED_DIM,
        access_count=1,
        last_accessed_at=now,
    )
    session.add(blob)
    await session.flush()
    session.add(
        BlobSource(
            blob_id=blob.id,
            org_id=part.org_id,
            source_kind="note_part",
            source_id=str(part.id),
        )
    )
    await _attach_inherited_tags(session, org_id=part.org_id, blob_id=blob.id, note_id=part.note_id)
    await _attach_channel_tag(session, org_id=part.org_id, blob_id=blob.id)
    try:
        async with session.begin_nested():
            session.add(
                NotePartIndexPointer(
                    part_id=part.id,
                    note_id=part.note_id,
                    org_id=part.org_id,
                    blob_id=blob.id,
                    content_hash=content_hash_value,
                )
            )
            await session.flush()
    except IntegrityError:
        # Concurrent resync just inserted the pointer. Drop the blob we
        # just made (the other transaction's blob is canonical).
        await session.execute(
            delete(MemoryBlob).where(MemoryBlob.id == blob.id, MemoryBlob.org_id == part.org_id)
        )


async def _update_blob_and_pointer(
    *,
    session: AsyncSession,
    pointer: NotePartIndexPointer,
    part: NotePart,
    text_body: str,
    content_hash_value: str,
    vector: list[float] | None,
    model_id: str,
) -> None:
    """Update path. Direct SQL UPDATE on the blob (preserves
    cluster/access counters); the FTS generated column follows ``text``
    automatically. If the part was reparented (merge), also refresh the
    blob's project and the pointer's note_id."""
    values: dict[str, object] = {
        "text": text_body,
        "fts_language": detect_fts_language(text_body),
        "embedding": vector,
        "model_id": model_id,
        "dim": EMBED_DIM,
    }
    if pointer.note_id != part.note_id:
        from mycelium_core.services.notes import project_tag_for_note

        values["project_id"] = await project_tag_for_note(session, note_id=part.note_id)
        pointer.note_id = part.note_id
    await session.execute(
        update(MemoryBlob)
        .where(MemoryBlob.id == pointer.blob_id, MemoryBlob.org_id == pointer.org_id)
        .values(**values)
    )
    pointer.content_hash = content_hash_value


async def _refresh_pointer_note(
    *,
    session: AsyncSession,
    pointer: NotePartIndexPointer,
    part: NotePart,
) -> None:
    """A part moved to another note (merge) but its body is unchanged:
    re-point the pointer + re-scope the blob's project, no re-embed."""
    from mycelium_core.services.notes import project_tag_for_note

    project_id = await project_tag_for_note(session, note_id=part.note_id)
    await session.execute(
        update(MemoryBlob)
        .where(MemoryBlob.id == pointer.blob_id, MemoryBlob.org_id == pointer.org_id)
        .values(project_id=project_id)
    )
    pointer.note_id = part.note_id


async def _part_ids_of_note(session: AsyncSession, note_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (await session.execute(select(NotePart.id).where(NotePart.note_id == note_id)))
        .scalars()
        .all()
    )


async def mark_note_parts_dirty(session: AsyncSession, *, note_id: uuid.UUID) -> None:
    """Queue every part of a note for re-index at commit.

    The note-level half of the ``index_scope`` guard, for the flip
    itself. No mapper listener is registered on ``Note`` and the scope
    lives there, so the UPDATE that sets it marks nothing dirty: without
    this call the change would reach the index only when some part's
    body next happened to change.

    One call covers BOTH directions, which is why it does not have to
    read the previous value: the per-part guard in ``_resync_part_blob``
    reads the scope that is now on the row and either erases that part's
    blobs or re-indexes them. Deferring to the flush rather than erasing
    here also keeps every index write inside the one window that runs
    without the caller's project perimeter (``db._index_maintenance_scope``);
    an erase issued inline from a project-scoped request deletes the
    provenance and leaves the blob.
    """
    for pid in await _part_ids_of_note(session, note_id):
        mark_note_part_dirty(session, pid)


async def rescope_note_blobs(
    session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID
) -> None:
    """Re-scope every already-indexed blob of a note to the note's CURRENT
    project perimeter (its project tag, or NULL). The perimeter lives on the
    blob's ``project_id``, set at index time; a note's project comes from a
    project-kind tag, so adding/removing that tag changes the perimeter
    without touching content. Project is metadata, not text -- one UPDATE,
    no re-embed -- so a peer's search sees the new perimeter immediately
    instead of only after the note's next content edit re-indexes it (task
    1d152747). A note not yet flushed to the index has no pointers here and
    is scoped correctly by the deferred index at commit."""
    from mycelium_core.services.notes import project_tag_for_note

    project_id = await project_tag_for_note(session, note_id=note_id)
    await session.execute(
        update(MemoryBlob)
        .where(
            MemoryBlob.org_id == org_id,
            MemoryBlob.id.in_(
                select(NotePartIndexPointer.blob_id).where(
                    NotePartIndexPointer.note_id == note_id,
                    NotePartIndexPointer.org_id == org_id,
                )
            ),
        )
        .values(project_id=project_id)
    )


# ---------------------------------------------------------------- tag wiring


async def _attach_inherited_tags(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    blob_id: uuid.UUID,
    note_id: uuid.UUID,
) -> None:
    """Copy the note's tags onto the part blob (faceted-search parity with
    task blobs, which inherit the task's tags)."""
    from mycelium_core.models.memory_blob import MemoryBlobTag
    from mycelium_core.models.note_tag import NoteTag

    rows = (
        (await session.execute(select(NoteTag.tag_id).where(NoteTag.note_id == note_id)))
        .scalars()
        .all()
    )
    for tid in set(rows):
        session.add(MemoryBlobTag(blob_id=blob_id, org_id=org_id, tag_id=tid))
    if rows:
        await session.flush()


async def _attach_channel_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    blob_id: uuid.UUID,
) -> None:
    """Pin the blob to the ``memory_channel`` tag with system_key='note'
    (seeded by ``taxonomy.ensure_default_memory_channels``). Ensure-seed
    here to keep the resync self-contained."""
    from mycelium_core.models.memory_blob import MemoryBlobTag
    from mycelium_core.models.tag import Tag, TagKind
    from mycelium_core.services import taxonomy

    await taxonomy.ensure_default_memory_channels(session, org_id=org_id)
    channel_id = (
        await session.execute(
            select(Tag.id).where(Tag.kind == TagKind.memory_channel, Tag.system_key == "note")
        )
    ).scalar_one_or_none()
    if channel_id is None:
        return
    try:
        async with session.begin_nested():
            session.add(MemoryBlobTag(blob_id=blob_id, org_id=org_id, tag_id=channel_id))
            await session.flush()
    except IntegrityError:
        return


# ---------------------------------------------------------------- backfill


async def run_pointer_backfill(session: AsyncSession, *, batch_size: int = 50) -> int:
    """Index note parts that don't have a ``note_part_index_pointer`` yet.

    The listener path catches every new mutation, but parts that pre-date
    this deploy never went through it. This sweep picks the first
    ``batch_size`` unindexed parts, skipping the ones whose note is at
    ``index_scope='none'`` (those have no pointer by design), and runs
    the same ``_resync_part_blob`` the listener would have. Returns the
    count indexed in this batch."""
    rows = (
        await session.execute(
            select(NotePart.id)
            .outerjoin(NotePartIndexPointer, NotePartIndexPointer.part_id == NotePart.id)
            .join(Note, Note.id == NotePart.note_id)
            .where(
                NotePartIndexPointer.part_id.is_(None),
                # A scoped-out part has no pointer by definition, so without
                # this term it stays a candidate on every tick, fills the
                # batch (which has no ORDER BY) and starves the real backlog.
                # The guard in the resync makes it harmless; this makes it
                # free. Inner join: ``note_part.note_id`` is NOT NULL and
                # cascades, so a part without a note does not outlive it.
                Note.index_scope != IndexScope.none,
            )
            .limit(batch_size)
        )
    ).all()
    if not rows:
        return 0
    indexed = 0
    for (part_id,) in rows:
        try:
            await _resync_part_blob(session, part_id)
            indexed += 1
        except Exception:
            logger.exception("note-search pointer backfill failed for part_id=%s", part_id)
    return indexed
