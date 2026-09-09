"""Liveness and readiness must answer different questions.

The property under test is the separation itself, not either endpoint's
happy path. Both probes pointed at ``/healthz`` before this, and
``/healthz`` returns a constant, so a database outage left the pod
advertising itself as able to serve while every real request failed.

The asymmetry asserted here is the whole point: with the database
unreachable, ``/readyz`` must fail and ``/healthz`` must NOT. If a later
change makes liveness consult a dependency, the pod starts being killed
for faults a restart cannot fix, and that is what turns a partial outage
into a crash loop.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mycelium_api.app import create_app


@pytest.fixture
def unreachable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the readiness check's only dependency fail.

    Patched at ``readiness.get_engine`` rather than by pointing the
    settings at a dead host: a bad DSN would take a connect timeout to
    fail, and the point here is the verdict, not how long it takes to
    reach it.
    """

    def _no_database() -> object:
        raise OSError("connection refused")

    monkeypatch.setattr("mycelium_core.readiness.get_engine", _no_database)


def test_readyz_fails_when_the_database_is_unreachable(unreachable_database: None) -> None:
    client = TestClient(create_app())
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not-ready"
    # The dependency class, so an operator knows where to look. Never the
    # driver's message, which would put connection detail on an
    # unauthenticated endpoint.
    assert body["dependency"] == "database"
    assert "connection refused" not in response.text


def test_healthz_still_answers_when_the_database_is_unreachable(
    unreachable_database: None,
) -> None:
    """Liveness is a process check. A dependency being down is not a
    reason to restart the process, and this is the assertion that keeps
    the two probes from being merged back together."""
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_answers_ready_against_a_live_database() -> None:
    client = TestClient(create_app())
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readyz_needs_no_credential() -> None:
    """The kubelet has none to present. Asserted rather than assumed,
    because the route-scope gate refuses anything not in its allowlist
    and a probe that starts returning 401 fails the pod silently."""
    client = TestClient(create_app())
    assert client.get("/readyz").status_code in (200, 503)
