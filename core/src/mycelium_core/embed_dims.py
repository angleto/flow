"""The two fleet embedding dimensions, defined once.

These are SCHEMA facts, not configuration. Both are fixed at the DDL
level -- ``memory_blobs.embedding`` is ``vector(EMBED_DIM)`` and
``memory_blobs.embedding_hosted`` is ``halfvec(EMBED_DIM_HOSTED)`` --
so changing either one is a drop-and-rebuild of the column plus a
re-embedding of the corpus (ADR-0030), never a value an operator sets
at boot. They lived as ``Settings`` fields until 2026-09-12, which
made that impossible situation expressible: ``MYCELIUM_EMBED_DIM=768``
coerced every new vector to 768 floats while the column stayed at
1024, so writes failed at the driver and the cause was three modules
away. A constant cannot be set to a value the column will refuse.

They live in a leaf module of their own, rather than in ``config`` or
in ``models.memory_blob``, because both of those need them and neither
may import the other: ``config`` is read before any model is imported,
and the models do not read configuration. The value was previously
written out four times (both of those modules, ``models.adjudication``,
and the migration DDL), tied together only by comments asking the
reader to keep them equal.

The migration DDL necessarily still spells the number out -- a
migration is a historical record and must not change meaning when a
constant does. ``core/tests/test_embed_dim_drift.py`` is what ties the
two together: it reads the dimension the live column actually has and
compares it with these constants.
"""

from __future__ import annotations

# LOCAL tier (``memory_blobs.embedding``, pgvector ``vector``): the
# always-on rank-0 fallback, works offline/OSS. 1024 is bge-m3's native
# width AND sits under pgvector's 2000-dim HNSW ceiling for ``vector``,
# so no halfvec is needed. A local checkpoint wider than this is
# truncated (Matryoshka) only when it declares MRL support; a narrower
# one cannot be padded faithfully and is refused at the write.
EMBED_DIM = 1024

# HOSTED tier (``memory_blobs.embedding_hosted``, pgvector ``halfvec``):
# a per-org Scaleway model selected via ``org_embedder_provider``, fused
# with the local tier at search time (RRF). 4000 is pgvector's HNSW
# ceiling for ``halfvec``, chosen so any model up to 4000 native dims
# fits by truncation with no reindex.
EMBED_DIM_HOSTED = 4000
