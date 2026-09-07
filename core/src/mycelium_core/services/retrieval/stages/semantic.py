"""Semantic dense stage: pgvector HNSW (``vector_ip_ops`` index after
migration 0007) ordered by ``max_inner_product`` against the query
embedding. Early-exits when the query couldn't be embedded (no
optional dep, dim mismatch upstream) so the pipeline degrades to
lexical-only without raising.

Two embedding tiers (task 5276207e), fused by RRF: the LOCAL tier
(``embedding`` vector(1024), always present) and the optional HOSTED
tier (``embedding_hosted`` halfvec(4000), per-org Scaleway). The query
is embedded by whichever tiers the org has: ``ctx.query_embedding``
carries the local vector, ``ctx.extras['query_embedding_hosted']`` the
hosted one. When both exist the stage emits TWO branches and merges
them so RRF fuses local-only rows (no hosted tier / not yet backfilled)
with hosted rows.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text

from mycelium_core.embedder import EmbedResult
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services.retrieval.types import (
    Candidate,
    RetrievalContext,
    Stage,
    merge_candidates,
)

logger = logging.getLogger(__name__)


@dataclass
class SemanticDenseStage(Stage):
    name: str = "semantic"
    oversample: int = 50
    # Minimum cosine similarity a neighbour must clear to enter the
    # candidate set. Embeddings are L2-normalized so inner product =
    # cosine; pgvector ``<#>`` (``max_inner_product``) returns the
    # NEGATED inner product, hence ``cosine = -distance``. 0.0 disables
    # the gate (every kNN row is kept, the historical behaviour). A
    # positive floor drops far neighbours so a keyword/proper-noun query
    # with no genuine semantic match doesn't flood the fusion with noise
    # that ties (rank-only RRF) with the real lexical hits. Per-org,
    # tuned from the admin GUI (Organization.settings).
    min_similarity: float = 0.0

    def _keep(self, distance: float) -> bool:
        # distance = -cosine; keep when cosine >= floor. The floor only
        # bites when > 0 so the default is a true no-op (preserves the
        # historical "keep every kNN row, including slightly-negative
        # cosine" behaviour exactly).
        if self.min_similarity <= 0.0:
            return True
        return -float(distance) >= self.min_similarity

    def _kept(self, rows: Sequence[Any], tier: str) -> list[Any]:
        """Keep the kNN rows that clear the cosine floor, preserving order.

        Fails LOUD when the floor rejects EVERY fetched neighbour: a floor
        set above the model's achievable cosine band (e.g. 0.8 against
        bge-m3's compressed ~0.35-0.65 range) silently disables the whole
        dense branch -- no error, no log, just keyword-only retrieval. That
        exact mis-calibration shipped to prod once; surface it instead of
        no-opping invisibly."""
        kept = [r for r in rows if self._keep(r[1])]
        if rows and not kept and self.min_similarity > 0.0:
            best_cos = -min(float(r[1]) for r in rows)
            logger.warning(
                "%s: semantic floor %.3f rejected all %d kNN neighbour(s) "
                "(best cosine %.3f); dense branch contributing nothing -- "
                "retrieval_semantic_min_similarity is likely mis-calibrated "
                "for the active embedding model",
                tier,
                self.min_similarity,
                len(rows),
                best_cos,
            )
        return kept

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]:
        # No embedding at all -> degrade to lexical-only.
        # Recall diagnostics (task 4f3c2207): record into the per-call
        # ``extras`` scratch slot so ``retrieve_with_meta`` can tell the
        # caller why dense recall did (not) contribute -- the floor-rejected
        # mis-calibration was invisible in the byte-identical response.
        diag = ctx.extras.setdefault("semantic_diag", {"contributed": False, "floor_rejected": 0})
        local: EmbedResult | None = ctx.query_embedding
        hosted: EmbedResult | None = ctx.extras.get("query_embedding_hosted")
        if local is None and hosted is None:
            return candidates
        # pgvector iterative_scan: filter (org/project/tag) runs BEFORE
        # kNN so a selective tag still surfaces matches. SET LOCAL
        # keeps the GUC bound to the current transaction only. Set
        # once even if we run both branches.
        await ctx.session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        new: list[Candidate] = []
        # Hosted tier first so its rank position is lower (= more important
        # to RRF) on rows where both tiers exist.
        if hosted is not None:
            dist_hosted = MemoryBlob.embedding_hosted.max_inner_product(hosted.vector)
            stmt_hosted = (
                select(MemoryBlob.id, dist_hosted)
                .where(
                    MemoryBlob.org_id == ctx.org_id,
                    ctx.project_pred,
                    MemoryBlob.embedding_hosted.is_not(None),
                    # Only rows in the SAME vector space as the query. Vectors from
                    # different models are not comparable (ADR-0030), and without this the
                    # kNN silently ranks them against each other: a row embedded before a
                    # model swap scores as a neighbour of a query it has no relation to,
                    # and nothing in the response says so. The column carries one model at
                    # a time by design and the backfill converges it, but a corpus mid-swap
                    # holds both, and mid-swap is exactly when this must not lie.
                    MemoryBlob.model_id_hosted == hosted.model_id,
                    *ctx.tag_clauses,
                )
                .order_by(dist_hosted, MemoryBlob.id)
                .limit(self.oversample)
            )
            rows_hosted = (await ctx.session.execute(stmt_hosted)).all()
            kept_hosted = self._kept(rows_hosted, f"{self.name}_hosted")
            diag["floor_rejected"] += len(rows_hosted) - len(kept_hosted)
            new.extend(
                Candidate(
                    blob_id=row[0],
                    scores_by_stage={f"{self.name}_hosted": float(rank)},
                )
                for rank, row in enumerate(kept_hosted, start=1)
            )
        if local is not None:
            dist_local = MemoryBlob.embedding.max_inner_product(local.vector)
            stmt_local = (
                select(MemoryBlob.id, dist_local)
                .where(
                    MemoryBlob.org_id == ctx.org_id,
                    ctx.project_pred,
                    MemoryBlob.embedding.is_not(None),
                    # Same space as the query; see the hosted branch above.
                    MemoryBlob.model_id == local.model_id,
                    *ctx.tag_clauses,
                )
                .order_by(dist_local, MemoryBlob.id)
                .limit(self.oversample)
            )
            rows_local = (await ctx.session.execute(stmt_local)).all()
            kept_local = self._kept(rows_local, self.name)
            diag["floor_rejected"] += len(rows_local) - len(kept_local)
            new.extend(
                Candidate(
                    blob_id=row[0],
                    scores_by_stage={self.name: float(rank)},
                )
                for rank, row in enumerate(kept_local, start=1)
            )
        if new:
            diag["contributed"] = True
        return merge_candidates(candidates, new)
