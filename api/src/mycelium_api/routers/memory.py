"""Memory router: write, hybrid search (retrieval-as-tool), get, GDPR
erase, consolidate, tier recompute, tag curation. Thin adapter
(docs/adr/0001, 0003, 0005, 0007, 0016, FR-8). The (org, project)
predicate is enforced in the service; tags are an orthogonal facet
inside that boundary; the embedder is injected (fake in tests)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from mycelium_api.deps import TenantCtx, tenant_admin_ctx, tenant_ctx
from mycelium_api.schemas import (
    ErasedOut,
    MemoryBlobOut,
    MemoryConsolidateIn,
    MemoryEraseIn,
    MemoryHitOut,
    MemorySearchIn,
    MemoryStatusOut,
    MemoryWriteIn,
    TagBrief,
    TagRefIn,
    TierCountsOut,
)
from mycelium_core.embedder import embedder_available
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.models.tag import Tag
from mycelium_core.services import memory as svc

router = APIRouter(prefix="/memory", tags=["memory"])


def _blob_out(b: MemoryBlob, tags: list[Tag] | None = None) -> MemoryBlobOut:
    return MemoryBlobOut(
        id=b.id,
        project_id=b.project_id,
        namespace=b.namespace,
        tier=b.tier,
        text=b.text,
        summary=b.summary,
        model_id=b.model_id,
        dim=b.dim,
        access_count=b.access_count,
        cluster_id=b.cluster_id,
        created_by=b.created_by,
        origin_model_id=b.origin_model_id,
        tags=[TagBrief(id=g.id, kind=g.kind, name=g.name, color=g.color) for g in (tags or [])],
    )


@router.post("/blobs", response_model=MemoryBlobOut)
async def write_blob(
    body: MemoryWriteIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> MemoryBlobOut:
    blob = await svc.write_blob(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_id=body.project_id,
        text_body=body.text,
        operation_id=body.operation_id,
        namespace=body.namespace,
        sources=body.sources,
        importance=body.importance,
        tag_ids=body.tag_ids,
        channel_tag_id=body.channel_tag_id,
        channel_key=body.channel_key,
    )
    tagmap = await svc.tags_by_blob(ctx.session, blob_ids=[blob.id])
    return _blob_out(blob, tagmap.get(blob.id))


@router.post("/search", response_model=list[MemoryHitOut])
async def search(
    body: MemorySearchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[MemoryHitOut]:
    hits = await svc.retrieve(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_id=body.project_id,
        query=body.query,
        operation_id=body.operation_id,
        limit=body.limit,
        grader_min_rrf=body.grader_min_rrf,
        tag_ids=body.tag_ids,
        channel_tag_id=body.channel_tag_id,
        channel_key=body.channel_key,
        created_by=body.created_by,
    )
    tagmap = await svc.tags_by_blob(ctx.session, blob_ids=[h.blob.id for h in hits])
    return [
        MemoryHitOut(
            blob=_blob_out(h.blob, tagmap.get(h.blob.id)),
            rrf=h.rrf,
            chunk_index=h.chunk_index,
            chunk_snippet=h.chunk_snippet,
            provenance=h.provenance,
        )
        for h in hits
    ]


@router.get("/status", response_model=MemoryStatusOut)
async def status_(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> MemoryStatusOut:
    """Whether semantic retrieval is available (the optional embedding
    model is installed) or memory is running keyword-only. Member-level
    via tenant_ctx; lets the SPA show "semantic vs keyword-only"."""
    return MemoryStatusOut(semantic=embedder_available())


@router.get("/migration-status")
async def migration_status_(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> dict[str, int]:
    """Embedding backfill coverage for this workspace (task 5276207e):
    {total, migrated, pending, hosted, stale}. ``total`` is blobs with
    non-NULL text; ``migrated`` is blobs with the always-on LOCAL vector;
    ``hosted`` is blobs with the optional hosted vector; ``pending`` is the
    local backfill's TODO.

    ``stale`` is blobs that HAVE a local vector written by a model that is
    no longer the active one. They are counted in ``migrated`` too, because
    they are embedded; the number answers a different question, which is
    whether the dense branch is ignoring part of a corpus that reports
    itself fully migrated. It falls to zero as the sweep converges."""
    from mycelium_core.services import embedding_migration as svc

    return await svc.migration_status(ctx.session)


@router.post("/rechunk")
async def rechunk_(
    ctx: Annotated[TenantCtx, Depends(tenant_admin_ctx)],
    source_kind: str = "note",
    batch_size: int = 50,
    dry_run: bool = False,
) -> dict[str, int]:
    """Admin-gated one-shot trigger: re-index legacy whole-doc notes
    through the paragraph chunker (task 2149e753). Returns
    ``{scanned, rechunked, skipped_short, batch_size}``; if
    ``rechunked == batch_size`` more candidates remain -- re-call to
    drain. ``dry_run=true`` reports the count without touching data.
    Idempotent: sources already chunked are skipped by the SELECT."""
    return await svc.rechunk_legacy_sources(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        source_kind=source_kind,
        batch_size=batch_size,
        dry_run=dry_run,
    )


@router.post("/migrate-embeddings")
async def migrate_embeddings_(
    ctx: Annotated[TenantCtx, Depends(tenant_admin_ctx)],
    batch_size: int = 200,
) -> dict[str, int]:
    """Admin-gated one-shot trigger: run the embedding backfill (both
    tiers) on this workspace now (don't wait for the worker tick).
    Returns ``{migrated, batch_size}``; if migrated == batch_size, more
    rows are pending -- re-call to drain."""
    from mycelium_core.services import embedding_migration as svc

    migrated = await svc.run_embedding_backfill(ctx.session, ctx.org_id, batch_size=batch_size)
    return {"migrated": migrated, "batch_size": batch_size}


@router.get("/blobs/{blob_id}", response_model=MemoryBlobOut)
async def get_blob(
    blob_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> MemoryBlobOut:
    blob = await svc.get_blob(ctx.session, org_id=ctx.org_id, blob_id=blob_id)
    tagmap = await svc.tags_by_blob(ctx.session, blob_ids=[blob.id])
    return _blob_out(blob, tagmap.get(blob.id))


@router.delete("/blobs/{blob_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_blob(
    blob_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    """Delete a single memory entry (member-level, RLS-scoped). Hard
    delete; cascades to the blob's tags/sources/vector. 404 if the blob
    is absent or belongs to another workspace."""
    await svc.delete_blob(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        blob_id=blob_id,
    )


@router.post("/erase", response_model=ErasedOut)
async def erase(
    body: MemoryEraseIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> ErasedOut:
    deleted = await svc.gdpr_erase(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        source_kind=body.source_kind,
        source_id=body.source_id,
    )
    return ErasedOut(deleted=deleted)


@router.post("/consolidate", response_model=MemoryBlobOut)
async def consolidate(
    body: MemoryConsolidateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> MemoryBlobOut:
    blob = await svc.consolidate(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_id=body.project_id,
        blob_ids=body.blob_ids,
        operation_id=body.operation_id,
    )
    tagmap = await svc.tags_by_blob(ctx.session, blob_ids=[blob.id])
    return _blob_out(blob, tagmap.get(blob.id))


@router.post("/blobs/{blob_id}/tags", status_code=status.HTTP_204_NO_CONTENT)
async def attach_blob_tag(
    blob_id: uuid.UUID,
    body: TagRefIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.attach_blob_tag(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        blob_id=blob_id,
        tag_id=body.tag_id,
    )


@router.delete("/blobs/{blob_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_blob_tag(
    blob_id: uuid.UUID,
    tag_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.detach_blob_tag(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        blob_id=blob_id,
        tag_id=tag_id,
    )


@router.post("/recompute-tier", response_model=TierCountsOut)
async def recompute_tier(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TierCountsOut:
    counts = await svc.recompute_tier(ctx.session, org_id=ctx.org_id)
    return TierCountsOut(hot=counts["hot"], warm=counts["warm"], cold=counts["cold"])
