"""Integration tests for webhook ingestion (T2.1) against a real, migrated
Postgres -- `ON CONFLICT DO NOTHING` is a database behaviour; a mocked
session would test the mock, not TR-3's replay-is-a-no-op guarantee.
"""

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from recoup.api.app import create_app
from recoup.gateway.ingestion import RAZORPAY_SOURCE, store_raw_event
from recoup.platform.clock import FrozenClock, get_clock
from recoup.platform.config import Settings
from recoup.platform.db import get_session
from recoup.platform.models import RawEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_SECRET = "whsec_test_only"


def _signature(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest_asyncio.fixture(loop_scope="module")
async def client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """`httpx.AsyncClient` over `ASGITransport`, not Starlette's
    `TestClient` -- `TestClient` runs the app through a blocking portal on
    its own thread with its own event loop, and asyncpg connections are
    loop-bound. A request through that portal against an engine created on
    *this* fixture's (module) loop fails cross-loop -- the same class of
    bug as nesting `asyncio.run()` inside a running loop (see F-005 in
    FAILURE-LOG), just one layer further from the obvious call site.
    """
    app = create_app()
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_clock] = lambda: FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


async def _row_count(engine: AsyncEngine, provider_event_id: str) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(RawEvent).where(RawEvent.provider_event_id == provider_event_id)
        )
        return len(result.all())


# --- store_raw_event, direct (TR-3) -----------------------------------------------


async def test_store_raw_event_inserts_a_new_row(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    async with sessionmaker() as session:
        inserted = await store_raw_event(
            session,
            clock,
            source=RAZORPAY_SOURCE,
            event_type="payment.captured",
            provider_event_id="evt-unique-1",
            payload={"event": "payment.captured"},
        )
    assert inserted is True
    assert await _row_count(engine, "evt-unique-1") == 1


async def test_store_raw_event_replay_is_a_no_op(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    payload = {"event": "payment.captured"}

    async with sessionmaker() as first:
        first_result = await store_raw_event(
            first,
            clock,
            source=RAZORPAY_SOURCE,
            event_type="payment.captured",
            provider_event_id="evt-replay-1",
            payload=payload,
        )
    async with sessionmaker() as second:
        second_result = await store_raw_event(
            second,
            clock,
            source=RAZORPAY_SOURCE,
            event_type="payment.captured",
            provider_event_id="evt-replay-1",
            payload=payload,
        )

    assert first_result is True
    assert second_result is False
    assert await _row_count(engine, "evt-replay-1") == 1


async def test_store_raw_event_persists_the_normalized_decline_category(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {"entity": {"error_reason": "payment_failed_due_to_insufficient_funds"}}
        },
    }
    async with sessionmaker() as session:
        await store_raw_event(
            session,
            clock,
            source=RAZORPAY_SOURCE,
            event_type="payment.failed",
            provider_event_id="evt-decline-1",
            payload=payload,
        )

    async with engine.connect() as conn:
        result = await conn.execute(
            select(RawEvent.decline_category).where(RawEvent.provider_event_id == "evt-decline-1")
        )
        assert result.scalar_one() == "INSUFFICIENT_FUNDS"


# --- the full HTTP route, against a real database ---------------------------------


async def test_http_replay_is_a_no_op_and_both_requests_return_200(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    body = json.dumps({"event": "order.paid", "payload": {}}).encode()
    headers = {"x-razorpay-signature": _signature(body)}
    settings = Settings(razorpay_webhook_secret=SecretStr(_SECRET))

    with patch("recoup.api.routes.webhooks.get_settings", return_value=settings):
        first = await client.post("/webhooks/razorpay", content=body, headers=headers)
        second = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200

    provider_event_id = hashlib.sha256(body).hexdigest()
    assert await _row_count(engine, provider_event_id) == 1


async def test_http_invalid_signature_never_reaches_storage(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    body = json.dumps({"event": "payment.captured", "payload": {}}).encode()
    settings = Settings(razorpay_webhook_secret=SecretStr(_SECRET))

    with patch("recoup.api.routes.webhooks.get_settings", return_value=settings):
        response = await client.post(
            "/webhooks/razorpay",
            content=body,
            headers={"x-razorpay-signature": _signature(body, secret="wrong")},
        )

    assert response.status_code == 400
    provider_event_id = hashlib.sha256(body).hexdigest()
    assert await _row_count(engine, provider_event_id) == 0
