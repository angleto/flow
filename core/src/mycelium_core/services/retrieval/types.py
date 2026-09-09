"""Core types of the retrieval pipeline (docs/adr/0005 + task `300084ac`).

The pipeline is a list of ``Stage`` invoked in order on a list of
``Candidate``. Each stage may add candidates (lexical/semantic
branches), reorder them (fusion/rerank), drop them (limit/dedupe), or
side-effect (access counter). The first stage receives an empty
candidate list; the final list returned by the executor is what the
caller sees.

The stages do NOT cross-communicate through hidden state: anything a
stage needs from a previous one travels through ``Candidate`` fields
(``scores_by_stage`` for per-branch ranks the fusion stage needs) or
through the shared ``RetrievalContext`` (org/project predicates, the
embedded query, embedder reference, channel filter).

Concurrency: today every stage is sequential. A future ``ParallelStages``
wrapper can run independent branches concurrently (e.g. lexical + dense
+ HyDE) without changing the Stage protocol; the executor stays
linear over the top-level list.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.embedder import Embedder, EmbedResult


class _AnyProject:
    """Sentinel: retrieve across every project perimeter rather than one.

    ``project_id=None`` already means something, and it is not this: it is
    the NULL perimeter, where task blobs deliberately live so that they stay
    org-wide. Three services mirror that reading in their own docstrings, so
    None keeps it.

    What was missing is the third case. A caller asking "what did we decide
    about X" does not know which project it was decided in -- that is the
    question -- and until this sentinel the API had no way to say so.

    The cost was measured on 2026-09-07 and the split is the whole story. A
    gold set of twenty such questions retrieved 3 at k=5. Tasks scored 3 of
    6, because task blobs carry ``project_id=NULL`` and the NULL perimeter is
    exactly what an unscoped search searched. Notes scored **0 of 14**,
    because a note blob carries its note's project and no unscoped search
    could reach one. Not ranking and not embeddings: a perimeter the caller
    had no way to name.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ANY_PROJECT"


#: Pass as ``project_id`` to search every perimeter in the org.
ANY_PROJECT = _AnyProject()

#: What a retrieval perimeter can be: one project, the NULL perimeter, or all.
ProjectScope = uuid.UUID | None | _AnyProject


@dataclass
class RetrievalContext:
    """Per-call context shared across stages. Built once by the wrapper
    in ``memory.retrieve``, immutable thereafter (stages must not
    mutate it -- pass mutable scratch through ``extras`` if needed)."""

    session: AsyncSession
    org_id: uuid.UUID
    actor_id: uuid.UUID
    project_id: ProjectScope
    operation_id: str
    embedder: Embedder
    # Pre-computed SQL predicates so stages don't recompute them.
    # ``project_pred`` is the ``MemoryBlob.project_id IS NULL | == X``
    # clause, or no clause at all under ``ANY_PROJECT``; ``tag_clauses``
    # is the (possibly empty) tuple of tag/
    # channel constraints to AND into both lexical and semantic branches.
    project_pred: ColumnElement[bool]
    tag_clauses: tuple[ColumnElement[bool], ...]
    # The query embedding (already metered upstream). None when the
    # embedder is unavailable or empty -- semantic stage degrades to a
    # no-op in that case (RRF over the lexical branch alone is still
    # well-defined).
    query_embedding: EmbedResult | None
    # Free extension slot for future stages (HyDE rewritten query,
    # rerank logits, per-stage diagnostics). Use namespaced keys.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    """One retrievable row mid-pipeline. ``score`` is the *current*
    aggregate score (what the next stage will sort/filter on);
    ``scores_by_stage`` is the per-stage record used by fusion +
    diagnostics (RRF needs per-branch ranks; reranker may want to
    surface its logit alongside the bi-encoder score in debug)."""

    blob_id: uuid.UUID
    score: float = 0.0
    scores_by_stage: dict[str, float] = field(default_factory=dict)
    # Provenance for dedupe (chunking will populate). 0 = whole document.
    source_kind: str | None = None
    source_id: str | None = None
    chunk_index: int = 0
    # Set by stages that need to load the row (snippet, rerank).
    # Avoids a separate SELECT in the caller for the typical small top-K.
    text: str | None = None
    created_at: datetime.datetime | None = None
    # User-facing provenance marker (ADR-0034). "humus" = surfaced via
    # the parallel humus source (archived material decomposed into
    # atoms); None = ordinary live retrieval. Carried through fusion /
    # ordering / dedupe to the Hit so the SPA can render the leaf icon.
    provenance: str | None = None


@runtime_checkable
class Stage(Protocol):
    """The single extension point. Every stage must be safe to skip
    when its precondition is unmet (e.g. SemanticDenseStage early-exits
    if ``ctx.query_embedding is None``)."""

    name: str

    async def run(
        self,
        query: str,
        ctx: RetrievalContext,
        candidates: list[Candidate],
    ) -> list[Candidate]: ...


def merge_candidates(
    existing: Sequence[Candidate],
    incoming: Sequence[Candidate],
) -> list[Candidate]:
    """Merge two candidate lists by ``blob_id`` (a stage that adds
    new candidates while preserving previous-stage scores). Per-stage
    scores accumulate; ``score`` (the aggregate) is left to the
    fusion stage to compute. Order of the result is preserved from
    ``existing``, with newcomers appended in their incoming order."""
    by_id: dict[uuid.UUID, Candidate] = {c.blob_id: c for c in existing}
    out: list[Candidate] = list(existing)
    for c in incoming:
        if c.blob_id in by_id:
            merged = by_id[c.blob_id]
            merged.scores_by_stage.update(c.scores_by_stage)
            # A blob surfaced by both a live branch and the humus source
            # is still humus (the note carries the flag): keep the marker
            # so the leaf icon and the humus cap both see it.
            if c.provenance and not merged.provenance:
                merged.provenance = c.provenance
        else:
            by_id[c.blob_id] = c
            out.append(c)
    return out
