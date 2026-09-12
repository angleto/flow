"""Memory redesign (DB-backed): graceful degradation when the optional
embedding model is missing, and memory channels as a tag kind.

The embedder is swapped through the ADR-0012 seam
(``set_embedder_override``). "Missing" is simulated with an override
whose ``embed`` raises (the same failure path as the missing
``sentence-transformers`` extra); ``/memory/status`` is additionally
exercised by clearing the override and stubbing ``find_spec`` so the
cheap probe reports unavailable without any override shortcut. The
override is always restored in teardown so other tests are unaffected
(mirrors test_f6_api.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

import mycelium_core.embedder as embedder_mod
from mycelium_api.main import app
from mycelium_core.embedder import EmbedResult, EmbedSide, set_embedder_override


class _BrokenEmbedder:
    """Stands in for the missing optional model: instantiable, but
    ``embed`` raises exactly like LocalEmbedder without the extra."""

    model_id = "broken-embed"

    async def embed(self, text: str, *, side: EmbedSide) -> EmbedResult:
        raise RuntimeError("LocalEmbedder requires the 'sentence-transformers' extra")


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


@pytest.fixture
def _broken_embedder() -> Iterator[None]:
    set_embedder_override(_BrokenEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _grant_and_rate(c: AsyncClient, h: dict[str, str]) -> None:
    await c.post("/billing/grant", headers=h, json={"amount": "100"})
    await c.post(
        "/billing/rate-cards",
        headers=h,
        json={
            "model_id": FakeEmbedder.model_id,
            "provider": "local",
            "credits_per_input": "0.001",
        },
    )


async def test_embedder_missing_write_and_lexical_recall(_broken_embedder: None) -> None:
    """Embedder unavailable: write still 200 and persists a keyword-only
    blob (model_id == "none"); a search whose query word is in the text
    finds it via the lexical fallback. No 500, no rate card needed (no
    embedding cost is metered)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # Deliberately NO billing grant / rate card: the keyword-only
        # path must not meter an embedding, so it must not require one.
        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "quarterly budget review for project alpha",
                "operation_id": "w-missing-1",
            },
        )
        assert w.status_code == 200, w.text
        body = w.json()
        assert body["model_id"] == "none"
        assert body["tier"] == "hot"
        blob_id = body["id"]

        found = await c.post(
            "/memory/search",
            headers=h,
            json={"project_id": proj, "query": "budget", "operation_id": "q-missing-1"},
        )
        assert found.status_code == 200, found.text
        hits = found.json()
        assert hits, "lexical fallback must return the blob"
        assert blob_id in {x["blob"]["id"] for x in hits}


async def test_embedder_present_semantic_recall(_fake_embedder: None) -> None:
    """Embedder available (fake): write + semantic recall returns the
    blob, and it is recorded with the real model id."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "the budget review for project alpha",
                "operation_id": "w-present-1",
            },
        )
        assert w.status_code == 200, w.text
        assert w.json()["model_id"] == FakeEmbedder.model_id
        blob_id = w.json()["id"]

        found = await c.post(
            "/memory/search",
            headers=h,
            json={"project_id": proj, "query": "budget alpha", "operation_id": "q-present-1"},
        )
        assert found.status_code == 200, found.text
        assert blob_id in {x["blob"]["id"] for x in found.json()}


async def test_memory_channel_filtering(_fake_embedder: None) -> None:
    """A seeded memory_channel tag (controlled vocabulary, obtained from
    GET /memory/channels -- NOT created ad-hoc via the generic tag
    endpoint, which now rejects kind=memory_channel) is folded into a
    blob on write and used as an AND facet on search: the right channel
    matches, a different one does not, and a non-channel tag id is
    rejected with a 4xx code."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)

        # Channels are a seeded, controlled vocabulary now: the generic
        # tag endpoint refuses kind=memory_channel; the tenant's
        # canonical channels come from GET /memory/channels instead.
        rejected = await c.post(
            "/tags", headers=h, json={"kind": "memory_channel", "name": "agent-scratch"}
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["code"] == "channel.not_tag_creatable"

        channels = (await c.get("/memory/channels", headers=h)).json()
        channel_id = next(ch["id"] for ch in channels if ch["system_key"] == "agent")
        other_channel_id = next(ch["id"] for ch in channels if ch["system_key"] == "manual")

        # A generic tag id is NOT a valid channel.
        generic = await c.post("/tags", headers=h, json={"kind": "generic", "name": "g1"})
        assert generic.status_code == 200, generic.text
        generic_id = generic.json()["id"]

        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "channelled memo about onboarding",
                "operation_id": "w-ch-1",
                "channel_tag_id": channel_id,
            },
        )
        assert w.status_code == 200, w.text
        blob_id = w.json()["id"]
        assert channel_id in {t["id"] for t in w.json()["tags"]}

        same = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": "onboarding",
                "operation_id": "q-ch-1",
                "channel_tag_id": channel_id,
            },
        )
        assert same.status_code == 200, same.text
        assert blob_id in {x["blob"]["id"] for x in same.json()}

        different = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": "onboarding",
                "operation_id": "q-ch-2",
                "channel_tag_id": other_channel_id,
            },
        )
        assert different.status_code == 200, different.text
        assert different.json() == []

        bad = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "x",
                "operation_id": "w-ch-bad",
                "channel_tag_id": generic_id,
            },
        )
        assert bad.status_code == 400, bad.text
        assert bad.json()["code"] == "tag.kind_mismatch"

        bad_search = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": "onboarding",
                "operation_id": "q-ch-bad",
                "channel_tag_id": generic_id,
            },
        )
        assert bad_search.status_code == 400, bad_search.text
        assert bad_search.json()["code"] == "tag.kind_mismatch"


async def test_memory_status_true_with_fake_override(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.get("/memory/status", headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == {"semantic": True}


async def test_memory_status_false_when_model_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No override set + the optional package not importable: the cheap
    probe must report keyword-only. Override is force-cleared and the
    spec lookup stubbed; nothing here persists past the test."""
    set_embedder_override(None)
    monkeypatch.setattr(
        embedder_mod.importlib.util,
        "find_spec",
        lambda name: None if name == "sentence_transformers" else object(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.get("/memory/status", headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == {"semantic": False}


async def test_chunk_index_and_snippet_for_multi_chunk_note(
    _fake_embedder: None,
) -> None:
    """task d46833bb: MemoryHitOut surfaces ``chunk_index`` and
    ``chunk_snippet`` for paragraph-split notes; ``chunk_index=0`` and
    ``chunk_snippet=None`` for single-vector (whole-doc) blobs.

    Multi-chunk: a long note (>800 words, namespace='note') gets
    paragraph-split by ``pick_chunker`` so each paragraph becomes its
    own blob with monotonic ``chunk_index``. A search whose terms hit
    only one paragraph must return that chunk's index and a
    ts_headline snippet of THAT chunk text.

    Single-vector: a short blob stays whole-doc (one BlobSource row,
    chunk_index=0); the API exposes ``chunk_index=0`` and
    ``chunk_snippet=None`` (no targeted snippet needed)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        proj = str(uuid.uuid4())

        # Build a >800-token note so ParagraphChunker kicks in. Three
        # blank-line-separated paragraphs; the query term lives only
        # in paragraph 2 (so the winning chunk_index must be 1).
        para0 = "alpha " * 400
        para1 = "the quarterly zorblax-marker review covers projections " * 60
        para2 = "epsilon " * 400
        long_text = f"{para0}\n\n{para1}\n\n{para2}"
        # BlobSource rows are what drives the multi-chunk detection +
        # chunk_index attribution; pass a synthetic ``(kind, id)`` so
        # each chunk records its index against the same parent.
        note_src_id = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "namespace": "note",
                "text": long_text,
                "operation_id": "w-chunk-1",
                "sources": [["note", note_src_id]],
            },
        )
        assert w.status_code == 200, w.text

        found = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": "zorblax-marker",
                "operation_id": "q-chunk-1",
            },
        )
        assert found.status_code == 200, found.text
        hits = found.json()
        assert hits, "the multi-chunk note must surface for the query"
        top = hits[0]
        # The winning chunk is the middle one (where the marker lives);
        # chunk_index >= 1 disambiguates "first chunk of multi-chunk"
        # from "whole-doc".
        assert top["chunk_index"] >= 1, top
        assert top["chunk_snippet"] is not None, top
        # ts_headline wraps matches in <b>...</b> per token (so the
        # hyphenated marker arrives as ``<b>zorblax</b>-<b>marker</b>``);
        # the literal token "zorblax" is enough proof the snippet
        # targets the right paragraph.
        assert "<b>zorblax</b>" in top["chunk_snippet"].lower(), top["chunk_snippet"]

        # Single-vector blob (short text): chunk_index=0, no snippet.
        proj2 = str(uuid.uuid4())
        single_src_id = str(uuid.uuid4())
        await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj2,
                "text": "remember the unique-fact-xtb keyword",
                "operation_id": "w-chunk-2",
                "sources": [["note", single_src_id]],
            },
        )
        found2 = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj2,
                "query": "unique-fact-xtb",
                "operation_id": "q-chunk-2",
            },
        )
        assert found2.status_code == 200, found2.text
        hits2 = found2.json()
        assert hits2
        assert hits2[0]["chunk_index"] == 0
        assert hits2[0]["chunk_snippet"] is None


async def test_rechunk_endpoint_re_indexes_legacy_long_note(
    _fake_embedder: None,
) -> None:
    """task 2149e753: POST /memory/rechunk takes a workspace where a
    long note was indexed pre-chunking (one BlobSource with
    chunk_index=0) and re-writes it through the ParagraphChunker so
    the source ends up with N>1 BlobSource rows.

    The fixture builds the legacy state by hand-overriding the
    chunker on write (``WholeChunker``) -- that simulates the deploy
    state where every note was single-vector. After rechunk we
    re-read the source's BlobSource rows from the public surface
    (the search hits) to confirm the new chunked indexing took.
    """
    from mycelium_core.bootstrap_admin import ensure_admin
    from mycelium_core.services import memory as mem_svc
    from mycelium_core.services.chunker import WholeChunker

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # /memory/rechunk is admin-gated (tenant_admin_ctx) like
        # /memory/migrate-embeddings: needs a platform admin AND the
        # X-Admin-Mode elevation header. Bootstrap one for the test.
        admin_email = _email()
        admin_pw = "Str0ng-Passw0rd!"
        await ensure_admin(admin_email, admin_pw)
        login = (
            await c.post("/auth/login", json={"email": admin_email, "password": admin_pw})
        ).json()
        me = (await c.get("/auth/me", headers={"Authorization": f"Bearer {login['token']}"})).json()
        orgs = (
            await c.get(
                "/workspaces",
                headers={"Authorization": f"Bearer {login['token']}"},
            )
        ).json()
        ws = orgs[0]["id"] if orgs else me["workspace_id"]
        h = {
            "Authorization": f"Bearer {login['token']}",
            "X-Workspace-Id": str(ws),
        }
        h_admin = {**h, "X-Admin-Mode": "1"}
        org_id = uuid.UUID(str(ws))
        user_id = uuid.UUID(me["user_id"])
        await _grant_and_rate(c, h)
        proj_id = uuid.uuid4()

        # Pre-rechunk fixture: pin a long note as a single whole-doc
        # blob, mimicking the deploy state. The marker lives in
        # paragraph 1 so a search for it will only match a real chunk
        # AFTER the rechunk (when paragraph 1 becomes its own blob).
        para0 = "alpha " * 400
        para1 = "the quarterly zorbgg-marker review covers projections " * 60
        para2 = "epsilon " * 400
        long_text = f"{para0}\n\n{para1}\n\n{para2}"
        source_id = str(uuid.uuid4())

        from mycelium_core.db import tenant_session

        async with tenant_session(str(org_id), str(user_id)) as s:
            await mem_svc.write_blob(
                s,
                org_id=org_id,
                actor_id=user_id,
                project_id=proj_id,
                text_body=long_text,
                operation_id="legacy-write",
                namespace="note",
                sources=[("note", source_id)],
                chunker=WholeChunker(),
            )

        # Sanity: before rechunk, the marker search returns the legacy
        # whole-doc blob (chunk_index=0). The semantic FakeEmbedder
        # also ranks it on top because the marker text dominates the
        # vector.
        pre = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": str(proj_id),
                "query": "zorbgg-marker",
                "operation_id": "q-pre",
            },
        )
        assert pre.status_code == 200, pre.text
        pre_hits = pre.json()
        assert pre_hits, "pre-rechunk: the legacy blob must still be findable"
        assert pre_hits[0]["chunk_index"] == 0

        # Dry-run reports the candidate count without touching data.
        dry = await c.post("/memory/rechunk?dry_run=true", headers=h_admin)
        assert dry.status_code == 200, dry.text
        dry_body = dry.json()
        assert dry_body["rechunked"] >= 1
        assert dry_body["scanned"] >= 1

        # Real run: rechunked count matches what dry-run promised.
        real = await c.post("/memory/rechunk", headers=h_admin)
        assert real.status_code == 200, real.text
        real_body = real.json()
        assert real_body["rechunked"] == dry_body["rechunked"]

        # Post-rechunk: the same query now picks up a real chunk
        # (chunk_index >= 1). The progressive-dedupe guard makes sure
        # the (deleted) chunk_index=0 sibling doesn't sneak back in.
        post = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": str(proj_id),
                "query": "zorbgg-marker",
                "operation_id": "q-post",
            },
        )
        assert post.status_code == 200, post.text
        post_hits = post.json()
        assert post_hits
        assert post_hits[0]["chunk_index"] >= 1, post_hits[0]

        # Calling rechunk again is a no-op (idempotent): the candidate
        # selector skips sources that already have chunk_index > 0.
        again = await c.post("/memory/rechunk", headers=h_admin)
        assert again.status_code == 200, again.text
        assert again.json()["rechunked"] == 0

        # Non-admin (or admin without X-Admin-Mode) is rejected.
        denied = await c.post("/memory/rechunk", headers=h)
        assert denied.status_code == 403, denied.text


async def test_embedder_present_but_no_rate_card_is_free(
    _fake_embedder: None,
) -> None:
    """The real out-of-the-box scenario: the embedder works but the
    org has NO rate card for the embedding model. Embedding via the
    bundled model is free, so write + recall must succeed (not 400
    billing.rate_card_not_found). Regression for "memory does
    nothing"."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # Deliberately NO _grant_and_rate: no wallet grant, no rate card.
        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "remember the zqx-token billing fact",
                "operation_id": "w-free-1",
            },
        )
        assert w.status_code == 200, w.text
        assert w.json()["model_id"] == FakeEmbedder.model_id
        blob_id = w.json()["id"]

        found = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": "zqx-token",
                "operation_id": "q-free-1",
            },
        )
        assert found.status_code == 200, found.text
        assert blob_id in {x["blob"]["id"] for x in found.json()}
