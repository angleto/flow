"""Unified search across tasks, notes and memory blobs.

Wraps the existing memory RRF pipeline with a kind-aware split: ``task``
kind uses the org-wide retrieve (project_id=None) and resolves blobs to
tasks via ``task_index_pointer``; ``note`` kind resolves note-part blobs
to their note via ``note_part_index_pointer`` (per-project); ``blob``
kind keeps the per-project predicate. Snippet is computed server-side via
Postgres ``ts_headline``; the response is typed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from mycelium_api.deps import TenantCtx, tenant_admin_ctx, tenant_ctx
from mycelium_api.schemas import SearchClickIn, SearchHit, SearchIn, TagBrief
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import search_clicks as clicks_svc
from mycelium_core.services import task_search as svc
from mycelium_core.services import tasks as tasks_svc

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=list[SearchHit])
async def search(
    body: SearchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[SearchHit]:
    # `_meta` is still discarded here, and that is a decision rather than an oversight. The
    # response model is a bare array, so carrying the recall meta means wrapping it in an
    # object, and the browser extension is a shipped artifact that consumes `SearchHit[]` and
    # is not redeployed with the server. Breaking it to add a field is not a trade this
    # endpoint gets to make on its own; the MCP surface carries the meta today, and the REST
    # envelope is its own decision with its own compatibility plan.
    hits, _meta = await svc.search_unified_with_meta(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_id=ctx.project_id,
        query=body.q,
        kinds=body.kinds,
        tag_ids=body.tag_ids,
        channel_keys=body.channel_keys,
        limit=body.limit,
        include_archived=body.include_archived,
        include_deleted=body.include_deleted,
        operation_id=body.operation_id,
        rerank=body.rerank,
        task_scope=body.task_scope,
        due_before=body.due_before,
        assignee_handles=body.assignee_handles,
        task_state_id=body.task_state_id,
    )

    # Two batched reads for the whole page rather than one per row. The
    # field was declared on SearchHit from the start and never filled, so
    # a caller that wanted to show which project a result belongs to had
    # to fetch each hit -- twenty-one requests to render twenty rows.
    task_ids = [h.task_id for h in hits if h.task_id is not None]
    note_ids = [h.note_id for h in hits if h.note_id is not None]
    task_tags = await tasks_svc.tags_by_task(ctx.session, task_ids=task_ids) if task_ids else {}
    note_tags = await notes_svc.tags_by_note(ctx.session, note_ids=note_ids) if note_ids else {}

    def _tags(h: svc.UnifiedHit) -> list[TagBrief]:
        # A blob hit is opaque: it has no entity, so it has no tags. An
        # empty list here says "nothing to show", which is the truth,
        # rather than "not loaded".
        if h.task_id is not None:
            rows = task_tags.get(h.task_id, [])
        elif h.note_id is not None:
            rows = note_tags.get(h.note_id, [])
        else:
            return []
        return [TagBrief(id=g.id, kind=g.kind, name=g.name, color=g.color) for g in rows]

    return [
        SearchHit(
            kind=h.kind,
            task_id=h.task_id,
            note_id=h.note_id,
            part_id=h.part_id,
            blob_id=h.blob_id,
            title=h.title,
            snippet=h.snippet,
            score=h.score,
            tags=_tags(h),
            scores_by_stage=h.scores_by_stage,
            scope=h.scope,
            model_id=h.model_id,
        )
        for h in hits
    ]


@router.post("/click", status_code=status.HTTP_204_NO_CONTENT)
async def log_click(
    body: SearchClickIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> Response:
    """Record one search-result click (ADR-0035 ``recall_at_k``,
    task 89508ca9): which query led to which entity, at which rank of
    the shown top-K. Append-only telemetry, fire-and-forget from the
    SPA; the nightly garden-health snapshot aggregates it."""
    await clicks_svc.log_click(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        query=body.q,
        hit_kind=body.hit_kind,
        hit_id=body.hit_id,
        rank=body.rank,
        result_count=body.result_count,
        is_probe=body.is_probe,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class ReindexOut(BaseModel):
    """Result of a one-shot pointer backfill. ``indexed`` is the count
    of tasks that got a pointer + blob in this call; if it equals
    ``batch_size`` there are more tasks to process and the caller
    should re-invoke (or wait for the periodic worker tick)."""

    indexed: int
    batch_size: int


class ReindexIn(BaseModel):
    batch_size: int = Field(default=200, ge=1, le=2000)


@router.post("/reindex", response_model=ReindexOut)
async def reindex(
    body: ReindexIn,
    ctx: Annotated[TenantCtx, Depends(tenant_admin_ctx)],
) -> ReindexOut:
    """Index every task that pre-dates the task-search deploy in this
    workspace. Admin-gated (the same sudo lever used by other tenant
    maintenance endpoints) because it touches every row.

    Runs the same ``_resync_task_blob`` the listener path runs on a
    fresh mutation, in one transaction, capped at ``batch_size``
    (default 200). Idempotent: tasks that already have a pointer are
    skipped by the SELECT itself.
    """
    indexed = await svc.run_pointer_backfill(ctx.session, batch_size=body.batch_size)
    return ReindexOut(indexed=indexed, batch_size=body.batch_size)
