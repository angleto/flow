"""Per-org embedder provider selection (task 5276207e).

One row per org picks which embedder backs the ``Embedder`` seam for that
tenant: the bundled local model (bge-m3, the rank-0 fallback) or a hosted
provider (Scaleway Generative APIs ``/v1/embeddings``) on our key or on
the org's own Fernet-encrypted key (BYOK). ``services.embedder_resolver``
reads this row, picks the embedder, and derives the ``CostBasis`` the
metering seam charges on. No row => the local embedder, basis ``local``.

Mirrors :mod:`mycelium_core.models.org_llm_provider`. Every hosted embedder
MUST emit ``embed_dims.EMBED_DIM_HOSTED``; the resolver enforces it.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, CheckConstraint, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, TimestampMixin, VersionMixin


class EmbedderProviderKind(enum.StrEnum):
    local = "local"
    scaleway = "scaleway"


class OrgEmbedderProvider(TimestampMixin, VersionMixin, Base):
    __tablename__ = "org_embedder_provider"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('local', 'scaleway')",
            name="ck_org_embedder_provider_kind",
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, server_default="local")
    # Hosted embedding model id (e.g. ``qwen/qwen3-embedding-8b``). NULL
    # for ``local`` (the model then comes from ``settings.embed_model``).
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Fernet ciphertext of the org's OWN provider key (BYOK). NULL means
    # "use our key" for a hosted provider (basis our_key), or N/A for local.
    api_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-row base URL for the hosted (OpenAI-compatible) embeddings
    # endpoint. NULL falls back to ``settings.scaleway_base_url``.
    base_url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
