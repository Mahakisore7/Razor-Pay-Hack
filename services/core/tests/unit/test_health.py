"""/health/live and /health/ready are distinct (TR-71): liveness never touches a
dependency, readiness checks everything traffic would need.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from recoup.api.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_live_never_touches_a_dependency(client: TestClient) -> None:
    with (
        patch("recoup.platform.db.ping", side_effect=AssertionError("db touched")),
        patch("recoup.platform.cache.ping", side_effect=AssertionError("redis touched")),
    ):
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_ok_when_dependencies_are_up(client: TestClient) -> None:
    with (
        patch("recoup.api.routes.health.db.ping", new=AsyncMock(return_value=True)),
        patch("recoup.api.routes.health.cache.ping", new=AsyncMock(return_value=True)),
    ):
        response = client.get("/health/ready")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert {c["name"]: c["ok"] for c in body["checks"]} == {"database": True, "redis": True}


def test_ready_degrades_without_500_when_a_dependency_is_down(client: TestClient) -> None:
    """A dependency outage must be visible, not a 500 -- readiness must always answer."""
    with (
        patch(
            "recoup.api.routes.health.db.ping",
            new=AsyncMock(side_effect=ConnectionError("no route to host")),
        ),
        patch("recoup.api.routes.health.cache.ping", new=AsyncMock(return_value=True)),
    ):
        response = client.get("/health/ready")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "degraded"
    db_check = next(c for c in body["checks"] if c["name"] == "database")
    assert db_check["ok"] is False
    assert "no route to host" in db_check["detail"]


def test_metrics_endpoint_is_prometheus_text_format(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
