"""`recoup audit verify --case <id>` (T2.9) against a real, migrated
Postgres. Sync tests, not `async def` -- the command drives its own
`asyncio.run()` internally (see `cli.py`), and calling that from inside
a test coroutine already running on pytest-asyncio's loop would nest
event loops (this codebase's F-005); `test_cli_import_events.py`'s
docstring covers the same reasoning for the same shape of command.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from recoup.cli import app
from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.domain.identifiers import SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.platform.clock import FrozenClock
from recoup.platform.config import Settings
from recoup.platform.models import AuditEventRow

pytestmark = pytest.mark.integration

runner = CliRunner()
_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))


def _settings(migrated_database_url: str) -> Settings:
    return Settings(database_url=migrated_database_url, razorpay_webhook_secret=SecretStr("unused"))


async def _seed_case(migrated_database_url: str, razorpay_customer_id: str) -> uuid.UUID:
    engine = create_async_engine(migrated_database_url)
    try:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            customer = await resolve_customer(session, razorpay_customer_id)
            signal = Signal(
                id=SignalId(uuid7()),
                leak_class=LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
                customer=customer,
                at_risk=Money(500_000, Currency.INR),
                detected_at=_CLOCK.now(),
                source_event_ids=(f"evt-{uuid.uuid4()}",),
                decline=None,
                context=SignalContext(),
            )
            case = await open_case_for_signal(session, _CLOCK, seed=1, signal=signal)
        assert case is not None
        return case.id
    finally:
        await engine.dispose()


async def _insert_diverged_event(migrated_database_url: str, case_id: uuid.UUID) -> None:
    """`audit_events` forbids `UPDATE`/`DELETE` (DATA-MODEL SS3.1), so a
    corrupted chain can't be simulated by editing an existing row -- this
    instead `INSERT`s a fourth event with a `prev_hash` that doesn't
    match seq 3's actual hash, the same shape genuine tampering (or a
    lost write) would leave behind for `verify_chain` to catch.
    """
    engine = create_async_engine(migrated_database_url)
    try:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            session.add(
                AuditEventRow(
                    id=uuid.uuid4(),
                    case_id=case_id,
                    seq=4,
                    kind="case_resolved",
                    payload={},
                    actor_type="system",
                    actor_id=None,
                    trace_id="trace-tamper",
                    occurred_at=_CLOCK.now(),
                    prev_hash="0" * 64,  # does not match seq 3's real hash
                    hash="1" * 64,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


def test_audit_verify_reports_an_intact_chain(migrated_database_url: str) -> None:
    case_id = asyncio.run(_seed_case(migrated_database_url, "cust_cli_verify_intact"))
    settings = _settings(migrated_database_url)

    with patch("recoup.platform.config.get_settings", return_value=settings):
        result = runner.invoke(app, ["audit", "verify", "--case", str(case_id)])

    assert result.exit_code == 0, result.stdout
    assert "chain intact" in result.stdout
    assert "3 event" in result.stdout


def test_audit_verify_reports_the_first_divergence(migrated_database_url: str) -> None:
    case_id = asyncio.run(_seed_case(migrated_database_url, "cust_cli_verify_diverged"))
    asyncio.run(_insert_diverged_event(migrated_database_url, case_id))
    settings = _settings(migrated_database_url)

    with patch("recoup.platform.config.get_settings", return_value=settings):
        result = runner.invoke(app, ["audit", "verify", "--case", str(case_id)])

    assert result.exit_code == 1
    assert "diverges at seq 4" in result.stdout


def test_audit_verify_exits_non_zero_for_a_case_with_no_events(migrated_database_url: str) -> None:
    settings = _settings(migrated_database_url)
    missing_case_id = uuid.uuid4()

    with patch("recoup.platform.config.get_settings", return_value=settings):
        result = runner.invoke(app, ["audit", "verify", "--case", str(missing_case_id)])

    assert result.exit_code == 1
    assert "no audit events found" in result.stdout


def test_audit_verify_rejects_a_malformed_case_id() -> None:
    result = runner.invoke(app, ["audit", "verify", "--case", "not-a-uuid"])

    assert result.exit_code == 1
    assert "not a valid case id" in result.stdout
