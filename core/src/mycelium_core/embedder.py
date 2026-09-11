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
import importlib.util
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

import httpx

from mycelium_core.config import get_settings


@dataclass(frozen=True)
class EmbedResult:
    vector: list[float]
    model_id: str
    tokens: int  # billable units for metering (ADR-0019)


@runtime_checkable
class Embedder(Protocol):
    async def embed(self, text: str) -> EmbedResult: ...


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

    Emits exactly the fleet ``embed_dim`` via ``_truncate_normalize``,
    the same coercion :class:`HostedEmbedder` applies. The invariant
    ("every embedder, local or hosted, MUST emit this dim" --
    ``config.embed_dim``) was enforced on the hosted side only, so the
    local tier could host exactly one model: the default bge-m3, whose
    1024 native dim happens to equal the fleet dim. Any other checkpoint
    failed at the first write with ``memory.dim_mismatch``, which is why
    the 2026-07-03 embedder round could only compare 1024d models.
    Truncation is the documented Matryoshka procedure and is meaningful
    only for MRL-trained checkpoints; a non-MRL model truncated here
    loses quality silently, so a candidate's MRL support belongs in the
    evidence for adopting it. A model emitting FEWER dims than the fleet
    cannot be padded faithfully and still fails at the write, by
    design."""

    def __init__(self, model_name: str = "BAAI/bge-m3") -> None:
        self._model_name = model_name
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
            model = SentenceTransformer(self._model_name)
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

    async def embed(self, text: str) -> EmbedResult:  # pragma: no cover - network/model
        model = await self._model_ready()

        def _run() -> list[float]:
            return list(model.encode(text, normalize_embeddings=True))  # type: ignore[attr-defined]

        vec = await asyncio.to_thread(_run)
        return EmbedResult(
            vector=_truncate_normalize(vec, get_settings().embed_dim),
            model_id=self._model_name,
            tokens=max(1, len(text.split())),
        )

    async def embed_batch(self, texts: list[str]) -> list[EmbedResult]:  # pragma: no cover - model
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

        def _run() -> list[list[float]]:
            rows: list[list[float]] = []
            for group in groups:
                arr = model.encode(  # type: ignore[attr-defined]
                    group, normalize_embeddings=True, batch_size=len(group)
                )
                rows.extend(list(row) for row in arr)
            return rows

        vecs = await asyncio.to_thread(_run)
        target = settings.embed_dim
        return [
            EmbedResult(
                vector=_truncate_normalize(v, target),
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
    client-side, so the fleet ``embed_dim`` is always honored regardless
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
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._target_dim = target_dim
        self._timeout = timeout

    @property
    def model_id(self) -> str:
        return self._model

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

    async def embed(self, text: str) -> EmbedResult:
        data = await self._post(text)
        rows = data.get("data") or []
        raw = (rows[0] if rows else {}).get("embedding") or []
        usage = data.get("usage") or {}
        return EmbedResult(
            vector=_truncate_normalize(raw, self._target_dim),
            model_id=self._model,
            tokens=self._tokens(usage, text),
        )

    async def embed_batch(self, texts: list[str]) -> list[EmbedResult]:
        if not texts:
            return []
        data = await self._post(texts)
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


async def embed_batch(emb: Embedder, texts: list[str]) -> list[EmbedResult]:
    """Use the embedder's batched API when available, fall back to a
    sequential loop otherwise. Lets callers (e.g. gateway index build)
    benefit from real batching without forcing every Embedder to
    implement it on the Protocol."""
    method = getattr(emb, "embed_batch", None)
    if method is not None:
        coro = cast(Callable[[list[str]], Awaitable[list[EmbedResult]]], method)
        return await coro(texts)
    return [await emb.embed(t) for t in texts]


def embed_dim() -> int:
    return get_settings().embed_dim


def embed_dim_hosted() -> int:
    """Fixed dim of the hosted tier (``embedding_hosted``, halfvec)."""
    return get_settings().embed_dim_hosted
