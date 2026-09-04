"""Cross-package test helpers (importable from any tests/ tree).

Pytest's rootdir is the workspace root (where ``pyproject.toml`` lives)
and gets added to ``sys.path`` automatically, so tests in
``api/tests/``, ``core/tests/``, ``mcp/tests/`` can ``from
tests_helpers import ...`` without any conftest gymnastics.

Kept intentionally minimal --- only the helpers that two or more test
packages would otherwise duplicate.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.mcp_scopes import SCOPE_CATALOG
from mycelium_core.models.ai_assistant import AiAssistant, AssistantRuntime
from mycelium_core.models.identity import Identity

# A fully-capable NON-DANGER assistant (every read + every write) for tests that
# need a writable agent. Deliberately DECOUPLED from the product DEFAULT_SCOPES,
# which is reads-only since the taxonomy review (task c19f2f63): tests want a
# working assistant, not the least-privilege default, so the two must not move
# together. Pass an explicit ``scope`` to seed a narrower one.
_TEST_CAPABLE_SCOPES: tuple[str, ...] = tuple(
    s.key for s in SCOPE_CATALOG if s.category != "danger"
)


async def seed_ai_assistant_identity(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    label: str = "agent",
    scope: Sequence[str] | None = None,
    runtime: AssistantRuntime = AssistantRuntime.internal,
) -> Identity:
    """Insert an ``AiAssistant`` and return its mirrored ``Identity``.

    The identity row is materialised by the migration-0085 after-insert
    trigger, conditional on the assistant carrying a non-empty handle
    --- so we mint a unique handle inline rather than going through
    ``ai_assistants_svc.create_assistant`` (which is owner-gated and
    also mints an agent_token we don't need here).

    ``scope`` ``None`` seeds ``_TEST_CAPABLE_SCOPES`` (every non-danger key, so
    the seeded agent can read AND write), NOT the product ``DEFAULT_SCOPES``
    (reads-only since the taxonomy review): tests want a working assistant, and
    decoupling keeps a policy change from silently breaking every write test.
    An empty list is a deliberate deny-all assistant; the model treats ``[]`` as
    "no scopes", matching the gate's fail-closed default. Pass an explicit subset
    to seed a narrowly-scoped assistant.

    Tests that exercise the scheduler / dispatch agent paths use the
    returned identity as ``assignee_id`` so the task is unambiguously
    routed to the llm_agent pool under the "default assignee = creator"
    rule (the human-only fallback would otherwise auto-assign the task
    to the calling user).

    ``runtime`` defaults to ``internal``, which is the OPPOSITE of the
    product default. That is deliberate and it is the point of the
    parameter: the column's server default is ``external``, and an
    external assistant is by definition not routed to the llm pool, so
    inheriting it here would leave every existing scheduler/dispatch
    test asserting the reverse of what it was written to assert -- and
    passing. Tests that want the external half say so explicitly."""
    handle = f"{label}-{uuid.uuid4().hex[:8]}"
    eff_scope = list(_TEST_CAPABLE_SCOPES) if scope is None else list(scope)
    assistant = AiAssistant(
        org_id=org_id,
        user_id=user_id,
        label=label,
        handle=handle,
        scope=eff_scope,
        is_active=True,
        runtime=runtime,
    )
    session.add(assistant)
    await session.flush()
    identity = (
        await session.execute(
            select(Identity).where(
                Identity.org_id == org_id,
                Identity.ai_assistant_id == assistant.id,
            )
        )
    ).scalar_one()
    return identity
