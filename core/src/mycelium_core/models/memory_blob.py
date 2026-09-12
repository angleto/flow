"""Hierarchical memory (docs/adr/0005, 0007, 0016, FR-8).

``memory_blobs`` is PARTITION BY HASH (org_id) with mandatory RLS; the
(org_id, project_id) predicate is the hard isolation boundary, never
relevance. The cold tier (embedding) stays always queryable; the tier
is a latency hint driven by an access score, never retention. FTS is a
generated column queried via raw SQL in retrieval (not mapped here).
Provenance is N:1 via ``blob_sources`` for GDPR erasure.
"""

from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal

from pgvector.sqlalchemy import HALFVEC, Vector
from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.embed_dims import EMBED_DIM, EMBED_DIM_HOSTED
from mycelium_core.models.base import Base, OrgScopedMixin, TimestampMixin

# Two embedding tiers, each a permanent store fused at search time (RRF):
#  - LOCAL  (``embedding``): bge-m3, always-on rank-0 fallback, works
#    offline/OSS.
#  - HOSTED (``embedding_hosted``): per-org Scaleway, selected via
#    ``org_embedder_provider``.
# Both widths come from ``mycelium_core.embed_dims``, which is where the
# reason for each number lives; both are fixed at the DDL level, so a
# change is a drop+rebuild of the column (embeddings are re-derivable
# from ``text`` via the backfill).


class Tier(enum.StrEnum):
    hot = "hot"
    warm = "warm"
    cold = "cold"


class MemoryBlob(OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "memory_blobs"
    # Composite PK (id, org_id): required because the table is
    # PARTITION BY HASH (org_id) (the PK must include the partition key).
    __table_args__ = (PrimaryKeyConstraint("id", "org_id", name="pk_memory_blobs"),)

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), default=uuid.uuid4)

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    namespace: Mapped[str] = mapped_column(String(40), nullable=False, server_default="email")
    tier: Mapped[str] = mapped_column(String(8), nullable=False, server_default="hot")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-row text-search config driving the stemmed ``fts_lang`` generated
    # column (task b1baaf52): a Postgres regconfig name ('italian',
    # 'english', ...) or 'simple' (no stemming). Set at write from the
    # source language (detected, or passed explicitly); see
    # services.fts_language. The generated column reads it via the
    # immutable ``fts_to_tsvector`` helper (migration 0066).
    fts_language: Mapped[str] = mapped_column(String(32), nullable=False, server_default="simple")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    dim: Mapped[int] = mapped_column(Integer, nullable=False, server_default=str(EMBED_DIM))
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_accessed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    importance: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, server_default="0")
    access_score: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0"
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    # Hosted tier (task 5276207e): populated when the org has a hosted
    # embedder (Scaleway) configured. halfvec(4000), HNSW halfvec_ip_ops.
    # ``model_id_hosted`` records the producing model so a per-org model
    # swap can be detected and re-embedded; NULL when no hosted tier.
    embedding_hosted: Mapped[list[float] | None] = mapped_column(
        HALFVEC(EMBED_DIM_HOSTED), nullable=True
    )
    model_id_hosted: Mapped[str | None] = mapped_column(String(160), nullable=True)
    dim_hosted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # First-class provenance (ADR-0049 / ADR-0028; migration 0085). Before
    # this, a blob's author was only a tag-lane convention (``agent/<handle>``),
    # which cannot satisfy the unconditional-provenance axiom once MORE THAN ONE
    # agent writes to the shared store. ``created_by`` is the authoring identity
    # (a user OR an ai_assistant, ADR-0028), nullable + ``SET NULL`` so a
    # deleted actor never cascades away its memories and legacy rows backfill
    # to NULL. ``origin_model_id`` is the LLM that produced the text (NULL for
    # a human author) -- the memory twin of ``note.origin_model_id`` /
    # ``kg.origin_model_id``. NOT the ``model_id`` above, which is the EMBEDDING
    # model. These drive provenance-filtered recall (retrieve ``created_by=``).
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    origin_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class BlobSource(Base):
    """Explicit N:1 provenance (docs/adr/0005): many sources can map to
    one blob; erasing the blob cascades here (FK ON DELETE CASCADE).
    ``chunk_index`` (migration 0008) extends the natural key so one
    source can own multiple chunks (paragraph-split for long notes).
    chunk_index=0 = whole document (legacy single-vector semantics)."""

    __tablename__ = "blob_sources"
    __table_args__ = (
        PrimaryKeyConstraint(
            "blob_id", "source_kind", "source_id", "chunk_index", name="pk_blob_sources"
        ),
        ForeignKeyConstraint(
            ["blob_id", "org_id"],
            ["memory_blobs.id", "memory_blobs.org_id"],
            ondelete="CASCADE",
            name="fk_blob_sources_blob_id_memory_blobs",
        ),
    )

    blob_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MemoryBlobTag(Base):
    """Structured facet on a memory blob (docs/adr/0003, 0005). Tags
    narrow retrieval inside the (org, project) boundary, never across
    it. Composite FK to the hash-partitioned blob (like ``blob_sources``)
    so erasing a blob cascades here."""

    __tablename__ = "memory_blob_tags"
    __table_args__ = (
        PrimaryKeyConstraint("blob_id", "tag_id", name="pk_memory_blob_tags"),
        ForeignKeyConstraint(
            ["blob_id", "org_id"],
            ["memory_blobs.id", "memory_blobs.org_id"],
            ondelete="CASCADE",
            name="fk_memory_blob_tags_blob_id_memory_blobs",
        ),
    )

    blob_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
