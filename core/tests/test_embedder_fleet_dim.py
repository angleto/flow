"""The local embedder must emit the fleet dim, like the hosted one.

``embed_dims.EMBED_DIM`` says "every embedder (local or hosted) MUST emit this
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
import sys

import pytest

from mycelium_core.config import get_settings
from mycelium_core.embed_dims import EMBED_DIM
from mycelium_core.embedder import EmbedSide, LocalEmbedder

# The relationship under test is "the embedder emits whatever the column is",
# so the expectation is READ from the fleet constant rather than written out
# again here: a test that pinned 1024 independently would keep passing while
# the embedder and the column disagreed. The number itself is pinned against
# the live DDL by ``test_embed_dim_drift``.
FLEET_DIM = EMBED_DIM


class StubSentenceTransformer:
    """What ``LocalEmbedder`` uses of SentenceTransformer, and nothing else.

    ``encode`` returns a ramp so a test can tell truncation (which keeps the
    leading dims) from padding or reordering, and honours
    ``normalize_embeddings`` as the real one does: the truncated PREFIX of a
    unit vector is still short of unit length, which is exactly why the
    coercion renormalizes.

    ``prompts`` is the checkpoint's own instruction declaration, the thing
    sentence-transformers loads out of ``config_sentence_transformers.json``.
    It is empty by default because the incumbent declares none, and
    ``encode`` REFUSES a ``prompt_name`` that is not in it -- exactly as the
    real class does, which is the failure mode that matters here: asking for
    a prompt a model does not have is a ValueError, not a silent no-op.
    """

    def __init__(self, dim: int, *, prompts: dict[str, str] | None = None) -> None:
        self._dim = dim
        self.batch_sizes: list[int] = []
        self.prompts = dict(prompts or {})
        self.prompt_names: list[str | None] = []

    def get_embedding_dimension(self) -> int:
        """The name sentence-transformers 5 uses. The 3.x name is a separate
        test below, because this package's floor still admits 3.x and the
        deprecated alias is what a 3.x checkpoint would answer to."""
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
        prompt_name: str | None = None,
    ) -> list[float] | list[list[float]]:
        self.prompt_names.append(prompt_name)
        if prompt_name is not None and prompt_name not in self.prompts:
            raise ValueError(f"Prompt name {prompt_name!r} not found in the configured prompts")
        if isinstance(texts, str):
            return self._ramp(normalize=normalize_embeddings)
        if batch_size is not None:
            self.batch_sizes.append(batch_size)
        return [self._ramp(normalize=normalize_embeddings) for _ in texts]


def _embedder(
    dim: int, *, prompts: dict[str, str] | None = None
) -> tuple[LocalEmbedder, StubSentenceTransformer]:
    emb = LocalEmbedder("stub/model")
    stub = StubSentenceTransformer(dim, prompts=prompts)
    emb._model = stub  # the load path needs the optional extra; skip it
    return emb, stub


@pytest.mark.asyncio
async def test_a_wider_checkpoint_is_truncated_to_the_fleet_dim() -> None:
    emb, _ = _embedder(2560)
    res = await emb.embed("una query qualunque", side=EmbedSide.document)
    assert len(res.vector) == FLEET_DIM


@pytest.mark.asyncio
async def test_truncation_keeps_the_leading_dims_and_renormalizes() -> None:
    """Matryoshka's contract: the leading dims carry the meaning, and the
    prefix is re-normalized to a unit vector (the inner-product opclass
    assumes one)."""
    emb, _ = _embedder(2560)
    res = await emb.embed("q", side=EmbedSide.document)
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
    res = await emb.embed("q", side=EmbedSide.document)
    assert len(res.vector) == FLEET_DIM
    assert res.vector[1] / res.vector[0] == pytest.approx(2.0)
    assert math.sqrt(sum(x * x for x in res.vector)) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_a_narrower_checkpoint_is_not_padded() -> None:
    """A vector shorter than the fleet dim cannot be extended faithfully, so
    it stays short and ``memory.write_blob`` rejects it. Silently padding
    would produce a vector that indexes and ranks and means nothing."""
    emb, _ = _embedder(768)
    res = await emb.embed("q", side=EmbedSide.document)
    assert len(res.vector) == 768


@pytest.mark.asyncio
async def test_batch_encoding_is_coerced_too() -> None:
    emb, _ = _embedder(4096)
    out = await emb.embed_batch(["uno", "due", "tre"], side=EmbedSide.document)
    assert [len(r.vector) for r in out] == [FLEET_DIM] * 3


@pytest.mark.asyncio
async def test_native_dim_is_reported_so_truncation_is_visible() -> None:
    """Without this the coercion is invisible: a candidate running 4096 ->
    1024 and one running natively at 1024 return the same shape."""
    emb, _ = _embedder(4096)
    assert emb.native_dim == 4096
    assert LocalEmbedder("stub/unloaded").native_dim is None


# --- The truncation must be DECLARED, not assumed (the MRL gate) -----------
#
# Coercing a wider checkpoint to the fleet dim is the Matryoshka procedure,
# and it is principled only for a model trained that way. Applied to any
# other model it returns a vector of exactly the right shape whose content
# is worse, so nothing downstream can notice: the fleet-dim fix above turned
# one loud failure (``memory.dim_mismatch`` at the write) into a silent
# degradation for that class of model. These assert the gate that keeps the
# failure loud, and that it runs on the REAL load path rather than only as a
# method somebody could forget to call.


class _FakeSentenceTransformers:
    """Stand-in for the optional extra, so ``_load_sync`` can run in CI.

    ``SentenceTransformer(name, revision=...)`` is the only thing
    ``LocalEmbedder`` imports from it, and it hands back the same stub the
    tests above drive. The ``revision`` keyword is part of the real
    signature, so the double takes it too: a stand-in that accepted fewer
    arguments would make the call site untestable in exactly the way that
    matters (it would pass here and fail on the real class).
    """

    def __init__(self, stub: StubSentenceTransformer) -> None:
        self._stub = stub
        self.revisions: list[str | None] = []

    def SentenceTransformer(
        self, name: str, *, revision: str | None = None
    ) -> StubSentenceTransformer:
        self.name = name
        self.revisions.append(revision)
        return self._stub


def _install_fake_extra(monkeypatch: pytest.MonkeyPatch, stub: StubSentenceTransformer) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", _FakeSentenceTransformers(stub))


def test_a_wider_checkpoint_loads_when_the_spec_declares_mrl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StubSentenceTransformer(FLEET_DIM * 2)
    _install_fake_extra(monkeypatch, stub)
    emb = LocalEmbedder("stub/wide-mrl", mrl=True)

    assert emb._load_sync() is stub
    assert emb.native_dim == FLEET_DIM * 2


def test_a_wider_checkpoint_is_refused_when_mrl_is_not_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StubSentenceTransformer(FLEET_DIM * 2)
    _install_fake_extra(monkeypatch, stub)
    emb = LocalEmbedder("stub/wide-plain", mrl=False)

    with pytest.raises(RuntimeError) as exc:
        emb._load_sync()

    # The message must carry both widths and the name of the knob: this
    # fires on a machine that is not the one that chose the model.
    msg = str(exc.value)
    assert "stub/wide-plain" in msg
    assert str(FLEET_DIM * 2) in msg and str(FLEET_DIM) in msg
    assert "MYCELIUM_EMBED_MODEL_IS_MRL" in msg


def test_a_narrower_checkpoint_is_refused_whatever_it_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MRL says a vector may be CUT, never that it may be stretched. The
    refusal here does not depend on the claim, and it replaces a failure
    that used to happen one layer down, at the write."""
    stub = StubSentenceTransformer(FLEET_DIM // 2)
    _install_fake_extra(monkeypatch, stub)
    emb = LocalEmbedder("stub/narrow", mrl=True)

    with pytest.raises(RuntimeError, match="narrower"):
        emb._load_sync()


def test_the_default_model_loads_with_nothing_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate must not ask anything of the incumbent. bge-m3 is natively
    at the fleet dim, so it is never truncated and there is no claim to
    make; a gate that required the flag anyway would be a migration nobody
    asked for."""
    stub = StubSentenceTransformer(FLEET_DIM)
    _install_fake_extra(monkeypatch, stub)

    assert LocalEmbedder("BAAI/bge-m3")._load_sync() is stub


def test_the_deployment_answers_for_its_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mrl=None`` (the production path, ``get_embedder``) reads the
    deployment's declaration for the model IT configured."""
    stub = StubSentenceTransformer(FLEET_DIM * 2)
    _install_fake_extra(monkeypatch, stub)
    monkeypatch.setenv("MYCELIUM_EMBED_MODEL_IS_MRL", "true")
    get_settings.cache_clear()
    try:
        assert LocalEmbedder("stub/wide-configured")._load_sync() is stub

        monkeypatch.setenv("MYCELIUM_EMBED_MODEL_IS_MRL", "false")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError):
            LocalEmbedder("stub/wide-configured")._load_sync()
    finally:
        # monkeypatch restores the env at teardown; the cached singleton
        # must be dropped too or the flipped value leaks to later tests.
        get_settings.cache_clear()


def test_a_stand_in_that_does_not_report_its_width_still_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate reads a width the model volunteers. Something that does not
    implement the accessor is not thereby suspect -- what it emits is still
    checked at the write -- and refusing it would make the gate a second,
    stricter definition of what an embedder is."""

    class Mute:
        def encode(self, texts: object, **_: object) -> list[float]:
            return [1.0] * FLEET_DIM

    monkeypatch.setitem(sys.modules, "sentence_transformers", _FakeSentenceTransformers(Mute()))  # type: ignore[arg-type]
    assert isinstance(LocalEmbedder("stub/mute")._load_sync(), Mute)


def test_the_pinned_revision_reaches_the_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model id is a moving reference: the pin is the only thing that makes
    a round repeatable, and it is worth nothing if it stops at the
    constructor. ``None`` (the deployment's case) must stay None rather than
    become a string like "main", which would be a different request."""
    stub = StubSentenceTransformer(FLEET_DIM)
    fake = _FakeSentenceTransformers(stub)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)

    LocalEmbedder("stub/model", revision="deadbeef")._load_sync()
    LocalEmbedder("stub/model")._load_sync()

    assert fake.revisions == ["deadbeef", None]


# --- the side reaches the model, and comes from the checkpoint -------------
#
# An instruction-tuned retrieval model is trained with a prefix on the query
# and nothing on the document. Getting that wrong does not raise: it returns
# a vector of the right shape from a model outside its training
# configuration, and costs recall silently. These assert the two halves of
# the fix -- that the side reaches ``encode``, and that what it applies is
# the checkpoint's OWN declaration rather than a string written beside the
# model id, which is the copy that would drift.

#: What Qwen3-Embedding ships in its config_sentence_transformers.json.
_INSTRUCT = "Instruct: Given a web search query, retrieve relevant passages\nQuery:"


@pytest.mark.asyncio
async def test_the_query_side_uses_the_prompt_the_checkpoint_declares() -> None:
    emb, stub = _embedder(FLEET_DIM, prompts={"query": _INSTRUCT})
    await emb.embed("dove sta il reranker", side=EmbedSide.query)
    assert stub.prompt_names == ["query"]


@pytest.mark.asyncio
async def test_the_document_side_of_the_same_model_uses_none() -> None:
    """The asymmetry IS the point: the same checkpoint, the other side, no
    prompt. A model that prefixed both sides would be no better than the
    symmetric run this replaces."""
    emb, stub = _embedder(FLEET_DIM, prompts={"query": _INSTRUCT})
    await emb.embed("un documento qualunque", side=EmbedSide.document)
    assert stub.prompt_names == [None]


@pytest.mark.asyncio
async def test_a_symmetric_checkpoint_is_asked_for_no_prompt_on_either_side() -> None:
    """bge-m3 declares nothing. Naming a prompt it does not have is a
    ValueError from the real class, so this is what keeps the incumbent
    working, not a nicety."""
    emb, stub = _embedder(FLEET_DIM)
    await emb.embed("x", side=EmbedSide.query)
    await emb.embed("x", side=EmbedSide.document)
    assert stub.prompt_names == [None, None]


@pytest.mark.asyncio
async def test_the_batched_path_carries_the_side_too() -> None:
    """The ingest path is the batched one. If only the single encode honoured
    the side, every document in a corpus would be embedded as a query and the
    round would still produce a full table."""
    emb, stub = _embedder(FLEET_DIM, prompts={"query": _INSTRUCT})
    await emb.embed_batch(["a", "b"], side=EmbedSide.query)
    assert stub.prompt_names == ["query"]


def test_the_declared_prompt_is_readable_for_the_record() -> None:
    """The round prints this: "the instruction-tuned model ran
    instruction-tuned" is a claim about what happened, and July's round shows
    what it costs to assume it."""
    emb, _ = _embedder(FLEET_DIM, prompts={"query": _INSTRUCT})
    assert emb.declared_prompt(EmbedSide.query) == _INSTRUCT
    assert emb.declared_prompt(EmbedSide.document) is None
    assert LocalEmbedder("stub/unloaded").declared_prompt(EmbedSide.query) is None


@pytest.mark.asyncio
async def test_an_empty_declaration_counts_as_no_prompt() -> None:
    """Qwen3 declares ``document: ""``. An empty string is a declaration
    that there is nothing to prepend, and passing its name to ``encode``
    would prepend nothing while claiming, in the round's table, that the
    model ran with a document-side instruction."""
    emb, stub = _embedder(FLEET_DIM, prompts={"query": _INSTRUCT, "document": ""})
    await emb.embed("x", side=EmbedSide.document)
    assert stub.prompt_names == [None]
    assert emb.declared_prompt(EmbedSide.document) is None


def test_the_three_x_accessor_name_still_answers() -> None:
    """``sentence-transformers`` 5 renamed ``get_sentence_embedding_dimension``
    to ``get_embedding_dimension`` and kept the old name as a deprecated
    alias that warns on every call. This package's floor is >=3, so a
    deployment can legitimately be running a version that has only the old
    name; asking for the new one alone would silently stop reporting the
    native dim there, and the truncation would go back to being invisible."""

    class ThreeX:
        def get_sentence_embedding_dimension(self) -> int:
            return 4096

    emb = LocalEmbedder("stub/3x")
    emb._model = ThreeX()
    assert emb.native_dim == 4096


def test_a_model_that_reports_no_width_is_not_refused() -> None:
    """Neither accessor: nothing to check, and the real check on what it
    emits happens at the write. Refusing here would make the gate a second,
    stricter definition of what an embedder is."""
    emb = LocalEmbedder("stub/mute")
    emb._model = object()
    assert emb.native_dim is None
