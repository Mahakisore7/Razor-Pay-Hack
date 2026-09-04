"""Route-level tests for `POST /webhooks/razorpay` (TR-1, TR-2, API-SPEC
SS2.1) -- signature verification and request wiring, with `store_raw_event`
mocked out so these run with no database. The dedup/no-op guarantee itself
(TR-3) needs a real Postgres and is covered in
`tests/integration/test_webhook_ingestion.py`.
"""

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from recoup.api.app import create_app
from recoup.platform.clock import FrozenClock, get_clock
from recoup.platform.config import Settings
from recoup.platform.db import get_session

_SECRET = "whsec_test_only"
_BODY = json.dumps({"event": "payment.captured", "payload": {}}).encode()


def _signature(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client() -> TestClient:
    app = create_app()

    async def _fake_session() -> AsyncIterator[None]:
        yield None

    app.dependency_overrides[get_session] = _fake_session
    app.dependency_overrides[get_clock] = lambda: FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    return TestClient(app)


def _settings_with_secret(secret: str | None) -> Settings:
    return Settings(razorpay_webhook_secret=SecretStr(secret) if secret is not None else None)


# --- signature verification -----------------------------------------------------


def test_valid_signature_is_accepted(client: TestClient) -> None:
    with (
        patch(
            "recoup.api.routes.webhooks.get_settings", return_value=_settings_with_secret(_SECRET)
        ),
        patch(
            "recoup.api.routes.webhooks.store_raw_event", new=AsyncMock(return_value=True)
        ) as stored,
    ):
        response = client.post(
            "/webhooks/razorpay",
            content=_BODY,
            headers={"x-razorpay-signature": _signature(_BODY)},
        )
    assert response.status_code == 200
    stored.assert_awaited_once()


def test_wrong_signature_is_rejected_with_no_detail_leaked(client: TestClient) -> None:
    with patch(
        "recoup.api.routes.webhooks.get_settings", return_value=_settings_with_secret(_SECRET)
    ):
        response = client.post(
            "/webhooks/razorpay",
            content=_BODY,
            headers={"x-razorpay-signature": _signature(_BODY, secret="wrong-secret")},
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid signature"


def test_missing_signature_header_is_rejected(client: TestClient) -> None:
    with patch(
        "recoup.api.routes.webhooks.get_settings", return_value=_settings_with_secret(_SECRET)
    ):
        response = client.post("/webhooks/razorpay", content=_BODY)
    assert response.status_code == 400


def test_unconfigured_secret_rejects_every_request_the_same_way(client: TestClient) -> None:
    """No webhook secret configured must fail closed, not fail open --
    and with the identical 400 a bad signature gets, not a distinguishable
    error that would tell a caller the server is misconfigured."""
    with patch("recoup.api.routes.webhooks.get_settings", return_value=_settings_with_secret(None)):
        response = client.post(
            "/webhooks/razorpay",
            content=_BODY,
            headers={"x-razorpay-signature": _signature(_BODY)},
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid signature"


def test_a_reused_signature_over_a_different_body_is_rejected(client: TestClient) -> None:
    """Constant-time comparison is exercised, not skipped: a signature valid
    for one body must not validate a different body."""
    with patch(
        "recoup.api.routes.webhooks.get_settings", return_value=_settings_with_secret(_SECRET)
    ):
        response = client.post(
            "/webhooks/razorpay",
            content=b'{"event": "payment.failed", "payload": {}}',
            headers={"x-razorpay-signature": _signature(_BODY)},
        )
    assert response.status_code == 400


# --- request wiring ---------------------------------------------------------------


def test_unrecognized_event_type_is_still_stored(client: TestClient) -> None:
    body = json.dumps({"event": "some.future.event", "payload": {}}).encode()
    with (
        patch(
            "recoup.api.routes.webhooks.get_settings", return_value=_settings_with_secret(_SECRET)
        ),
        patch(
            "recoup.api.routes.webhooks.store_raw_event", new=AsyncMock(return_value=True)
        ) as stored,
    ):
        response = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={"x-razorpay-signature": _signature(body)},
        )
    assert response.status_code == 200
    stored.assert_awaited_once()
    assert stored.await_args is not None
    assert stored.await_args.kwargs["event_type"] == "some.future.event"


def test_unparseable_body_is_still_stored_flagged(client: TestClient) -> None:
    body = b"{not valid json"
    with (
        patch(
            "recoup.api.routes.webhooks.get_settings", return_value=_settings_with_secret(_SECRET)
        ),
        patch(
            "recoup.api.routes.webhooks.store_raw_event", new=AsyncMock(return_value=True)
        ) as stored,
    ):
        response = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={"x-razorpay-signature": _signature(body)},
        )
    assert response.status_code == 200
    stored.assert_awaited_once()
    assert stored.await_args is not None
    assert stored.await_args.kwargs["event_type"] == "_unparseable"
    assert stored.await_args.kwargs["payload"]["_ingestion_error"] == "unparseable_json"


def test_a_replay_is_reported_as_a_duplicate_not_an_error(client: TestClient) -> None:
    with (
        patch(
            "recoup.api.routes.webhooks.get_settings", return_value=_settings_with_secret(_SECRET)
        ),
        patch("recoup.api.routes.webhooks.store_raw_event", new=AsyncMock(return_value=False)),
    ):
        response = client.post(
            "/webhooks/razorpay",
            content=_BODY,
            headers={"x-razorpay-signature": _signature(_BODY)},
        )
    assert response.status_code == 200
