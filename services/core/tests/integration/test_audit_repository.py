"""Integration tests for `recoup.audit.repository.record_event` (T2.9)
against a real Postgres -- sequencing, hash-chaining across calls, PII
masking, and the concurrency fix `record_event`'s own docstring
describes (locking `cases`, not the chain's own tail row, after the
first version of this raced under
`test_outbox.py::test_concurrent_workers_never_double_claim`).
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from recoup.audit.events import Actor, ActorKind, AuditEvent, AuditKind, compute_hash
from recoup.audit.repository import record_event
from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.domain.identifiers import AuditEventId, CaseId, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.platform.clock import FrozenClock
from recoup.platform.models import AuditEventRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))


async def _seed_case(
    sessionmaker: async_sessionmaker[AsyncSession], razorpay_customer_id: str
) -> CaseId:
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


async def _events(
    sessionmaker: async_sessionmaker[AsyncSession], case_id: CaseId
) -> list[AuditEventRow]:
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditEventRow)
            .where(AuditEventRow.case_id == case_id)
            .order_by(AuditEventRow.seq)
        )
        return list(result.scalars().all())


# --- sequencing and hash-chaining ----------------------------------------------------


async def test_a_cases_first_event_is_seq_one_with_an_empty_prev_hash(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_audit_seq_one")

    rows = await _events(sessionmaker, case_id)

    # open_case_for_signal itself already wrote signal_detected/case_opened/arm_assigned.
    assert [row.seq for row in rows] == [1, 2, 3]
    assert rows[0].kind == "signal_detected"
    assert rows[0].prev_hash == ""


async def test_a_second_call_appends_with_the_priors_hash(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_audit_seq_two")

    async with sessionmaker() as session:
        await record_event(
            session,
            case_id=case_id,
            kind=AuditKind.CASE_RESOLVED,
            payload={"kind": "recovered"},
            actor=Actor.system(),
            trace_id="trace-1",
            occurred_at=_CLOCK.now(),
        )
        await session.commit()

    rows = await _events(sessionmaker, case_id)
    assert [row.seq for row in rows] == [1, 2, 3, 4]
    assert rows[3].kind == "case_resolved"
    assert rows[3].prev_hash == rows[2].hash


async def test_the_stored_hash_matches_a_fresh_recomputation(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_audit_hash_check")
    rows = await _events(sessionmaker, case_id)

    for row in rows:
        reconstructed = AuditEvent(
            id=AuditEventId(row.id),
            case_id=CaseId(row.case_id),
            seq=row.seq,
            kind=AuditKind(row.kind),
            payload=row.payload,
            actor=Actor(ActorKind(row.actor_type), row.actor_id),
            trace_id=row.trace_id,
            occurred_at=row.occurred_at,
            prev_hash=row.prev_hash,
        )
        assert compute_hash(reconstructed) == row.hash


# --- PII masking (DOMAIN-MODEL SS10) --------------------------------------------------


async def test_a_pii_keyed_payload_field_is_redacted_before_it_reaches_the_row(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_audit_pii")

    async with sessionmaker() as session:
        await record_event(
            session,
            case_id=case_id,
            kind=AuditKind.MESSAGE_VALIDATED,
            payload={"customer_phone": "+919876543210", "channel": "sms"},
            actor=Actor.system(),
            trace_id="trace-pii",
            occurred_at=_CLOCK.now(),
        )
        await session.commit()

    rows = await _events(sessionmaker, case_id)
    written = rows[-1]
    assert written.payload["customer_phone"] == "***REDACTED***"
    assert written.payload["channel"] == "sms"  # non-PII-keyed fields pass through


# --- trace id propagation --------------------------------------------------------------


async def test_the_supplied_trace_id_is_stored_verbatim(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_audit_trace")

    async with sessionmaker() as session:
        await record_event(
            session,
            case_id=case_id,
            kind=AuditKind.CASE_RESOLVED,
            payload={},
            actor=Actor.system(),
            trace_id="deadbeef00000000deadbeef00000000",
            occurred_at=_CLOCK.now(),
        )
        await session.commit()

    rows = await _events(sessionmaker, case_id)
    assert rows[-1].trace_id == "deadbeef00000000deadbeef00000000"


# --- concurrency (the bug this module's docstring documents) -------------------------


async def test_concurrent_appends_to_the_same_case_never_collide_on_seq(
    migrated_database_url: str,
) -> None:
    concurrent_engine = create_async_engine(migrated_database_url, pool_size=12, max_overflow=0)
    try:
        sessionmaker = async_sessionmaker(concurrent_engine, expire_on_commit=False)
        case_id = await _seed_case(sessionmaker, "cust_audit_concurrency")

        async def _append(i: int) -> None:
            async with sessionmaker() as session:
                await record_event(
                    session,
                    case_id=case_id,
                    kind=AuditKind.CASE_RESOLVED,
                    payload={"i": i},
                    actor=Actor.system(),
                    trace_id=f"trace-{i}",
                    occurred_at=_CLOCK.now(),
                )
                await session.commit()

        await asyncio.gather(*(_append(i) for i in range(10)))

        rows = await _events(sessionmaker, case_id)
        # 3 from case creation + 10 concurrent appends, gapless and unique.
        assert [row.seq for row in rows] == list(range(1, 14))
    finally:
        await concurrent_engine.dispose()
