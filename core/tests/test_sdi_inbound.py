"""SdI inbound notification ingest (F7b): namespace-agnostic parsing and the
cross-org correlation (resolve the tenant by IdentificativoSdI with no tenant
context, via the SECURITY DEFINER resolver, then apply the outcome).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.invoice import BuyerVerdict, ConservationStatus, InvoiceState, SdiStatus
from mycelium_core.models.sdi_notification import InvoiceNotification
from mycelium_core.sdi_channel import IntermediaryIdentity, TransmitResult, set_channel_override
from mycelium_core.services import invoice as inv
from mycelium_core.services import sdi_mandate as mandate
from mycelium_core.services.auth import signup
from mycelium_core.services.sdi_inbound import ingest_notification, parse_notification
from mycelium_core.services.sdi_notification_xsd import NS_MESSAGGI
from mycelium_core.services.taxonomy import ClientInput, create_client

# v1 notification fixtures are XSD-valid against MessaggiTypes_v1.1: SdI
# rejects anything else, and so do we. The root carries the official messaggi
# namespace; children are unqualified (the schema is elementFormDefault
# unqualified). ``ds:Signature`` is omitted on purpose -- the validator is
# signature-relaxed (XAdES verification is a separate, post-v1 concern).


def _rc(
    ident: str,
    *,
    nome_file: str = "IT01234567890_00001.xml",
    message_id: str = "MID00001",
) -> bytes:
    return (
        f'<m:RicevutaConsegna xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>{nome_file}</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<DataOraConsegna>2026-05-25T10:01:00</DataOraConsegna>"
        f"<Destinatario><Codice>ABCDEFG</Codice><Descrizione>Acme</Descrizione></Destinatario>"
        f"<MessageId>{message_id}</MessageId>"
        f"</m:RicevutaConsegna>"
    ).encode()


def _ns(ident: str) -> bytes:
    return (
        f'<m:NotificaScarto xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>IT01234567890_00001.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<ListaErrori><Errore><Codice>00001</Codice>"
        f"<Descrizione>boom</Descrizione></Errore></ListaErrori>"
        f"<MessageId>MID00002</MessageId>"
        f"</m:NotificaScarto>"
    ).encode()


def _ne(ident: str, esito: str, *, message_id: str = "MID00NE1") -> bytes:
    return (
        f'<m:NotificaEsito xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>IT01234567890_00001.xml</NomeFile>"
        f'<EsitoCommittente versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<Esito>{esito}</Esito>"
        f"</EsitoCommittente>"
        f"<MessageId>{message_id}</MessageId>"
        f"</m:NotificaEsito>"
    ).encode()


def _dt(ident: str, *, message_id: str = "MID00DT1") -> bytes:
    return (
        f'<m:NotificaDecorrenzaTermini xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>IT01234567890_00001.xml</NomeFile>"
        f"<MessageId>{message_id}</MessageId>"
        f"</m:NotificaDecorrenzaTermini>"
    ).encode()


def test_parse_ricevuta_consegna() -> None:
    parsed = parse_notification(_rc("100000000001"))
    assert parsed.outcome == "RC"
    assert parsed.identificativo_sdi == "100000000001"
    assert parsed.message_id == "MID00001"
    assert parsed.esito is None


def test_parse_notifica_scarto() -> None:
    parsed = parse_notification(_ns("100000000002"))
    assert parsed.outcome == "NS"
    assert parsed.identificativo_sdi == "100000000002"


def test_parse_rejects_lax_namespace() -> None:
    # Bare XML without the official messaggi namespace is rejected: SdI never
    # emits a notification without the canonical namespace; tolerating that
    # would mask a real protocol bug.
    bare = b"<RicevutaConsegna><IdentificativoSdI>1</IdentificativoSdI></RicevutaConsegna>"
    with pytest.raises(ValueError, match="MessaggiTypes"):
        parse_notification(bare)


def test_parse_rejects_unknown_root() -> None:
    foo = f'<m:Foo xmlns:m="{NS_MESSAGGI}"/>'.encode()
    with pytest.raises(ValueError, match="MessaggiTypes"):
        parse_notification(foo)


def test_parse_rejects_missing_required_field() -> None:
    # A RicevutaConsegna missing DataOraConsegna is structurally invalid; the
    # XSD gate must reject it before XPath extraction.
    incomplete = (
        f'<m:RicevutaConsegna xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>1</IdentificativoSdI>"
        f"<NomeFile>x.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<Destinatario><Codice>ABCDEFG</Codice><Descrizione>A</Descrizione></Destinatario>"
        f"<MessageId>M</MessageId>"
        f"</m:RicevutaConsegna>"
    ).encode()
    with pytest.raises(ValueError, match="MessaggiTypes"):
        parse_notification(incomplete)


def test_inbound_app_rejects_malformed_xml_with_400() -> None:
    # ADR-0011: SdI push must never see a 500 (its retry/log path gets
    # noisy). Both an empty body and arbitrary non-XML must surface as 400,
    # not bubble lxml.XMLSyntaxError into a 500.
    from fastapi.testclient import TestClient

    from mycelium_sdi_inbound.app import create_app

    client = TestClient(create_app())
    assert client.post("/sdi/notification", content=b"").status_code == 400
    assert client.post("/sdi/notification", content=b"HELLO").status_code == 400


def test_inbound_app_separates_liveness_from_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    """This service writes every delivery it accepts, so a pod that
    cannot reach the database must fall out of rotation rather than
    acknowledge an invoice it will not record. Readiness says so;
    liveness must not, because restarting fixes no database.

    The API asserts the same separation over its own app
    (``api/tests/test_probes_do_not_share_a_check.py``). Repeated here
    because these are two FastAPI apps and the route registration, not
    the shared check, is what a change would break.
    """
    from fastapi.testclient import TestClient

    from mycelium_sdi_inbound.app import create_app

    client = TestClient(create_app())
    assert client.get("/readyz").json() == {"status": "ready"}

    def _no_database() -> object:
        raise OSError("connection refused")

    monkeypatch.setattr("mycelium_core.readiness.get_engine", _no_database)
    not_ready = client.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json()["dependency"] == "database"
    # Liveness is unmoved by the same outage.
    assert client.get("/healthz").status_code == 200


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="INB",
        )
    return r.org_id, r.user_id


@pytest.fixture
def _coop() -> Iterator[None]:
    class FakeCoop:
        name = "sdicoop"

        @property
        def intermediary(self) -> IntermediaryIdentity | None:
            return IntermediaryIdentity(country_code="IT", fiscal_code="11122233344")

        async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> TransmitResult:
            # IdentificativoSdI is xsd:integer with up to 12 digits per the
            # official schema; derive a unique numeric id from the invoice
            # UUID so concurrent/repeated test runs never collide.
            numeric = int(invoice_id.replace("-", "")[:11], 16) % 10**12
            return TransmitResult(
                identificativo_sdi=str(numeric),
                conservation=ConservationStatus.ade_pending,
                channel=self.name,
            )

    set_channel_override(FakeCoop)
    try:
        yield
    finally:
        set_channel_override(None)


async def test_inbound_ingest_correlates_cross_org_and_marks_delivered(_coop: None) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        issuer = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="P",
            legal_name="Acme Srl",
            vat_number="01234567890",
            address="Via Roma 1",
            postal_code="00100",
            city="Roma",
            is_default=True,
        )
        client = await create_client(
            s,
            org_id=org,
            actor_id=user,
            name="C",
            profile=ClientInput(
                legal_name="Client SpA",
                country_code="IT",
                vat_number="09876543210",
                sdi_code="ABCDEFG",
                address="Via Milano 2",
                postal_code="20100",
                city="Milano",
            ),
        )
        await mandate.grant_mandate(s, org_id=org, actor_id=user, issuer_profile_id=issuer.id)
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client.id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="svc",
            unit_price=Decimal(100),
        )
        tx = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        identificativo = tx.identificativo_sdi
    assert identificativo is not None

    # Ingest a delivery receipt with NO tenant context: the service must
    # resolve the org cross-org by IdentificativoSdI, then apply the outcome.
    updated = await ingest_notification(_rc(identificativo))
    assert updated is not None
    assert updated.sdi_status is SdiStatus.RC
    assert updated.state is InvoiceState.delivered
    assert updated.conservation_status is ConservationStatus.ade_covered

    # A notification matching NOTHING (unknown IdentificativoSdI AND unknown
    # NomeFile -- since ADR-0046 a known file name is a legitimate lost-ACK
    # correlation key) yields None (SdI may retry; never a 500).
    assert (
        await ingest_notification(_rc("999999999999", nome_file="IT99999999999_ZZZZZ.xml")) is None
    )


async def test_inbound_lost_ack_reconciles_by_nome_file(_coop: None) -> None:
    """ADR-0046 end-to-end reconcile: the dispatch ACK was lost (invoice
    parked transmitted/ident-less), then the filing's RC arrives with an
    identifier we never saw -- the NomeFile fallback resolves the org AND
    the invoice, which adopts the identifier and settles."""
    import httpx

    from mycelium_core.errors import ConflictError
    from mycelium_core.sdi_channel import IntermediaryIdentity as _II
    from mycelium_core.sdi_channel import TransmitResult as _TR

    class LostAckCoop:
        name = "sdicoop"

        @property
        def intermediary(self) -> _II | None:
            return _II(country_code="IT", fiscal_code="11122233344")

        async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> _TR:
            raise httpx.ReadTimeout("lost ack")

    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        issuer = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="P",
            legal_name="Acme Srl",
            vat_number="01234567890",
            address="Via Roma 1",
            postal_code="00100",
            city="Roma",
            is_default=True,
        )
        client = await create_client(
            s,
            org_id=org,
            actor_id=user,
            name="C",
            profile=ClientInput(
                legal_name="Client SpA",
                country_code="IT",
                vat_number="09876543210",
                sdi_code="ABCDEFG",
                address="Via Milano 2",
                postal_code="20100",
                city="Milano",
            ),
        )
        await mandate.grant_mandate(s, org_id=org, actor_id=user, issuer_profile_id=issuer.id)
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client.id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="svc",
            unit_price=Decimal(100),
        )
        invoice_id = d.id
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(
                s, org_id=org, actor_id=user, invoice_id=invoice_id, channel=LostAckCoop()
            )
    async with tenant_session(str(org), str(user)) as s:
        parked = await inv.get_invoice(s, org_id=org, invoice_id=invoice_id)
        nome_file = parked.nome_file
        assert parked.identificativo_sdi is None and nome_file is not None

    # No tenant context, unknown identifier: only the NomeFile can correlate.
    # Ident + MessageId are derived per run: the suite shares a dirty DB, so a
    # fixed identifier would collide with a previous run's adopted value.
    fresh_ident = str(int(uuid.uuid4().hex[:11], 16) % 10**12)
    updated = await ingest_notification(
        _rc(fresh_ident, nome_file=nome_file, message_id=f"M{uuid.uuid4().hex[:8]}")
    )
    assert updated is not None
    assert updated.identificativo_sdi == fresh_ident
    assert updated.state is InvoiceState.delivered
    assert updated.sdi_status is SdiStatus.RC


async def _setup_transmitted_invoice(coop: None) -> str:
    """Spin up an org + transmitted invoice and return its IdentificativoSdI.
    Used by the NE/DT integration tests below."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        issuer = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="P",
            legal_name="Acme Srl",
            vat_number="01234567890",
            address="Via Roma 1",
            postal_code="00100",
            city="Roma",
            is_default=True,
        )
        client = await create_client(
            s,
            org_id=org,
            actor_id=user,
            name="C",
            profile=ClientInput(
                legal_name="Client SpA",
                country_code="IT",
                vat_number="09876543210",
                sdi_code="ABCDEFG",
                address="Via Milano 2",
                postal_code="20100",
                city="Milano",
            ),
        )
        await mandate.grant_mandate(s, org_id=org, actor_id=user, issuer_profile_id=issuer.id)
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client.id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="svc",
            unit_price=Decimal(100),
        )
        tx = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
    assert tx.identificativo_sdi is not None
    return tx.identificativo_sdi


async def test_ne_ec01_marks_accepted(_coop: None) -> None:
    # Buyer accepts via EsitoCommittente EC01 -> verdict=accepted, state=accepted.
    ident = await _setup_transmitted_invoice(_coop)
    await ingest_notification(_rc(ident))  # delivery first
    updated = await ingest_notification(_ne(ident, "EC01"))
    assert updated is not None
    assert updated.sdi_status is SdiStatus.NE
    assert updated.buyer_verdict is BuyerVerdict.accepted
    assert updated.state is InvoiceState.accepted
    assert updated.buyer_verdict_at is not None


async def test_ne_ec02_marks_rejected(_coop: None) -> None:
    ident = await _setup_transmitted_invoice(_coop)
    await ingest_notification(_rc(ident))
    updated = await ingest_notification(_ne(ident, "EC02"))
    assert updated is not None
    assert updated.buyer_verdict is BuyerVerdict.rejected
    assert updated.state is InvoiceState.rejected


async def test_dt_marks_deemed_accepted_when_no_prior_ne(_coop: None) -> None:
    # 15-day window expired without a buyer NE -> deemed acceptance.
    ident = await _setup_transmitted_invoice(_coop)
    await ingest_notification(_rc(ident))
    updated = await ingest_notification(_dt(ident))
    assert updated is not None
    assert updated.sdi_status is SdiStatus.DT
    assert updated.buyer_verdict is BuyerVerdict.deemed_accepted
    assert updated.state is InvoiceState.accepted
    assert updated.dt_received_at is not None


async def test_dt_does_not_clobber_prior_ne_rejection(_coop: None) -> None:
    # NE (explicit verdict) must win over a later DT (timeout). Only sdi_status
    # and dt_received_at change; the verdict stays as 'rejected'.
    ident = await _setup_transmitted_invoice(_coop)
    await ingest_notification(_rc(ident))
    await ingest_notification(_ne(ident, "EC02"))
    updated = await ingest_notification(_dt(ident))
    assert updated is not None
    assert updated.sdi_status is SdiStatus.DT
    assert updated.dt_received_at is not None
    assert updated.buyer_verdict is BuyerVerdict.rejected
    assert updated.state is InvoiceState.rejected


async def test_audit_log_appends_one_row_per_notification(_coop: None) -> None:
    # Every ingest writes an invoice_notifications row; a SdI retry with the
    # same message_id is swallowed by the dedupe unique index.
    ident = await _setup_transmitted_invoice(_coop)
    await ingest_notification(_rc(ident))
    await ingest_notification(_ne(ident, "EC01"))
    # Retry of the NE with the same MessageId: idempotent (no second row).
    await ingest_notification(_ne(ident, "EC01"))

    org_id = await _resolve_org_for(ident)
    async with tenant_session(str(org_id), "00000000-0000-0000-0000-000000000000") as s:
        stmt = select(InvoiceNotification.kind).order_by(InvoiceNotification.received_at)
        rows = (await s.execute(stmt)).scalars().all()
    assert rows == ["RC", "NE"]


async def _resolve_org_for(identificativo: str) -> uuid.UUID:
    # Helper that mirrors sdi_inbound._resolve_org; exposed here so the audit
    # assertion can open a tenant_session without leaking a private symbol.
    from sqlalchemy import text as _text

    async with admin_session() as s:
        val = (
            await s.execute(
                _text("SELECT sdi_resolve_invoice_org(:ident)"), {"ident": identificativo}
            )
        ).scalar()
    assert val is not None
    return uuid.UUID(str(val))
