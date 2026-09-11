"""The device authorization grant: open, answer, collect.

The assertions worth having here are about ORDER and about what exists at
each moment, because that is what the mechanism this replaces got wrong.
In particular: after an approval and before a collection, no credential
exists. That property is the reason the flow was rewritten, and a test
that only checked the happy path would pass just as well against the
version that minted first and handed over second.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import AuthError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_token import AgentToken, WorkspaceBinding
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.device_authorization import DeviceAuthorization
from mycelium_core.services import device_authorization as svc
from mycelium_core.services.auth import signup

SCOPE = ["tasks:read", "notes:read"]
LABEL = "Browser extension"
PROVIDER = "mycelium-extension"


async def _account() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="Device",
        )
    return r.org_id, r.user_id


async def _redeem(device_code: str) -> svc.RedeemedCredential:
    async with admin_session() as s:
        return await svc.redeem(
            s, device_code=device_code, label=LABEL, scope=SCOPE, provider=PROVIDER
        )


async def _assistant_count(org: uuid.UUID, user: uuid.UUID) -> int:
    async with tenant_session(str(org), str(user)) as s:
        rows = (await s.execute(select(AiAssistant.id))).scalars().all()
        return len(rows)


async def test_open_request_returns_two_codes_and_stores_only_the_digest() -> None:
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension", origin_ip="203.0.113.7")
        row = (
            await s.execute(
                select(DeviceAuthorization).where(DeviceAuthorization.user_code == opened.user_code)
            )
        ).scalar_one()

    # The long code is the only thing that collects, and it is not stored.
    assert opened.device_code
    assert opened.device_code not in row.device_code_hash
    assert len(row.device_code_hash) == 64
    # The short code is not a secret and is stored as it is displayed.
    assert row.user_code == opened.user_code
    assert len(opened.user_code) == 9
    assert opened.user_code[4] == "-"
    # Nothing ambiguous to read out loud.
    assert not set(opened.user_code) & set("ILOU01V")
    assert row.origin_ip == "203.0.113.7"
    # Nobody has answered, and nothing was minted.
    assert row.approved_at is None and row.denied_at is None and row.redeemed_at is None
    assert row.assistant_id is None


async def test_nothing_is_minted_until_the_device_collects() -> None:
    """The property the previous mechanism did not have."""
    org, user = await _account()
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension")

    before = await _assistant_count(org, user)

    async with admin_session() as s:
        await svc.approve(s, user_code=opened.user_code, approver_id=user, org_id=org)

    # Approved, and still nothing exists to be orphaned.
    assert await _assistant_count(org, user) == before

    granted = await _redeem(opened.device_code)

    assert granted.secret.startswith("mycelium_at_")
    assert await _assistant_count(org, user) == before + 1
    assert set(granted.scope) == set(SCOPE)


async def test_the_credential_is_bound_to_the_approver_and_their_account() -> None:
    org, user = await _account()
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension")
        await svc.approve(s, user_code=opened.user_code, approver_id=user, org_id=org)
    granted = await _redeem(opened.device_code)

    async with tenant_session(str(org), str(user)) as s:
        token = (
            await s.execute(
                select(AgentToken).where(AgentToken.assistant_id == granted.assistant_id)
            )
        ).scalar_one()
        assert token.user_id == user
        # Reaches every workspace its holder belongs to, decided per
        # request; not a wider grant, and asserted because the panel's
        # whole model depends on it.
        assert token.workspace_binding is WorkspaceBinding.account
        # Bounded, and well short of the machine-to-machine floor.
        assert token.expires_at is not None
        remaining = token.expires_at - datetime.datetime.now(tz=datetime.UTC)
        assert 89 <= remaining.days <= 90
        assert granted.expires_at == token.expires_at


async def test_the_row_records_what_the_collection_produced() -> None:
    org, user = await _account()
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension")
        await svc.approve(s, user_code=opened.user_code, approver_id=user, org_id=org)
    granted = await _redeem(opened.device_code)

    async with admin_session() as s:
        row = (
            await s.execute(
                select(DeviceAuthorization).where(DeviceAuthorization.user_code == opened.user_code)
            )
        ).scalar_one()
    assert row.answered_by_id == user
    assert row.approved_org_id == org
    assert row.assistant_id == granted.assistant_id
    assert row.redeemed_at is not None


async def test_waiting_is_reported_as_pending_not_as_success() -> None:
    """A 2xx here is the bug the sibling project shipped: every HTTP
    client reads it as "it worked" and stores an empty session."""
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension")
    with pytest.raises(AuthError) as err:
        await _redeem(opened.device_code)
    assert err.value.code is MessageCode.AUTH_DEVICE_PENDING


async def test_a_refusal_is_told_to_the_device_rather_than_timing_out() -> None:
    org, user = await _account()
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension")
        await svc.deny(s, user_code=opened.user_code, actor_id=user)
        row = (
            await s.execute(
                select(DeviceAuthorization).where(DeviceAuthorization.user_code == opened.user_code)
            )
        ).scalar_one()
        # Attributed: a refusal has an author too.
        assert row.answered_by_id == user

    with pytest.raises(AuthError) as err:
        await _redeem(opened.device_code)
    assert err.value.code is MessageCode.AUTH_DEVICE_DENIED
    assert await _assistant_count(org, user) == 0


async def test_an_unanswered_request_expires_and_says_so() -> None:
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension", ttl_seconds=-1)
    with pytest.raises(AuthError) as err:
        await _redeem(opened.device_code)
    assert err.value.code is MessageCode.AUTH_DEVICE_EXPIRED


async def test_an_approval_that_landed_before_expiry_is_still_collectable() -> None:
    """The clock must not throw away a decision somebody made. Expiry is
    checked after the answer, not before it."""
    org, user = await _account()
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension")
        await svc.approve(s, user_code=opened.user_code, approver_id=user, org_id=org)
        # Backdate the window so it has lapsed, keeping the approval.
        row = (
            await s.execute(
                select(DeviceAuthorization).where(DeviceAuthorization.user_code == opened.user_code)
            )
        ).scalar_one()
        row.expires_at = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(seconds=1)

    granted = await _redeem(opened.device_code)
    assert granted.secret.startswith("mycelium_at_")


async def test_a_device_code_collects_exactly_once() -> None:
    org, user = await _account()
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension")
        await svc.approve(s, user_code=opened.user_code, approver_id=user, org_id=org)

    await _redeem(opened.device_code)
    with pytest.raises(NotFoundError) as err:
        await _redeem(opened.device_code)
    assert err.value.code is MessageCode.AUTH_DEVICE_CODE_INVALID
    # A replay mints nothing the second time.
    assert await _assistant_count(org, user) == 1


async def test_an_answered_request_cannot_be_answered_again() -> None:
    org, user = await _account()
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension")
        await svc.approve(s, user_code=opened.user_code, approver_id=user, org_id=org)
        with pytest.raises(NotFoundError):
            await svc.approve(s, user_code=opened.user_code, approver_id=user, org_id=org)


async def test_an_unknown_short_code_is_absent_rather_than_described() -> None:
    async with admin_session() as s:
        with pytest.raises(NotFoundError) as err:
            await svc.find_open(s, user_code="AAAA-AAAA")
    assert err.value.code is MessageCode.AUTH_DEVICE_CODE_INVALID


async def test_the_short_code_is_matched_however_it_was_typed() -> None:
    async with admin_session() as s:
        opened = await svc.open_request(s, client="extension")
        bare = opened.user_code.replace("-", "").lower()
        spaced = f" {bare[:4]} {bare[4:]} "
        assert (await svc.find_open(s, user_code=bare)).user_code == opened.user_code
        assert (await svc.find_open(s, user_code=spaced)).user_code == opened.user_code


async def test_a_finished_request_releases_its_short_code() -> None:
    """The partial index is what allows this. A plain unique constraint
    would consume the space one request at a time."""
    org, user = await _account()
    async with admin_session() as s:
        first = await svc.open_request(s, client="extension")
        await svc.approve(s, user_code=first.user_code, approver_id=user, org_id=org)
        # Re-issuing the same code is now legal at the database level.
        row = DeviceAuthorization(
            device_code_hash="f" * 64,
            user_code=first.user_code,
            client="extension",
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(minutes=10),
        )
        s.add(row)
        await s.flush()
        # And it is the OPEN one that a lookup finds.
        assert (await svc.find_open(s, user_code=first.user_code)).id == row.id


async def test_sweep_removes_only_what_is_long_finished() -> None:
    async with admin_session() as s:
        recent = await svc.open_request(s, client="extension")
        stale = await svc.open_request(s, client="extension")
        row = (
            await s.execute(
                select(DeviceAuthorization).where(DeviceAuthorization.user_code == stale.user_code)
            )
        ).scalar_one()
        row.expires_at = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=30)
        await s.flush()

        removed = await svc.sweep_expired(s, older_than_days=7)
        assert removed == 1
        assert (await svc.find_open(s, user_code=recent.user_code)) is not None
