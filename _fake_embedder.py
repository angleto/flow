"""Deterministic in-memory embedder for tests (ADR-0012 seam).

A stable hashed bag-of-words projected into the production dimension
and L2-normalized, so cosine similarity tracks token overlap without a
real model. Process-stable (hashlib, not the salted builtin hash).

Tokenization uses ``\\w+`` so punctuation does not stick to words
(``"tasks,"`` becomes the token ``tasks``). A whitespace split made
docstring tweaks shift ranks under bag-of-words, which made the tests
fragile to perfectly valid doc edits.
"""

from __future__ import annotations

import hashlib
import math
import re

from mycelium_core.embed_dims import EMBED_DIM
from mycelium_core.embedder import EmbedResult, EmbedSide

_TOKEN = re.compile(r"\w+")


def _hash_idx(token: str, dim: int) -> int:
    h = hashlib.md5(token.encode()).hexdigest()  # noqa: S324 (non-crypto use)
    return int(h, 16) % dim


class FakeEmbedder:
    """Deterministic hashed bag-of-tokens, SYMMETRIC in the side.

    Ignoring ``side`` is not a shortcut in the double, it is the contract of
    the model it stands in for: bge-m3, the incumbent, embeds a query and a
    document the same way. A fake that returned different vectors per side
    would break every retrieval test for a reason no production embedder
    has. What a wrong side costs on an instruction-tuned model is measured
    in the embedder round, not asserted here.
    """

    model_id = "fake-embed"

    async def embed(self, text: str, *, side: EmbedSide) -> EmbedResult:
        del side  # symmetric, see the class docstring
        dim = EMBED_DIM
        vec = [0.0] * dim
        tokens = _TOKEN.findall(text.lower())
        for tok in tokens:
            vec[_hash_idx(tok, dim)] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        else:
            vec[0] = 1.0
        return EmbedResult(vector=vec, model_id=self.model_id, tokens=max(1, len(tokens)))
