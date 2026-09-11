"""The local embedder must emit the fleet ``embed_dim``, like the hosted one.

``config.embed_dim`` says "every embedder (local or hosted) MUST emit this
dim", and ``HostedEmbedder`` enforced it while ``LocalEmbedder`` returned
whatever the checkpoint produced. The consequence was not a wrong vector but
a closed door: ``memory.write_blob`` raises ``memory.dim_mismatch`` on any
length but the fleet dim, so the local tier could host exactly one model,
the 1024-native default. That is why the 2026-07-03 embedder round compared
only 1024d checkpoints.

The encode itself needs the optional model extra, so these tests drive
``LocalEmbedder`` over a stub standing in for the two SentenceTransformer
methods it calls.
"""

from __future__ import annotations

import math

import pytest

from mycelium_core.embedder import LocalEmbedder

FLEET_DIM = 1024


class StubSentenceTransformer:
    """The two methods ``LocalEmbedder`` uses, and nothing else.

    ``encode`` returns a ramp so a test can tell truncation (which keeps the
    leading dims) from padding or reordering, and honours
    ``normalize_embeddings`` as the real one does: the truncated PREFIX of a
    unit vector is still short of unit length, which is exactly why the
    coercion renormalizes.
    """

    def __init__(self, dim: int) -> None:
        self._dim = dim
        self.batch_sizes: list[int] = []

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    def _ramp(self, *, normalize: bool) -> list[float]:
        vec = [float(i + 1) for i in range(self._dim)]
        if not normalize:
            return vec
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec]

    def encode(
        self,
        texts: str | list[str],
        *,
        normalize_embeddings: bool = False,
        batch_size: int | None = None,
    ) -> list[float] | list[list[float]]:
        if isinstance(texts, str):
            return self._ramp(normalize=normalize_embeddings)
        if batch_size is not None:
            self.batch_sizes.append(batch_size)
        return [self._ramp(normalize=normalize_embeddings) for _ in texts]


def _embedder(dim: int) -> tuple[LocalEmbedder, StubSentenceTransformer]:
    emb = LocalEmbedder("stub/model")
    stub = StubSentenceTransformer(dim)
    emb._model = stub  # the load path needs the optional extra; skip it
    return emb, stub


@pytest.mark.asyncio
async def test_a_wider_checkpoint_is_truncated_to_the_fleet_dim() -> None:
    emb, _ = _embedder(2560)
    res = await emb.embed("una query qualunque")
    assert len(res.vector) == FLEET_DIM


@pytest.mark.asyncio
async def test_truncation_keeps_the_leading_dims_and_renormalizes() -> None:
    """Matryoshka's contract: the leading dims carry the meaning, and the
    prefix is re-normalized to a unit vector (the inner-product opclass
    assumes one)."""
    emb, _ = _embedder(2560)
    res = await emb.embed("q")
    norm = math.sqrt(sum(x * x for x in res.vector))
    assert norm == pytest.approx(1.0)
    # Ramp 1..2560 truncated to 1..1024, then scaled by one factor: the
    # ratio between consecutive components survives.
    assert res.vector[1] / res.vector[0] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_the_native_dim_model_is_unchanged_in_direction() -> None:
    """The default bge-m3 path: 1024 native into a 1024 fleet dim is a
    no-op in direction, so this change cannot move production retrieval."""
    emb, _ = _embedder(FLEET_DIM)
    res = await emb.embed("q")
    assert len(res.vector) == FLEET_DIM
    assert res.vector[1] / res.vector[0] == pytest.approx(2.0)
    assert math.sqrt(sum(x * x for x in res.vector)) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_a_narrower_checkpoint_is_not_padded() -> None:
    """A vector shorter than the fleet dim cannot be extended faithfully, so
    it stays short and ``memory.write_blob`` rejects it. Silently padding
    would produce a vector that indexes and ranks and means nothing."""
    emb, _ = _embedder(768)
    res = await emb.embed("q")
    assert len(res.vector) == 768


@pytest.mark.asyncio
async def test_batch_encoding_is_coerced_too() -> None:
    emb, _ = _embedder(4096)
    out = await emb.embed_batch(["uno", "due", "tre"])
    assert [len(r.vector) for r in out] == [FLEET_DIM] * 3


@pytest.mark.asyncio
async def test_native_dim_is_reported_so_truncation_is_visible() -> None:
    """Without this the coercion is invisible: a candidate running 4096 ->
    1024 and one running natively at 1024 return the same shape."""
    emb, _ = _embedder(4096)
    assert emb.native_dim == 4096
    assert LocalEmbedder("stub/unloaded").native_dim is None
