"""Note multi-part CRUD + reorder + per-user collapse state.

Phase 2a of the parts cluster (task 71c9d670, parent c0459c4b, design
note 2d228758). Service-layer entry points the REST router + future
MCP tools / CLI dial into. The ``notes.transcript`` column stays as
the source of truth in this phase; the parts table is the new
canonical surface that Phase 6 (task 1cd8bc0a) will promote.

Concurrency: ``NotePart`` carries ``VersionMixin``; mutations use
``optimistic_update`` exactly like ``Note`` / ``Task`` so a SPA
autosave sees the same ``stale_version`` semantics here.

Reorder: the SPA hands us the desired ordering as a list of
``part_ids``; we rewrite every part's ``ord`` to its index in that
list inside a single transaction. The DB-side ``UNIQUE (note_id,
ord) DEFERRABLE INITIALLY DEFERRED`` constraint (migration 0011)
means we never need a 'next free' scratchpad value to step through:
PostgreSQL checks the uniqueness only at COMMIT.

UI state is user-scoped, no version, last-write-wins. The absence of
a row means "expanded" — only an explicit toggle materialises the
row, so the table stays tiny on first visit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.concurrency import optimistic_update
from mycelium_core.config import get_settings
from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.identity import Identity
from mycelium_core.models.membership import Role
from mycelium_core.models.note import Note
from mycelium_core.models.note_part import NotePart, NotePartTrash, NotePartUIState
from mycelium_core.services import audit, note_search, text_patch
from mycelium_core.services.note_effective import effective_note_clause
from mycelium_core.services.rbac import require_role


class _Unset:
    """Sentinel for omit-vs-explicit-None on the patch path. Mirrors
    the pattern in services.notes for symmetric semantics."""


_UNSET: Any = _Unset()


async def list_parts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    include_deleted: bool = False,
    include_proposed: bool = False,
) -> list[NotePart]:
    """Return the parts of a note in ``ord`` order. Member-level
    (RLS already scopes the SELECT to the tenant).

    Empty unless the note is EFFECTIVE (task a186c989): the parts are
    where a note's text actually lives, so a table that does not know
    about ``notes`` hands out the body of a note in the bin or of an
    un-approved proposal -- which is precisely what the blob-side
    perimeter already refuses to retrieve.

    Both opt-ins have ONE legitimate caller,
    ``entity_revisions.snapshot_note``, and for the same reason: a
    snapshot must photograph the parts that are there whatever the note's
    state, or the revision restores as an empty note. The delete revision
    is written AFTER ``deleted_at`` is set (hence ``include_deleted``),
    and the same trap waits on the review axis (hence
    ``include_proposed``).
    """
    rows = (
        (
            await session.execute(
                select(NotePart)
                .join(Note, Note.id == NotePart.note_id)
                .where(
                    NotePart.note_id == note_id,
                    NotePart.org_id == org_id,
                    effective_note_clause(
                        include_deleted=include_deleted, include_proposed=include_proposed
                    ),
                )
                .order_by(NotePart.ord, NotePart.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def parts_by_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[NotePart]]:
    """Batched ``{note_id: [parts]}`` for the list endpoints. One
    query, ordered so each note's value is already sorted.

    Effective notes only, exactly like its single-note twin
    ``list_parts``: a batch reader must not be the cheap way around the
    perimeter just because it takes many ids at once (task a186c989).
    """
    if not note_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(NotePart)
                .join(Note, Note.id == NotePart.note_id)
                .where(
                    NotePart.org_id == org_id,
                    NotePart.note_id.in_(list(note_ids)),
                    effective_note_clause(),
                )
                .order_by(NotePart.note_id, NotePart.ord, NotePart.id)
            )
        )
        .scalars()
        .all()
    )
    out: dict[uuid.UUID, list[NotePart]] = {}
    for part in rows:
        out.setdefault(part.note_id, []).append(part)
    return out


async def _get_note_in_org(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    include_deleted: bool = False,
    include_proposed: bool = False,
) -> Note:
    """The owning note, and it has to be EFFECTIVE (task a186c989): a note
    in the bin is restored first and worked on afterwards, and an
    un-approved proposal is not editable at all -- that leg used to be
    enforced by accident, from inside the revision logger, AFTER the write
    had already started.

    ``include_deleted`` is for the two places that must see a note on its
    way out: the revision logger (``merge_notes`` soft-deletes the source
    and then writes its outgoing revision) and the merge idempotency
    branch (a re-run of an already-merged source must stay a no-op, not
    become a 404). ``include_proposed`` is the photographer's opt-out,
    used by the revision logger only.
    """
    note = (
        await session.execute(
            select(Note).where(
                Note.id == note_id,
                Note.org_id == org_id,
                effective_note_clause(
                    include_deleted=include_deleted, include_proposed=include_proposed
                ),
            )
        )
    ).scalar_one_or_none()
    if note is None:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return note


async def _get_part(session: AsyncSession, *, org_id: uuid.UUID, part_id: uuid.UUID) -> NotePart:
    """One part, by id, only while its note is EFFECTIVE (task a186c989).

    A part id is not a permission to read the note it belongs to. This
    join covers every part-addressed surface that flows through here --
    the ``get_note_part`` tool, the raw-body download and its capability
    token (checked at READ time, so a token minted before the note was
    trashed stops working immediately), the body write/patch paths and
    the annotation apply -- and ``delete_part`` / ``_get_trashed`` carry
    the same clause, so reading, trashing, restoring and purging a part
    all answer to one perimeter instead of three.
    """
    part = (
        await session.execute(
            select(NotePart)
            .join(Note, Note.id == NotePart.note_id)
            .where(
                NotePart.id == part_id,
                NotePart.org_id == org_id,
                effective_note_clause(),
            )
        )
    ).scalar_one_or_none()
    if part is None:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return part


async def _get_trashed(
    session: AsyncSession, *, org_id: uuid.UUID, part_id: uuid.UUID
) -> NotePartTrash:
    """A trashed part, while its note is EFFECTIVE. The side table keeps
    the whole body, so it is a read surface like any other, and it is the
    branch a purge takes when the part is already in the bin -- gating it
    keeps read, restore and purge on one perimeter (task a186c989)."""
    entry = (
        await session.execute(
            select(NotePartTrash)
            .join(Note, Note.id == NotePartTrash.note_id)
            .where(
                NotePartTrash.id == part_id,
                NotePartTrash.org_id == org_id,
                effective_note_clause(),
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise NotFoundError(MessageCode.NOTE_PART_NOT_TRASHED)
    return entry


async def get_part(session: AsyncSession, *, org_id: uuid.UUID, part_id: uuid.UUID) -> NotePart:
    """Read a single part by id: random access into a long note's body
    without fetching every other part. Member-level; RLS already scopes
    the SELECT to the tenant. Raises ``NOTE_NOT_FOUND`` for an unknown or
    foreign part id."""
    return await _get_part(session, org_id=org_id, part_id=part_id)


async def _log_parts_revision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    changed_fields: list[str],
    channel: str = "api",
) -> None:
    """Recovery-history row for a STRUCTURAL change to a note's parts.

    The body mutators (update / append / prepend / replace) have always
    written one; create, reorder, merge, trash, restore and purge did
    not, which left three holes: the timeline claimed nothing happened,
    a mis-click was unrecoverable, and -- now that a snapshot carries
    ``parts`` and not only the flat ``transcript`` -- a restore of the
    preceding revision is what actually undoes them. Every mutation of a
    note's body, content OR structure, writes a row here.

    ``version_from == version_to``: parts carry their own
    ``VersionMixin``, so a part change never bumps the note row's
    version; the snapshot is what records the change. Channel ``api``
    (not ``web``) on purpose -- these are discrete structural acts, not
    keystrokes, so each seals its own row instead of coalescing into an
    autosave window.
    """
    from mycelium_core.services.notes import _log_note_revision

    # The revision logger runs from every mutator, and one of those flows
    # must be able to see a note on its way out: ``merge_notes`` soft-
    # deletes the source and only then writes its outgoing revision.
    note = await _get_note_in_org(
        session,
        org_id=org_id,
        note_id=note_id,
        include_deleted=True,
        include_proposed=True,
    )
    await _log_note_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        version_from=note.version,
        version_to=note.version,
        changed_fields=changed_fields,
        channel=channel,
        edit_session_id=None,
    )


async def _assert_not_promoted(
    session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID
) -> None:
    """Enforce the read-only invariant of a transplanted note (docs/adr/0029
    D2): a note promoted to a task (``promoted_at IS NOT NULL``) is read-only
    at the service layer, so every CONTENT mutation (note title/body + any
    part create/update/append/prepend/replace/delete/reorder/merge) is
    refused with ``NOTE_PROMOTED_READONLY`` -- mirroring the existing
    ``set_maturity`` / link guards in ``note_links``. A missing note resolves
    to ``None`` here and is left to the mutator's own ``NOT_FOUND`` path."""
    promoted_at = (
        await session.execute(
            select(Note.promoted_at).where(Note.id == note_id, Note.org_id == org_id)
        )
    ).scalar_one_or_none()
    if promoted_at is not None:
        raise DomainError(MessageCode.NOTE_PROMOTED_READONLY)


async def _actor_identity_id(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> uuid.UUID | None:
    """Resolve the caller's Identity row in this org (ADR-0028).

    The twin of ``note_links._actor_identity_id`` and it exists for the
    same reason: ``actor_id`` throughout these services is a ``users``
    id, while every ``created_by`` in the note family is an Identity --
    a user or an ai_assistant. Writing the actor straight into the
    column is a foreign key violation, which is how this was found.

    Returns None when the actor has no Identity in the workspace. The
    column is nullable precisely so that an absence is recordable
    rather than fatal: attribution is worth having and is not worth
    refusing a write for.
    """
    return (
        await session.execute(
            select(Identity.id).where(
                Identity.org_id == org_id,
                Identity.user_id == actor_id,
            )
        )
    ).scalar_one_or_none()


async def create_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    body: str,
    title: str | None = None,
    lang: str | None = None,
    ord: int | None = None,
) -> NotePart:
    """Append a new part to the note. When ``ord`` is omitted the
    part lands at the end (max(existing ord) + 1, or 0 if empty);
    when supplied it inserts at that position, pushing every part
    with ord >= ``ord`` forward by one. The push is a single UPDATE
    against the deferred-unique constraint."""
    await require_role(session, org_id, actor_id, Role.member)
    await _get_note_in_org(session, org_id=org_id, note_id=note_id)
    await _assert_not_promoted(session, org_id=org_id, note_id=note_id)
    if ord is None:
        max_ord = (
            await session.execute(select(func.max(NotePart.ord)).where(NotePart.note_id == note_id))
        ).scalar()
        target_ord = 0 if max_ord is None else int(max_ord) + 1
    else:
        if ord < 0:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        target_ord = ord
        # Shift everyone at >= target_ord up by one (deferred unique
        # constraint tolerates the transient collision until COMMIT).
        await session.execute(
            update(NotePart)
            .where(
                NotePart.note_id == note_id,
                NotePart.org_id == org_id,
                NotePart.ord >= target_ord,
            )
            .values(ord=NotePart.ord + 1)
            .execution_options(synchronize_session=False)
        )
    # Migration 0010. The actor was always in scope here and was never
    # written down, which left a shared work note orderable but not
    # attributable. ``created_by`` is an Identity (ADR-0028), not the
    # ``users`` row ``actor_id`` names, so it goes through the same
    # resolver the note links use; None when the actor has no identity
    # in this workspace, which the column tolerates.
    identity_id = await _actor_identity_id(session, org_id=org_id, actor_id=actor_id)
    part = NotePart(
        org_id=org_id,
        note_id=note_id,
        ord=target_ord,
        title=title,
        body=body,
        lang=lang,
        created_by=identity_id,
    )
    session.add(part)
    await session.flush()
    await _log_parts_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        changed_fields=[f"parts[{target_ord}]._create"],
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part.id,
        action="create",
        diff={"note_id": str(note_id), "ord": str(target_ord)},
    )
    return part


async def apply_patch_to_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
    expected_version: int,
    patch: str,
    base_sha256: str,
    channel: str = "api",
    edit_session_id: str | None = None,
) -> int:
    """Apply a strict unified diff to a part's body and persist via
    :func:`update_part`. The sha256 base gate (``base_sha256`` against the
    live body) raises ConflictError(PATCH_STALE) on drift; the version gate
    is re-asserted by ``update_part``'s ``optimistic_update``. Both the
    body write and the caller's capability-token consume share the session
    transaction, so any raise rolls everything back (the token is not
    burned)."""
    part = await _get_part(session, org_id=org_id, part_id=part_id)
    new_body = text_patch.apply_patch_text(
        part.body or "",
        patch,
        expected_sha256=base_sha256,
        max_result_bytes=get_settings().note_body_max_bytes,
    )
    return await update_part(
        session,
        org_id=org_id,
        actor_id=actor_id,
        part_id=part_id,
        expected_version=expected_version,
        body=new_body,
        channel=channel,
        edit_session_id=edit_session_id,
    )


async def update_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
    expected_version: int,
    body: str | None = None,
    title: str | None | _Unset = _UNSET,
    lang: str | None | _Unset = _UNSET,
    channel: str = "api",
    edit_session_id: str | None = None,
) -> int:
    """Edit a part's ``body`` and/or ``lang``. Returns the new
    version. ``lang`` uses an "omit" sentinel so the caller can
    explicitly clear it (pass None as a JSON null).

    ``channel`` + ``edit_session_id`` flow through to the parent
    note's recovery-history revision so a debounced SPA autosave
    coalesces into a single open revision instead of stamping a
    sealed row per keystroke. The part row's own ``version`` still
    bumps on every save (optimistic_update); only the note-level
    revision row coalesces."""
    await require_role(session, org_id, actor_id, Role.member)
    part = await _get_part(session, org_id=org_id, part_id=part_id)
    await _assert_not_promoted(session, org_id=org_id, note_id=part.note_id)
    values: dict[str, Any] = {}
    if body is not None:
        values["body"] = body
    if not isinstance(title, _Unset):
        values["title"] = title
    if not isinstance(lang, _Unset):
        values["lang"] = lang
    if not values:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    # A write that changes nothing is not a write. Without this an
    # interactive client that re-sends the body it was given (an editor
    # whose serializer happens to reproduce it, a retried request, a
    # "Save" on an untouched form) bumps ``version`` and stamps a
    # recovery-history row that says an edit happened when none did --
    # which is exactly the noise that made the silent-rewrite incident
    # hard to read. The version gate above has already run, so callers
    # still get a conflict on stale writes; this only collapses the
    # genuinely idempotent case.
    if all(getattr(part, field) == value for field, value in values.items()):
        return int(part.version)
    new_version = await optimistic_update(
        session,
        NotePart,
        pk=part_id,
        expected_version=expected_version,
        values=values,
    )
    note_search.mark_note_part_dirty(session, part_id)
    # Record a note-level revision so the timeline reflects part
    # edits. version_from == version_to: the note's row version is
    # not bumped by part changes (parts carry their own VersionMixin),
    # but the snapshot (which derives ``transcript`` from parts)
    # captures the new body. Lazy import: avoids a hard import cycle
    # between note_parts and notes via entity_revisions.
    from mycelium_core.services.notes import _log_note_revision

    note = await _get_note_in_org(session, org_id=org_id, note_id=part.note_id)
    await _log_note_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=part.note_id,
        version_from=note.version,
        version_to=note.version,
        # Tag the timeline with the edited part's position (``ord``,
        # the ``#N`` chip the editor shows) and every field that
        # changed, so a recovery row reads "Part 5: body" instead of a
        # bare ``parts.body``. ``ord`` is captured at edit time: a later
        # reorder does not rewrite history. The previous form logged a
        # single field even when body+title+lang changed together; the
        # comprehension records them all.
        changed_fields=[
            f"parts[{part.ord}].{field}" for field in ("body", "title", "lang") if field in values
        ],
        channel=channel,
        edit_session_id=edit_session_id,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part_id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


async def append_to_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
    chunk: str,
    expected_version: int,
    is_last: bool = True,
    operation_id: str | None = None,
    channel: str = "api",
) -> tuple[int, int]:
    """Append ``chunk`` to a part's body WITHOUT resending the existing
    body. Built for streaming a markdown file past the MCP/JSON-RPC
    per-``tools/call`` payload cap (~100k chars): the client splits the
    file into ordered chunks and appends them in sequence, each asserting
    ``expected_version`` (the cursor). Returns ``(new_version,
    appended_chars)``.

    Chunks are concatenated **raw** (no separator) so the reassembled
    body is byte-for-byte identical to the source file.

    Idempotency (retry-safe, no extra storage): a replay of the same
    chunk at the same cursor -- the part version is exactly one ahead of
    ``expected_version`` AND the body already ends with ``chunk`` -- is a
    no-op returning ``(current_version, 0)``.

    Concurrency: any other version mismatch raises ``stale_version`` (no
    last-write-wins), so two writers racing the same part cannot silently
    interleave.

    History: the note-level recovery revision is logged once, on the
    final chunk (``is_last=True``), so a 16-chunk upload seals a single
    revision capturing the final body rather than 16 growing snapshots.
    ``operation_id`` flows through as the revision's ``edit_session_id``.

    Refuses with ``body.limit_exceeded`` when the resulting body would
    exceed ``MYCELIUM_NOTE_BODY_MAX_BYTES``.
    """
    await require_role(session, org_id, actor_id, Role.member)
    part = await _get_part(session, org_id=org_id, part_id=part_id)
    await _assert_not_promoted(session, org_id=org_id, note_id=part.note_id)
    body = part.body or ""
    # Idempotent replay: cursor advanced by exactly one and the tail is
    # already this chunk -> the previous attempt landed; treat as no-op.
    if part.version == expected_version + 1 and chunk and body.endswith(chunk):
        return part.version, 0
    new_body = body + chunk
    max_bytes = get_settings().note_body_max_bytes
    if len(new_body.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    new_version = await optimistic_update(
        session,
        NotePart,
        pk=part_id,
        expected_version=expected_version,
        values={"body": new_body},
    )
    if is_last:
        # Re-index the part only on the final chunk so a 16-chunk upload
        # triggers a single re-embed, not N (the original 27f4d6c9 contract).
        note_search.mark_note_part_dirty(session, part_id)
        # Lazy import: avoids the note_parts <-> notes import cycle.
        from mycelium_core.services.notes import _log_note_revision

        note = await _get_note_in_org(session, org_id=org_id, note_id=part.note_id)
        await _log_note_revision(
            session,
            org_id=org_id,
            actor_id=actor_id,
            note_id=part.note_id,
            version_from=note.version,
            version_to=note.version,
            # ``parts[ord].body``: tag the timeline with the edited part's
            # position so a recovery row reads "Part N: body" (see update_part).
            changed_fields=[f"parts[{part.ord}].body"],
            channel=channel,
            edit_session_id=operation_id,
        )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part_id,
        action="append",
        diff={"appended_chars": str(len(chunk)), "is_last": str(is_last)},
    )
    return new_version, len(chunk)


async def prepend_to_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
    text: str,
    expected_version: int,
    operation_id: str | None = None,
    channel: str = "api",
) -> tuple[int, int]:
    """Prepend ``text`` to the FRONT of a part's body without resending
    the existing body (task 5662a07f: partial writes). Single-shot --
    the natural shape for adding a header / intro on top; for very large
    front-matter, chunk-append into a fresh part and reorder instead.

    ``text`` is concatenated raw before the current body (no separator),
    so the caller controls any trailing newline. Concurrency-safe via
    ``expected_version`` (a mismatch raises ``stale_version``, no
    last-write-wins). Returns ``(new_version, prepended_chars)``. Refuses
    with ``body.limit_exceeded`` past ``MYCELIUM_NOTE_BODY_MAX_BYTES``.
    """
    await require_role(session, org_id, actor_id, Role.member)
    part = await _get_part(session, org_id=org_id, part_id=part_id)
    await _assert_not_promoted(session, org_id=org_id, note_id=part.note_id)
    body = part.body or ""
    new_body = text + body
    max_bytes = get_settings().note_body_max_bytes
    if len(new_body.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    new_version = await optimistic_update(
        session,
        NotePart,
        pk=part_id,
        expected_version=expected_version,
        values={"body": new_body},
    )
    note_search.mark_note_part_dirty(session, part_id)
    # Single-shot: seal the recovery revision now (unlike chunked append,
    # which defers to is_last).
    from mycelium_core.services.notes import _log_note_revision

    note = await _get_note_in_org(session, org_id=org_id, note_id=part.note_id)
    await _log_note_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=part.note_id,
        version_from=note.version,
        version_to=note.version,
        # ``parts[ord].body``: tag the timeline with the edited part's
        # position so a recovery row reads "Part N: body" (see update_part).
        changed_fields=[f"parts[{part.ord}].body"],
        channel=channel,
        edit_session_id=operation_id,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part_id,
        action="prepend",
        diff={"prepended_chars": str(len(text))},
    )
    return new_version, len(text)


async def replace_in_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
    find: str,
    replace: str,
    expected_version: int,
    count: int = 0,
    operation_id: str | None = None,
    channel: str = "api",
) -> tuple[int, int]:
    """Anchored find/replace inside ONE part without resending the body
    (task 5662a07f): swap the literal ``find`` for ``replace``. ``count``
    <= 0 replaces every occurrence; a positive ``count`` only the first N.
    Concurrency-safe via ``expected_version`` (mismatch raises
    ``stale_version``, no last-write-wins). Returns ``(new_version,
    replacements)``.

    A no-op -- ``find`` empty or absent from the body -- returns
    ``(current_version, 0)`` WITHOUT bumping the version and without
    asserting ``expected_version`` (nothing changed, so nothing to race).
    Refuses with ``body.limit_exceeded`` when the result would exceed
    ``MYCELIUM_NOTE_BODY_MAX_BYTES`` (a replacement can grow the body).
    """
    await require_role(session, org_id, actor_id, Role.member)
    part = await _get_part(session, org_id=org_id, part_id=part_id)
    await _assert_not_promoted(session, org_id=org_id, note_id=part.note_id)
    body = part.body or ""
    occurrences = body.count(find) if find else 0
    if occurrences == 0:
        return part.version, 0
    n = occurrences if count <= 0 else min(count, occurrences)
    new_body = body.replace(find, replace) if count <= 0 else body.replace(find, replace, count)
    max_bytes = get_settings().note_body_max_bytes
    if len(new_body.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    new_version = await optimistic_update(
        session,
        NotePart,
        pk=part_id,
        expected_version=expected_version,
        values={"body": new_body},
    )
    note_search.mark_note_part_dirty(session, part_id)
    # Single-shot edit: seal the recovery revision now (same pattern as
    # prepend_to_part). version_from == version_to: the note row's own
    # version is unchanged by part edits.
    from mycelium_core.services.notes import _log_note_revision

    note = await _get_note_in_org(session, org_id=org_id, note_id=part.note_id)
    await _log_note_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=part.note_id,
        version_from=note.version,
        version_to=note.version,
        # ``parts[ord].body``: tag the timeline with the edited part's
        # position so a recovery row reads "Part N: body" (see update_part).
        changed_fields=[f"parts[{part.ord}].body"],
        channel=channel,
        edit_session_id=operation_id,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part_id,
        action="replace",
        diff={"replacements": str(n)},
    )
    return new_version, n


async def trash_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
    expected_version: int | None = None,
) -> None:
    """Remove a part from the note, RESTORABLY: the row moves to
    ``note_part_trash`` (migration 0089) keeping its id, ord, body,
    title, lang and version, and :func:`restore_part` puts it back.
    This is the ordinary delete verb for a part -- the inverse pair
    ``Note`` and ``Task`` already had, and the reason part removal no
    longer has to be fenced behind a danger scope.

    The remaining parts keep their ords (no automatic compaction) so
    any deep-link via ord survives, and so a restore can aim at the
    slot the part came from; reorder stays an explicit operation.

    ``expected_version`` is an optional optimistic-concurrency guard:
    supply it to refuse trashing a part someone edited since you read
    it (``stale_version``). Omitting it trashes unconditionally, which
    is the contract the REST DELETE and the SPA already rely on.

    The search blob goes with the part: a trashed part must not keep
    surfacing in ``memory_search``. :func:`restore_part` re-indexes,
    which mints a NEW blob -- the content round-trips losslessly, the
    blob's clustering and access counters do not.
    """
    await require_role(session, org_id, actor_id, Role.member)
    part = await _get_part(session, org_id=org_id, part_id=part_id)
    await _assert_not_promoted(session, org_id=org_id, note_id=part.note_id)
    if expected_version is not None and part.version != expected_version:
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION, current_version=int(part.version))
    session.add(
        NotePartTrash(
            id=part.id,
            org_id=org_id,
            note_id=part.note_id,
            ord=part.ord,
            title=part.title,
            body=part.body,
            lang=part.lang,
            merged_from_note_id=part.merged_from_note_id,
            part_version=part.version,
            trashed_by=actor_id,
        )
    )
    # Drop the part's search blob inline first: the index pointer cascades
    # with the part row (FK ON DELETE CASCADE), so a deferred flush would
    # no longer resolve the blob to delete. Deleting the blob now cascades
    # the pointer, so the part DELETE below has nothing left to cascade.
    await note_search.delete_part_index_now(session, part_id)
    await session.execute(delete(NotePart).where(NotePart.id == part_id, NotePart.org_id == org_id))
    await session.flush()
    await _log_parts_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=part.note_id,
        changed_fields=[f"parts[{part.ord}]._trash"],
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part_id,
        action="trash",
        diff={"note_id": str(part.note_id), "ord": str(part.ord)},
    )


async def restore_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
) -> NotePart:
    """Put a trashed part back into its note -- the exact inverse of
    :func:`trash_part`. The part returns with its ORIGINAL id, body,
    title, lang and version, so ids captured before the trash resolve
    again and a stale ``expected_version`` still loses.

    Placement: the part aims at the ord it held when trashed. If that
    slot was taken in the meantime, everything at or after it shifts
    forward by one (the same deferred-unique shift :func:`create_part`
    uses for insert-at-ord), so the part lands where it was relative
    to its neighbours instead of being appended at the end.

    Raises ``note.part.not_trashed`` when no trash entry matches:
    an unknown id, one already restored, or one purged by
    :func:`delete_part`.
    """
    await require_role(session, org_id, actor_id, Role.member)
    entry = await _get_trashed(session, org_id=org_id, part_id=part_id)
    await _assert_not_promoted(session, org_id=org_id, note_id=entry.note_id)
    # The note itself may have been deleted (and its trash rows cascade
    # with it), but a note can also have been emptied of parts; validate
    # it is still there so the re-INSERT cannot violate the FK.
    await _get_note_in_org(session, org_id=org_id, note_id=entry.note_id)
    occupied = (
        await session.execute(
            select(NotePart.id).where(
                NotePart.note_id == entry.note_id,
                NotePart.org_id == org_id,
                NotePart.ord == entry.ord,
            )
        )
    ).scalar_one_or_none()
    if occupied is not None:
        await session.execute(
            update(NotePart)
            .where(
                NotePart.note_id == entry.note_id,
                NotePart.org_id == org_id,
                NotePart.ord >= entry.ord,
            )
            .values(ord=NotePart.ord + 1)
            .execution_options(synchronize_session=False)
        )
    part = NotePart(
        id=entry.id,
        org_id=org_id,
        note_id=entry.note_id,
        ord=entry.ord,
        title=entry.title,
        body=entry.body,
        lang=entry.lang,
        merged_from_note_id=entry.merged_from_note_id,
        version=entry.part_version,
    )
    session.add(part)
    await session.execute(
        delete(NotePartTrash).where(NotePartTrash.id == part_id, NotePartTrash.org_id == org_id)
    )
    await session.flush()
    # Re-index: trash dropped the blob, so this mints a fresh one from
    # the restored row on the next search flush.
    note_search.mark_note_part_dirty(session, part.id)
    await _log_parts_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=entry.note_id,
        changed_fields=[f"parts[{entry.ord}]._restore"],
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part_id,
        action="restore",
        diff={"note_id": str(entry.note_id), "ord": str(entry.ord)},
    )
    return part


async def list_trashed(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
) -> list[NotePartTrash]:
    """Trashed parts of a note, most recently trashed first. Without
    this a restore would be undiscoverable: the caller that trashed the
    part is rarely the one that wants it back.

    The owning note must be EFFECTIVE (task a186c989). The two bins are
    different axes and this side table carries the full body: a part
    thrown away from a LIVE note is what it is for (migration 0089:
    "restoring a part into a note that no longer exists is not a
    thing"), so a note in the bin is restored first.
    """
    rows = (
        (
            await session.execute(
                select(NotePartTrash)
                .join(Note, Note.id == NotePartTrash.note_id)
                .where(
                    NotePartTrash.note_id == note_id,
                    NotePartTrash.org_id == org_id,
                    effective_note_clause(),
                )
                .order_by(NotePartTrash.trashed_at.desc(), NotePartTrash.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def delete_part(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    part_id: uuid.UUID,
) -> None:
    """PURGE a part: irreversible, no trash entry, nothing to restore
    from. This is what the ``delete:notes`` danger key fences; the
    restorable delete is :func:`trash_part`.

    Accepts a part in either state. A live part is destroyed outright
    (its search blob with it); an already-trashed one has its trash
    entry destroyed, so a purge never needs a restore-then-delete
    dance. The remaining parts keep their ords in both cases.
    """
    await require_role(session, org_id, actor_id, Role.member)
    # Same perimeter as every other part-addressed operation (task
    # a186c989). A purge is the most destructive of them, so it must not
    # be the one door left open: reading a gated note's part is refused,
    # and so is destroying it.
    part = (
        await session.execute(
            select(NotePart)
            .join(Note, Note.id == NotePart.note_id)
            .where(
                NotePart.id == part_id,
                NotePart.org_id == org_id,
                effective_note_clause(),
            )
        )
    ).scalar_one_or_none()
    if part is None:
        # Not live: purge the trash entry, or 404 if there is none.
        entry = await _get_trashed(session, org_id=org_id, part_id=part_id)
        await session.execute(
            delete(NotePartTrash).where(NotePartTrash.id == part_id, NotePartTrash.org_id == org_id)
        )
        await session.flush()
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note_part",
            entity_id=part_id,
            action="purge",
            diff={"note_id": str(entry.note_id), "ord": str(entry.ord), "from": "trash"},
        )
        return
    await _assert_not_promoted(session, org_id=org_id, note_id=part.note_id)
    # Drop the part's search blob inline first: the index pointer cascades
    # with the part row (FK ON DELETE CASCADE), so a deferred flush would
    # no longer resolve the blob to delete. Deleting the blob now cascades
    # the pointer, so the part DELETE below has nothing left to cascade.
    await note_search.delete_part_index_now(session, part_id)
    await session.execute(delete(NotePart).where(NotePart.id == part_id, NotePart.org_id == org_id))
    await session.flush()
    await _log_parts_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=part.note_id,
        changed_fields=[f"parts[{part.ord}]._purge"],
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_part",
        entity_id=part_id,
        action="delete",
        diff={"note_id": str(part.note_id), "ord": str(part.ord)},
    )


async def reorder_parts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    part_ids: Sequence[uuid.UUID],
) -> list[NotePart]:
    """Rewrite every part's ``ord`` so the sequence matches
    ``part_ids`` exactly. The caller is expected to send the FULL
    ordering (all current parts, in the desired order); a missing or
    extra part raises DOMAIN_ERROR so the SPA can't accidentally
    drop a row by reordering.

    Implementation: bump every targeted row to ``i + 1_000_000`` to
    move them out of the deferred-unique check's collision window
    in one pass, then bring them down to ``i`` in a second pass.
    Two UPDATEs is cheaper than ``len(parts)`` individual ones and
    survives any concurrent write because the deferred constraint
    is the final check at COMMIT.
    """
    await require_role(session, org_id, actor_id, Role.member)
    await _get_note_in_org(session, org_id=org_id, note_id=note_id)
    await _assert_not_promoted(session, org_id=org_id, note_id=note_id)
    existing = await list_parts(session, org_id=org_id, note_id=note_id)
    if {p.id for p in existing} != set(part_ids):
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if len(part_ids) != len(set(part_ids)):
        raise DomainError(MessageCode.DOMAIN_ERROR)
    # Two-pass swap. The deferred constraint tolerates collisions
    # mid-transaction; the high-ord pass would still be safe under an
    # IMMEDIATE constraint (no duplicates inside the high range), so
    # the pattern works regardless of when the next COMMIT lands.
    HIGH = 1_000_000
    for i, pid in enumerate(part_ids):
        await session.execute(
            update(NotePart)
            .where(NotePart.id == pid, NotePart.org_id == org_id)
            .values(ord=HIGH + i)
            .execution_options(synchronize_session=False)
        )
    for i, pid in enumerate(part_ids):
        await session.execute(
            update(NotePart)
            .where(NotePart.id == pid, NotePart.org_id == org_id)
            .values(ord=i)
            .execution_options(synchronize_session=False)
        )
    await session.flush()
    # The PREVIOUS revision holds the previous ordering, which is the
    # only record of it: reorder rewrites every ord in place and keeps
    # no history of its own.
    await _log_parts_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        changed_fields=["parts._reorder"],
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="parts_reorder",
        diff={"part_ids": ",".join(str(p) for p in part_ids)},
    )
    return await list_parts(session, org_id=org_id, note_id=note_id)


async def set_ui_state(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    part_id: uuid.UUID,
    collapsed: bool,
) -> NotePartUIState:
    """Toggle the user's collapse state for a part. Upsert on
    (user_id, part_id) — last-write-wins, no version. The part must
    belong to a note in this org (RLS already enforces it, but we
    fetch defensively so a stale part_id surfaces as NOT_FOUND)."""
    await require_role(session, org_id, user_id, Role.member)
    await _get_part(session, org_id=org_id, part_id=part_id)
    stmt = (
        pg_insert(NotePartUIState)
        .values(user_id=user_id, part_id=part_id, collapsed=collapsed)
        .on_conflict_do_update(
            index_elements=[NotePartUIState.user_id, NotePartUIState.part_id],
            set_={"collapsed": collapsed, "updated_at": func.now()},
        )
        .returning(NotePartUIState)
    )
    row = (await session.execute(stmt)).scalar_one()
    return row


async def set_ui_states_bulk(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    note_id: uuid.UUID,
    collapsed: bool,
) -> dict[uuid.UUID, bool]:
    """Collapse-all / expand-all: set the caller's collapse state for
    EVERY part of ``note_id`` in a single upsert, so a long note folds
    or unfolds in one round-trip instead of one PUT per part. Returns
    the resulting ``{part_id: collapsed}`` map. User-scoped,
    last-write-wins, same semantics as :func:`set_ui_state` — just the
    note-wide variant. A note with no parts is a no-op (empty map)."""
    await require_role(session, org_id, user_id, Role.member)
    await _get_note_in_org(session, org_id=org_id, note_id=note_id)
    part_ids = list(
        (
            await session.execute(
                select(NotePart.id).where(NotePart.note_id == note_id, NotePart.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    if not part_ids:
        return {}
    stmt = (
        pg_insert(NotePartUIState)
        .values([{"user_id": user_id, "part_id": pid, "collapsed": collapsed} for pid in part_ids])
        .on_conflict_do_update(
            index_elements=[NotePartUIState.user_id, NotePartUIState.part_id],
            set_={"collapsed": collapsed, "updated_at": func.now()},
        )
    )
    await session.execute(stmt)
    return {pid: collapsed for pid in part_ids}


async def get_ui_states_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    note_id: uuid.UUID,
) -> dict[uuid.UUID, bool]:
    """``{part_id: collapsed}`` map for the calling user over the
    parts of ``note_id``. Missing entries default to expanded at
    the caller (no row materialisation needed)."""
    rows = (
        await session.execute(
            select(NotePartUIState.part_id, NotePartUIState.collapsed)
            .join(NotePart, NotePart.id == NotePartUIState.part_id)
            .where(
                NotePartUIState.user_id == user_id,
                NotePart.note_id == note_id,
            )
        )
    ).all()
    return {pid: bool(collapsed) for pid, collapsed in rows}


async def merge_notes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_note_id: uuid.UUID,
    target_note_id: uuid.UUID,
    strategy: str = "append",
) -> Note:
    """Fold the source note's parts into the target note (Phase 2b).

    Strategy ``append`` (only one shipped in this PR; ``interleave``
    raises DOMAIN_ERROR until a clear use case asks for it):
    every source part is moved to the target with a fresh ``ord``
    starting at ``max(target.ord) + 1``, ``merged_from_note_id`` is
    set on each moved part so the audit trail survives a future
    source hard-delete, and the source note is soft-deleted. A
    ``supersedes`` NoteNoteLink (target -> source) records the
    relationship so the graph still shows the lineage.

    Idempotency: a second merge of an already-merged source returns
    the target unchanged (the soft-delete check skips it).

    Refuses self-merge and cross-org merges; both raise DOMAIN_ERROR.
    Single transaction (the caller's session.flush() commits the
    move + soft-delete + supersedes link atomically).
    """
    from mycelium_core.models.note_link import NoteNoteLink

    if strategy != "append":
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if source_note_id == target_note_id:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    # Idempotency: a re-run against an already-merged (soft-deleted) source
    # must stay the documented no-op below, not turn into a 404.
    source = await _get_note_in_org(
        session, org_id=org_id, note_id=source_note_id, include_deleted=True
    )
    target = await _get_note_in_org(session, org_id=org_id, note_id=target_note_id)
    if source.deleted_at is not None:
        # Idempotent: source already merged or deleted earlier.
        return target
    if target.deleted_at is not None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    # A transplanted note is read-only (docs/adr/0029 D2): block a merge that
    # would mutate a promoted source (its parts move out + soft-delete) or a
    # promoted target (it gains the source's parts).
    await _assert_not_promoted(session, org_id=org_id, note_id=source_note_id)
    await _assert_not_promoted(session, org_id=org_id, note_id=target_note_id)

    source_parts = await list_parts(session, org_id=org_id, note_id=source_note_id)
    target_parts = await list_parts(session, org_id=org_id, note_id=target_note_id)
    next_ord = (target_parts[-1].ord + 1) if target_parts else 0
    # Move each source part to the target: keep the body / lang, reset
    # ``ord`` to land at the tail, stamp ``merged_from_note_id``.
    for offset, sp in enumerate(source_parts):
        await session.execute(
            update(NotePart)
            .where(NotePart.id == sp.id, NotePart.org_id == org_id)
            .values(
                note_id=target_note_id,
                ord=next_ord + offset,
                merged_from_note_id=source_note_id,
            )
            .execution_options(synchronize_session=False)
        )
        # Core UPDATE bypasses mapper events: re-point the part's search
        # index to the target note (the resync refreshes note_id + project
        # without re-embedding when the body is unchanged).
        note_search.mark_note_part_dirty(session, sp.id)
    # Soft-delete the source (matches services.notes.soft_delete_note
    # semantics: deleted_at = now(), maturity untouched, FK rows kept).
    await session.execute(
        update(Note)
        .where(Note.id == source_note_id, Note.org_id == org_id)
        .values(deleted_at=func.now())
        .execution_options(synchronize_session=False)
    )
    # Lineage: target supersedes source. Idempotent on the unique
    # (parent, child, kind) triplet.
    existing = (
        await session.execute(
            select(NoteNoteLink).where(
                NoteNoteLink.org_id == org_id,
                NoteNoteLink.parent_note_id == target_note_id,
                NoteNoteLink.child_note_id == source_note_id,
                NoteNoteLink.kind == "supersedes",
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            NoteNoteLink(
                org_id=org_id,
                parent_note_id=target_note_id,
                child_note_id=source_note_id,
                kind="supersedes",
            )
        )
    await session.flush()
    # BOTH notes changed shape, so both get a revision: the target
    # gained parts, the source lost every one of them and was trashed.
    # Without the pair, the only record of a merge was an audit line
    # with two ids in it, and neither note's timeline showed the biggest
    # edit it ever had.
    await _log_parts_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=target_note_id,
        changed_fields=["parts._merge_in"],
    )
    await _log_parts_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=source_note_id,
        changed_fields=["parts._merge_out"],
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=target_note_id,
        action="merge_in",
        diff={
            "source_note_id": str(source_note_id),
            "moved_parts": str(len(source_parts)),
        },
    )
    # Refresh the target so the caller sees the post-merge state
    # (e.g. updated_at, version) consistent with what hit the DB.
    return (
        await session.execute(select(Note).where(Note.id == target_note_id, Note.org_id == org_id))
    ).scalar_one()


__all__ = [
    "append_to_part",
    "create_part",
    "delete_part",
    "get_part",
    "get_ui_states_for_user",
    "list_parts",
    "list_trashed",
    "merge_notes",
    "parts_by_note",
    "prepend_to_part",
    "reorder_parts",
    "replace_in_part",
    "restore_part",
    "set_ui_state",
    "trash_part",
    "update_part",
]
