"""Embedder abstraction (docs/adr/0012, 0005, FR-8).

Protocol + neutral DTO + injectable factory, the same seam as the LLM
provider and the email connector: the production embedder runs a local
multilingual model; CI injects a deterministic in-memory embedder.
``model_id`` and the produced ``dim`` are recorded per blob so a future
re-embedding to a different model is a new column, not an in-place
change (ADR-0005).

Process-singleton: ``get_embedder()`` returns the SAME ``LocalEmbedder``
instance for the life of the process (when no override is set). The
underlying SentenceTransformer is multi-hundred-MB in resident memory;
returning a fresh instance per call (the pre-fix shape) made every
caller pay the in-memory load again and inflated the working set toward
the pod memory limit. The override seam used by tests bypasses this
cache, so CI behavior is unchanged.

Async-safe load: both ``embed`` and ``embed_batch`` move the
SentenceTransformer construction *and* the encode into a worker thread,
so a cold first call never blocks the asyncio event loop (which would
otherwise stall every concurrent request, including liveness probes
that share the loop). ``prewarm()`` is exposed so a server can warm the
model at startup off the request path.
"""

from __future__ import annotations

import asyncio
import enum
import importlib.util
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

import httpx

from mycelium_core.config import get_settings
from mycelium_core.embed_dims import EMBED_DIM


@dataclass(frozen=True)
class EmbedResult:
    vector: list[float]
    model_id: str
    tokens: int  # billable units for metering (ADR-0019)


class EmbedSide(enum.StrEnum):
    """Which half of a retrieval pair a text is being embedded as.

    A whole class of retrieval models -- every instruction-tuned one, which
    is most of what has been published since 2025 -- is trained with an
    instruction prefix on the QUERY and nothing on the document. Embedding
    both sides identically does not fail: it returns vectors of the right
    shape from a model running outside the configuration it was trained
    for, and costs a few points of recall silently. So the side is not an
    optimisation the embedder may ignore, it is part of asking the
    question, and it has no default: a call site that has not decided which
    side it is on has a bug that only a benchmark would ever reveal.

    A symmetric model (bge-m3, the incumbent) simply ignores it.
    """

    query = "query"
    document = "document"


@runtime_checkable
class Embedder(Protocol):
    async def embed(self, text: str, *, side: EmbedSide) -> EmbedResult: ...


def estimate_tokens(text: str, *, window: int) -> int:
    """Cheap token estimate for batching decisions only (never for metering).

    chars/4 is the usual rough ratio; it is clamped to ``window`` because a
    text longer than the model's sequence window is truncated by the encoder,
    so its cost stops growing there. Over-estimating is harmless (a smaller
    group), under-estimating is what we must avoid, hence no lower clamp.
    """
    approx = max(1, len(text) // 4)
    return min(approx, window) if window > 0 else approx


def group_by_token_budget(texts: list[str], *, budget: int, window: int) -> list[list[str]]:
    """Split ``texts`` into groups whose (longest x count) stays under ``budget``.

    The encoder pads every sequence in a group up to the longest one, so the
    group's cost is that product, not the sum of the individual lengths. A
    single text always forms a group of its own if it exceeds the budget by
    itself: dropping it would silently lose an embedding, and truncation is
    already handled by ``window``.
    """
    budget = max(1, budget)
    out: list[list[str]] = []
    cur: list[str] = []
    cur_max = 0
    for t in texts:
        est = estimate_tokens(t, window=window)
        nxt_max = max(cur_max, est)
        if cur and nxt_max * (len(cur) + 1) > budget:
            out.append(cur)
            cur, cur_max = [t], est
        else:
            cur.append(t)
            cur_max = nxt_max
    if cur:
        out.append(cur)
    return out


class LocalEmbedder:
    """Reference local model (sentence-transformers, CPU/ARM). Lazily
    imported so the heavy dependency is optional and never loaded in
    CI (tests inject a fake). The model is loaded once per instance and
    the instance itself is cached at module scope by ``get_embedder``;
    both the load and the encode are dispatched to a worker thread.

    Emits exactly ``embed_dims.EMBED_DIM`` via ``_truncate_normalize``,
    the same coercion :class:`HostedEmbedder` applies. That invariant
    ("every embedder, local or hosted, MUST emit this dim") used to be
    enforced on the hosted side only, so the local tier could host exactly
    one model: the default bge-m3, whose native width happens to equal the
    fleet dim. Any other checkpoint failed at its first write with
    ``memory.dim_mismatch``, which is why the 2026-07-03 embedder round
    could compare only models of that one width.

    Truncation is the documented Matryoshka procedure and it is only
    principled for an MRL-trained checkpoint: an ordinary model's
    dimensions carry no nesting, so cutting it to the fleet width loses
    real information and the loss shows up as slightly worse retrieval
    rather than as an error. That is why ``mrl`` is not inferred. A wider
    checkpoint that has not DECLARED Matryoshka support is refused at
    load, which is the noisy failure the coercion would otherwise have
    replaced with a silent degradation. A narrower one is refused for a
    stronger reason: a short vector cannot be padded faithfully at all."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        *,
        mrl: bool | None = None,
        revision: str | None = None,
    ) -> None:
        """``mrl`` declares that this checkpoint is Matryoshka-trained and
        may therefore be truncated to the fleet dim. ``None`` reads the
        deployment's answer for its configured model
        (``MYCELIUM_EMBED_MODEL_IS_MRL``); the embedder round passes it per
        candidate, because in a round the claim belongs to the candidate
        and is part of the evidence for adopting it.

        ``revision`` pins the checkpoint to one commit of its repository.
        ``None`` follows the repository's default branch, which is what a
        deployment wants (it upgrades when the image is rebuilt) and what a
        measurement must not do: a model id is a moving reference, and
        ``Qwen3-Embedding-0.6B`` was republished in April 2026, after the
        round that scored it. A comparison whose inputs can change under it
        is not reproducible, so the round pins every candidate."""
        self._model_name = model_name
        self._mrl = mrl
        self._revision = revision
        self._model: object | None = None
        self._load_lock = asyncio.Lock()

    def _load_sync(self) -> object:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - optional extra
                raise RuntimeError(
                    "LocalEmbedder requires the 'sentence-transformers' extra"
                ) from exc
            model = SentenceTransformer(self._model_name, revision=self._revision)
            self._check_dim_is_usable(model)
            # bge-m3 ships max_seq_length=8192. Transformer attention cost is
            # quadratic in sequence length, so a single 8192-token sequence
            # alone can allocate multiple GB of activations, and the encode
            # below would then multiply that by the batch. The worker was
            # OOMKilled in exactly this path (backfilling long note parts),
            # taking every other worker job down with it. Cap the window
            # instead: texts longer than the cap are truncated, which is what
            # already happened past 8192 -- we are moving the truncation
            # point, not introducing it.
            cap = get_settings().embedder_max_seq_tokens
            if cap > 0:
                current = getattr(model, "max_seq_length", None)
                if not isinstance(current, int) or current > cap:
                    model.max_seq_length = cap
            self._model = model
        return self._model

    def _check_dim_is_usable(self, model: object) -> None:
        """Refuse, at LOAD time, a checkpoint whose width the fleet cannot
        honor. Here rather than at the first encode because this is a
        property of the configuration, not of the text: the answer is the
        same for every call, so the first one should be the one that says
        so, while the process is still starting and the message can still
        name the model."""
        getter = getattr(model, "get_sentence_embedding_dimension", None)
        native = getter() if callable(getter) else None
        if not isinstance(native, int) or native == EMBED_DIM:
            # Unknown width is not a refusal: a stand-in that does not
            # implement the accessor is still a usable embedder, and the
            # real check on what it emits is the one at the write.
            return
        if native < EMBED_DIM:
            raise RuntimeError(
                f"embedder {self._model_name!r} emits {native} dims, narrower than the "
                f"fleet dim {EMBED_DIM}. A short vector cannot be padded faithfully "
                f"(the inner-product opclass assumes a real unit vector), so this "
                f"checkpoint cannot serve the local tier as the column stands."
            )
        if not self._mrl_declared():
            raise RuntimeError(
                f"embedder {self._model_name!r} emits {native} dims, wider than the fleet "
                f"dim {EMBED_DIM}, and has not been declared Matryoshka-trained. "
                f"Truncating a non-MRL checkpoint degrades retrieval silently. Set "
                f"MYCELIUM_EMBED_MODEL_IS_MRL=true (or pass mrl=True) only if the model "
                f"card documents MRL support at {EMBED_DIM} dims."
            )

    def _mrl_declared(self) -> bool:
        return get_settings().embed_model_is_mrl if self._mrl is None else self._mrl

    async def _model_ready(self) -> object:
        # Fast path: already loaded, no lock contention.
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                return await asyncio.to_thread(self._load_sync)
            return self._model

    async def prewarm(self) -> None:
        """Force the in-memory load now (off the request path). Safe to
        call multiple times; subsequent calls are no-ops."""
        await self._model_ready()

    @property
    def native_dim(self) -> int | None:
        """Dimension the loaded model actually emits, BEFORE the fleet-dim
        coercion in ``embed``; ``None`` until the model is loaded.

        Exposed because that coercion is otherwise invisible: a checkpoint
        running 4096 -> 1024 and one running natively at 1024 produce
        output of the same shape, and only the first has paid the
        Matryoshka truncation. A comparison between embedders that does not
        report this is not interpretable."""
        model = self._model
        if model is None:
            return None
        getter = getattr(model, "get_sentence_embedding_dimension", None)
        dim = getter() if callable(getter) else None
        return int(dim) if isinstance(dim, int) else None

    def declared_prompt(self, side: EmbedSide) -> str | None:
        """The instruction prefix this CHECKPOINT declares for ``side``, or
        ``None`` when it declares none (a symmetric model, or a side it does
        not distinguish). ``None`` until the model is loaded.

        Read from the checkpoint's own ``config_sentence_transformers.json``
        (sentence-transformers exposes it as ``model.prompts``) rather than
        configured next to the model name. A prefix written by hand beside a
        model id is a second copy of something the model already states, and
        the two drift the first time a checkpoint is republished with a
        different instruction -- at which point the model is being run
        outside its training configuration again, which is the exact failure
        this seam exists to prevent."""
        prompts = getattr(self._model, "prompts", None)
        if not isinstance(prompts, dict):
            return None
        prompt = prompts.get(side.value)
        return prompt if isinstance(prompt, str) and prompt else None

    def _prompt_name(self, side: EmbedSide) -> str | None:
        """The ``prompt_name`` to hand ``encode``. ``None`` when the model
        declares nothing for this side -- passing a name it does not know is
        a ValueError, not a no-op."""
        return side.value if self.declared_prompt(side) is not None else None

    async def embed(  # pragma: no cover - network/model
        self, text: str, *, side: EmbedSide
    ) -> EmbedResult:
        model = await self._model_ready()
        prompt_name = self._prompt_name(side)

        def _run() -> list[float]:
            return list(
                model.encode(  # type: ignore[attr-defined]
                    text, normalize_embeddings=True, prompt_name=prompt_name
                )
            )

        vec = await asyncio.to_thread(_run)
        return EmbedResult(
            vector=_truncate_normalize(vec, EMBED_DIM),
            model_id=self._model_name,
            tokens=max(1, len(text.split())),
        )

    async def embed_batch(  # pragma: no cover - model
        self, texts: list[str], *, side: EmbedSide
    ) -> list[EmbedResult]:
        """Encode many texts, grouped so that peak memory is bounded.

        SentenceTransformer batches internally, so batching is ~an order of
        magnitude faster than N calls to ``embed``. But a FIXED batch count
        (the pre-fix ``batch_size=32``) bounds the number of sequences, not
        the amount of work: 32 short titles and 32 full note parts differ by
        two orders of magnitude in activation memory, and the second shape is
        what OOMKilled the worker. So the grouping here is by an estimated
        TOKEN budget instead: long texts land in small groups, short ones
        still ride in large ones, and the peak is roughly flat either way.
        The estimate is deliberately cheap (chars/4, clamped to the model's
        window) -- running the real tokenizer to decide how to call the
        tokenizer is not worth it, and over-estimating only costs a smaller
        group.
        """
        if not texts:
            return []
        model = await self._model_ready()
        settings = get_settings()
        groups = group_by_token_budget(
            texts,
            budget=settings.embedder_batch_token_budget,
            window=settings.embedder_max_seq_tokens,
        )
        # The batched path is the INGEST path, so a side dropped here would
        # mis-embed a whole corpus while the single-encode path stayed right,
        # and the run would still produce a complete table.
        prompt_name = self._prompt_name(side)

        def _run() -> list[list[float]]:
            rows: list[list[float]] = []
            for group in groups:
                arr = model.encode(  # type: ignore[attr-defined]
                    group,
                    normalize_embeddings=True,
                    batch_size=len(group),
                    prompt_name=prompt_name,
                )
                rows.extend(list(row) for row in arr)
            return rows

        vecs = await asyncio.to_thread(_run)
        return [
            EmbedResult(
                vector=_truncate_normalize(v, EMBED_DIM),
                model_id=self._model_name,
                tokens=max(1, len(t.split())),
            )
            for v, t in zip(vecs, texts, strict=True)
        ]


def _truncate_normalize(vec: list[float], target_dim: int) -> list[float]:
    """Coerce a raw embedding to exactly ``target_dim`` L2-normalized
    floats. Matryoshka models keep their leading dims meaningful, so a
    longer vector is truncated; a shorter one cannot be padded
    faithfully (the IP opclass assumes a real unit vector), so the
    caller treats that as a dim mismatch upstream."""
    out = [float(x) for x in vec[:target_dim]]
    norm = math.sqrt(sum(x * x for x in out))
    if norm > 0:
        out = [x / norm for x in out]
    return out


class HostedEmbedder:
    """OpenAI-compatible ``/v1/embeddings`` client (Scaleway Generative
    APIs). httpx-only, same shape as :class:`mycelium_core.llm_openai.OpenAILLM`.
    Emits exactly ``target_dim`` floats: it requests ``dimensions`` (the
    Matryoshka knob) and defensively truncates + L2-renormalizes
    client-side, so the fleet dim is always honored regardless
    of what the endpoint returns. Token counts come from the API ``usage``
    block so the metering seam charges real tokens."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        target_dim: int,
        timeout: float = 30.0,
        prefixes: Mapping[EmbedSide, str] | None = None,
    ) -> None:
        """``prefixes`` is the hosted equivalent of what a local checkpoint
        declares in its own config. It has to be supplied rather than read,
        because an ``/v1/embeddings`` endpoint serves a model without
        exposing its sentence-transformers configuration: the instruction
        the model was trained with is knowable only from its model card.
        Empty (the default) is a symmetric model, and is what every hosted
        provider configured so far is."""
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._target_dim = target_dim
        self._timeout = timeout
        self._prefixes = dict(prefixes or {})

    def _apply(self, text: str, side: EmbedSide) -> str:
        prefix = self._prefixes.get(side, "")
        return f"{prefix}{text}" if prefix else text

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def native_dim(self) -> int | None:
        """Always ``None``: an ``/v1/embeddings`` endpoint returns a vector of
        the width that was ASKED for (``dimensions``), and says nothing about
        the width the model emits before that. Reporting the requested width
        here would claim a measurement nobody made -- a hosted candidate that
        truncated and one that did not would look identical, and the round
        prints this column precisely to tell them apart."""
        return None

    def declared_prompt(self, side: EmbedSide) -> str | None:
        """The prefix this client prepends for ``side``, or ``None``.

        Same accessor as :meth:`LocalEmbedder.declared_prompt` and a weaker
        claim: there it is read from the checkpoint, here it is what someone
        configured from the model card. The round reports it either way, so a
        table never implies a model ran instruction-tuned when it did not."""
        prefix = self._prefixes.get(side, "")
        return prefix or None

    def _payload(self, input_: object) -> dict[str, object]:
        return {"model": self._model, "input": input_, "dimensions": self._target_dim}

    async def _post(self, input_: object) -> Any:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.post(
                f"{self._base_url}/embeddings", json=self._payload(input_), headers=headers
            )
            r.raise_for_status()
            return r.json()

    @staticmethod
    def _tokens(usage: Any, fallback: str) -> int:
        total = usage.get("total_tokens") or usage.get("prompt_tokens")
        if isinstance(total, int) and total > 0:
            return total
        return max(1, len(fallback.split()))

    async def embed(self, text: str, *, side: EmbedSide) -> EmbedResult:
        data = await self._post(self._apply(text, side))
        rows = data.get("data") or []
        raw = (rows[0] if rows else {}).get("embedding") or []
        usage = data.get("usage") or {}
        return EmbedResult(
            vector=_truncate_normalize(raw, self._target_dim),
            model_id=self._model,
            tokens=self._tokens(usage, text),
        )

    async def embed_batch(self, texts: list[str], *, side: EmbedSide) -> list[EmbedResult]:
        if not texts:
            return []
        data = await self._post([self._apply(t, side) for t in texts])
        rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        usage = data.get("usage") or {}
        # The batch usage is for the whole call; attribute a per-row share
        # (at least 1 token each) so metering stays additive and non-zero.
        total = usage.get("total_tokens") or usage.get("prompt_tokens") or 0
        per = max(1, int(total) // len(texts)) if isinstance(total, int) and total else 1
        out: list[EmbedResult] = []
        for row, t in zip(rows, texts, strict=False):
            raw = row.get("embedding") or []
            out.append(
                EmbedResult(
                    vector=_truncate_normalize(raw, self._target_dim),
                    model_id=self._model,
                    tokens=per if per > 1 else max(1, len(t.split())),
                )
            )
        return out


_FactoryFn = Callable[[], Embedder]
_override: _FactoryFn | None = None
_hosted_override: _FactoryFn | None = None
_singleton: LocalEmbedder | None = None


def set_embedder_override(fn: _FactoryFn | None) -> None:
    """Test seam: replace the LOCAL model-backed embedder with an
    in-memory one. Production leaves this None. Never set in production."""
    global _override
    _override = fn


def set_hosted_embedder_override(fn: _FactoryFn | None) -> None:
    """Test seam for the HOSTED tier: when set, ``resolve_hosted_embedder``
    returns this fake for every org (basis our_key), so tests can exercise
    the local+hosted dual-write/dual-read without a real Scaleway call.
    Production leaves this None."""
    global _hosted_override
    _hosted_override = fn


def get_hosted_embedder_override() -> _FactoryFn | None:
    """The hosted-tier test override, consumed by the embedder resolver."""
    return _hosted_override


def get_embedder() -> Embedder:
    """The LOCAL embedder (the always-on rank-0 tier, ``embedding``
    column). Settings ``embed_model`` picks the model (default bge-m3,
    1024d). Override via ``set_embedder_override`` for tests."""
    if _override is not None:
        return _override()
    global _singleton
    if _singleton is None:
        from mycelium_core.config import get_settings as _gs

        _singleton = LocalEmbedder(_gs().embed_model)
    return _singleton


def embedder_available() -> bool:
    """Cheap probe for status reporting: can a usable embedder be
    produced *without* loading the model? An injected override (CI, or
    a future hosted provider) is always considered available; otherwise
    the local model needs the optional ``sentence-transformers`` extra,
    so we only check that it is importable (no model download/load).
    Never raises."""
    if _override is not None:
        return True
    try:
        return importlib.util.find_spec("sentence_transformers") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


async def embed_batch(emb: Embedder, texts: list[str], *, side: EmbedSide) -> list[EmbedResult]:
    """Use the embedder's batched API when available, fall back to a
    sequential loop otherwise. Lets callers (e.g. gateway index build)
    benefit from real batching without forcing every Embedder to
    implement it on the Protocol."""
    method = getattr(emb, "embed_batch", None)
    if method is not None:
        coro = cast(Callable[..., Awaitable[list[EmbedResult]]], method)
        return await coro(texts, side=side)
    return [await emb.embed(t, side=side) for t in texts]
