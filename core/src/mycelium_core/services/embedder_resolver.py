"""Per-org hosted embedder resolution (task 5276207e).

The hosted tier mirror of :mod:`mycelium_core.services.llm_resolver`. The
LOCAL tier is always-on (``embedder.get_embedder()`` -> bge-m3, the
``embedding`` column); this module resolves the optional HOSTED tier
(``embedding_hosted`` halfvec column) per org:

- no row / inactive / ``local`` / no key resolvable -> ``None`` (the org
  has no hosted tier; writes/reads use the local tier only);
- ``scaleway`` with the org's OWN Fernet key -> ``(HostedEmbedder, byok)``;
- ``scaleway`` on OUR key (``settings.scaleway_api_key``) -> ``(..., our_key)``.

Every hosted embedder MUST emit ``embed_dims.EMBED_DIM_HOSTED``; the
fail-closed probe in ``set_org_embedder_provider`` rejects a key/model
that can't, so a bad config is never stored active.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.crypto import decrypt_secret, encrypt_secret
from mycelium_core.embed_dims import EMBED_DIM_HOSTED
from mycelium_core.embedder import (
    Embedder,
    EmbedSide,
    HostedEmbedder,
    get_hosted_embedder_override,
)
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.billing import CostBasis
from mycelium_core.models.membership import Role
from mycelium_core.models.org_embedder_provider import EmbedderProviderKind, OrgEmbedderProvider
from mycelium_core.services import audit
from mycelium_core.services.rbac import require_role

# Default Scaleway embedding model (SHORT id, as ``/v1/models`` lists).
# qwen3-embedding-8b is Matryoshka (native 4096); verified to return
# exactly the fleet hosted dim (4000) with ``dimensions=4000``.
_DEFAULT_SCALEWAY_EMBED_MODEL = "qwen3-embedding-8b"


async def get_org_embedder_provider(
    session: AsyncSession, org_id: uuid.UUID
) -> OrgEmbedderProvider | None:
    return (
        await session.execute(
            select(OrgEmbedderProvider).where(OrgEmbedderProvider.org_id == org_id)
        )
    ).scalar_one_or_none()


def _build_scaleway_embedder(
    *, key: str, model: str | None, base_url: str | None
) -> HostedEmbedder:
    settings = get_settings()
    return HostedEmbedder(
        api_key=key,
        model=model or _DEFAULT_SCALEWAY_EMBED_MODEL,
        base_url=base_url or settings.scaleway_base_url,
        target_dim=EMBED_DIM_HOSTED,
    )


async def resolve_hosted_embedder(
    session: AsyncSession, org_id: uuid.UUID
) -> tuple[Embedder, CostBasis] | None:
    """The org's hosted embedder + the basis to meter it on, or ``None``
    when the org has no hosted tier (writes/reads stay local-only)."""
    override = get_hosted_embedder_override()
    if override is not None:
        return override(), CostBasis.our_key

    cfg = await get_org_embedder_provider(session, org_id)
    if cfg is None or not cfg.is_active or cfg.provider == EmbedderProviderKind.local:
        return None

    settings = get_settings()
    own_key = decrypt_secret(cfg.api_key_ciphertext) if cfg.api_key_ciphertext else None
    if cfg.provider == EmbedderProviderKind.scaleway:
        key = own_key or settings.scaleway_api_key
        if not key:
            return None
        embedder = _build_scaleway_embedder(key=key, model=cfg.model, base_url=cfg.base_url)
        return embedder, (CostBasis.byok if own_key else CostBasis.our_key)
    return None


async def _probe_embedder_key(
    kind: EmbedderProviderKind, *, key: str, model: str | None, base_url: str | None
) -> None:
    """Fail-closed: embed a tiny text with the candidate hosted embedder
    and require it to emit exactly the fleet hosted dim, so a key/model
    that can't fill ``embedding_hosted`` is never stored active. Any
    failure raises ``DomainError(PROVIDER_KEY_INVALID)``."""
    try:
        if kind == EmbedderProviderKind.scaleway:
            embedder = _build_scaleway_embedder(key=key, model=model, base_url=base_url)
            res = await embedder.embed("ping", side=EmbedSide.document)
            if len(res.vector) != EMBED_DIM_HOSTED:
                raise DomainError(MessageCode.PROVIDER_KEY_INVALID)
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError(MessageCode.PROVIDER_KEY_INVALID) from exc


async def set_org_embedder_provider(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    provider: str,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    validate_key: bool = True,
) -> OrgEmbedderProvider:
    """Admin: select the org's hosted embedder. ``api_key`` semantics on
    update: ``None`` leaves the stored key untouched, ``""`` clears it
    (back to our-key/local), a value (re)encrypts and stores it (BYOK).
    A NEW key is fail-closed probed (must emit the hosted dim) unless
    ``validate_key=False``."""
    await require_role(session, org_id, actor_id, Role.admin)
    try:
        kind = EmbedderProviderKind(provider)
    except ValueError as exc:
        raise DomainError(MessageCode.DOMAIN_ERROR) from exc

    if validate_key and api_key and kind != EmbedderProviderKind.local:
        await _probe_embedder_key(kind, key=api_key, model=model, base_url=base_url)

    existing = await get_org_embedder_provider(session, org_id)
    if existing is None:
        row = OrgEmbedderProvider(
            org_id=org_id,
            provider=kind.value,
            model=model,
            base_url=base_url,
            api_key_ciphertext=encrypt_secret(api_key) if api_key else None,
        )
        session.add(row)
        await session.flush()
    else:
        existing.provider = kind.value
        existing.model = model
        existing.base_url = base_url
        if api_key is not None:
            existing.api_key_ciphertext = encrypt_secret(api_key) if api_key else None
        existing.version += 1
        await session.flush()
        row = existing
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="org_embedder_provider",
        entity_id=None,
        action="set",
        diff={"provider": kind.value, "model": model or "", "base_url": base_url or ""},
    )
    return row
