"""Integration tests for `recoup.detection.pipeline` (T2.2) against a real,
migrated Postgres -- customer find-or-create, the already-detected
idempotency check, and `cases_open_dedup` are all database behaviours a
mocked session would test the mock of, not the constraint itself (the
same reasoning `test_webhook_ingestion.py` applies to `ON CONFLICT DO
NOTHING`).
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from recoup.detection.pipeline import (
    already_detected,
    open_case_for_signal,
    resolve_customer,
    run_detection,
)
from recoup.domain.case import CaseState
from recoup.domain.identifiers import CustomerRef, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.gateway.ingestion import RAZORPAY_SOURCE, store_raw_event
from recoup.platform.clock import FrozenClock
from recoup.platform.models import AuditEventRow, CaseRow, RawEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))


def _signal(customer_ref: CustomerRef, *, at_risk_paise: int, source_event_id: str) -> Signal:
    return Signal(
        id=SignalId(uuid7()),
        leak_class=LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
        customer=customer_ref,
        at_risk=Money(at_risk_paise, Currency.INR),
        detected_at=_CLOCK.now(),
        source_event_ids=(source_event_id,),
        decline=None,
        context=SignalContext(),
    )


async def _store_payment_failed_event(
    session: AsyncSession, *, provider_event_id: str, razorpay_customer_id: str, amount_paise: int
) -> RawEvent:
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{provider_event_id}",
                    "customer_id": razorpay_customer_id,
                    "amount": amount_paise,
                    "method": "upi",
                }
            }
        },
    }
    await store_raw_event(
        session,
        _CLOCK,
        source=RAZORPAY_SOURCE,
        event_type="payment.failed",
        provider_event_id=provider_event_id,
        payload=payload,
    )
    result = await session.execute(
        select(RawEvent).where(RawEvent.provider_event_id == provider_event_id)
    )
    return result.scalar_one()


# --- resolve_customer ---------------------------------------------------------------


async def test_resolve_customer_creates_a_new_customer(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        ref = await resolve_customer(session, "cust_new_1")
        await session.commit()
    assert ref.razorpay_customer_id == "cust_new_1"
    assert ref.contact_hash


async def test_resolve_customer_finds_an_existing_customer(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as first:
        first_ref = await resolve_customer(first, "cust_existing_1")
        await first.commit()
    async with sessionmaker() as second:
        second_ref = await resolve_customer(second, "cust_existing_1")
        await second.commit()
    assert first_ref.id == second_ref.id


# --- already_detected ----------------------------------------------------------------


async def test_already_detected_is_false_before_any_signal(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        assert await already_detected(session, "evt-never-seen") is False


async def test_already_detected_is_true_once_a_signal_references_the_event(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        customer = await resolve_customer(session, "cust_dup_check")
        signal = _signal(customer, at_risk_paise=100_000, source_event_id="evt-dup-check-1")
        case = await open_case_for_signal(session, _CLOCK, seed=1, signal=signal)
    assert case is not None

    async with sessionmaker() as check:
        assert await already_detected(check, "evt-dup-check-1") is True


# --- open_case_for_signal ------------------------------------------------------------


async def test_open_case_for_signal_creates_a_detected_case(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        customer = await resolve_customer(session, "cust_open_case_1")
        signal = _signal(customer, at_risk_paise=321_000, source_event_id="evt-open-case-1")
        case = await open_case_for_signal(session, _CLOCK, seed=42, signal=signal)

    assert case is not None
    assert case.state == CaseState.DETECTED
    assert case.at_risk.paise == 321_000
    assert case.cost_spent.paise == 0
    assert case.cost_ceiling.paise == 0


async def test_open_case_for_signal_writes_a_three_event_audit_chain(engine: AsyncEngine) -> None:
    """I4 (T2.9): case creation is three transitions -- the signal, the
    case, and its arm -- and each writes exactly one event, gapless and
    hash-chained, in the same transaction as the rows they describe."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        customer = await resolve_customer(session, "cust_open_case_audit")
        signal = _signal(customer, at_risk_paise=444_000, source_event_id="evt-open-case-audit")
        case = await open_case_for_signal(session, _CLOCK, seed=1, signal=signal)
    assert case is not None

    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditEventRow)
            .where(AuditEventRow.case_id == case.id)
            .order_by(AuditEventRow.seq)
        )
        rows = result.scalars().all()

    assert [row.kind for row in rows] == ["signal_detected", "case_opened", "arm_assigned"]
    assert [row.seq for row in rows] == [1, 2, 3]
    assert rows[0].prev_hash == ""
    assert rows[1].prev_hash == rows[0].hash
    assert rows[2].prev_hash == rows[1].hash
    assert {row.trace_id for row in rows} == {rows[0].trace_id}  # one trace for the whole creation


async def test_a_dedup_no_op_writes_no_audit_event(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as first_session:
        customer = await resolve_customer(first_session, "cust_dedup_no_audit")
        first_signal = _signal(customer, at_risk_paise=333_000, source_event_id="evt-dedup-audit-1")
        first_case = await open_case_for_signal(first_session, _CLOCK, seed=7, signal=first_signal)
    assert first_case is not None

    async with sessionmaker() as second_session:
        customer = await resolve_customer(second_session, "cust_dedup_no_audit")
        second_signal = _signal(
            customer, at_risk_paise=333_000, source_event_id="evt-dedup-audit-2"
        )
        second_case = await open_case_for_signal(
            second_session, _CLOCK, seed=7, signal=second_signal
        )
    assert second_case is None

    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditEventRow).where(AuditEventRow.case_id == first_case.id)
        )
        # Exactly the first case's own three events -- the rejected second
        # attempt created and rolled back nothing to have events at all.
        assert len(result.scalars().all()) == 3


async def test_arm_assignment_is_reproducible_across_two_separately_opened_cases(
    engine: AsyncEngine,
) -> None:
    """Regression test for a real bug T3.8's own reproducibility check
    found: `open_case_for_signal` used to hash arm assignment against the
    case's own freshly-`uuid7()`-generated id, which is real-wall-clock-
    derived and therefore different every call -- so two signals with
    the *same* source_event_ids (the stable identity `assign_arm` is
    meant to be reproducible against, per T3.2's own "arm = f(hash(seed
    | case_id))") could still roll different arms, at the same seed,
    purely because they were processed a few microseconds apart. Two
    different customers/amounts here (so TR-8 dedup does not swallow the
    second one) with the identical `source_event_ids` must land on the
    identical arm.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as first_session:
        first_customer = await resolve_customer(first_session, "cust_arm_repro_1")
        first_signal = _signal(
            first_customer, at_risk_paise=111_000, source_event_id="evt-arm-repro-shared"
        )
        first_case = await open_case_for_signal(first_session, _CLOCK, seed=99, signal=first_signal)
    assert first_case is not None

    async with sessionmaker() as second_session:
        second_customer = await resolve_customer(second_session, "cust_arm_repro_2")
        second_signal = _signal(
            second_customer, at_risk_paise=222_000, source_event_id="evt-arm-repro-shared"
        )
        second_case = await open_case_for_signal(
            second_session, _CLOCK, seed=99, signal=second_signal
        )
    assert second_case is not None

    assert first_case.arm == second_case.arm


async def test_open_case_for_signal_dedups_against_an_open_case_at_the_same_amount(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as first_session:
        customer = await resolve_customer(first_session, "cust_case_dedup_1")
        first_signal = _signal(customer, at_risk_paise=555_500, source_event_id="evt-dedup-1")
        first_case = await open_case_for_signal(first_session, _CLOCK, seed=7, signal=first_signal)
    assert first_case is not None

    async with sessionmaker() as second_session:
        customer = await resolve_customer(second_session, "cust_case_dedup_1")
        second_signal = _signal(customer, at_risk_paise=555_500, source_event_id="evt-dedup-2")
        second_case = await open_case_for_signal(
            second_session, _CLOCK, seed=7, signal=second_signal
        )
    assert second_case is None

    async with engine.connect() as conn:
        result = await conn.execute(
            select(CaseRow).where(CaseRow.customer_id == first_case.customer.id)
        )
        assert len(result.all()) == 1


# --- run_detection, end to end --------------------------------------------------------


async def test_run_detection_opens_a_case_from_a_stored_payment_failed_event(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        raw_event = await _store_payment_failed_event(
            session,
            provider_event_id="evt-run-detection-1",
            razorpay_customer_id="cust_run_detection_1",
            amount_paise=210_000,
        )
        case = await run_detection(session, _CLOCK, seed=3, raw_event=raw_event)

    assert case is not None
    assert case.at_risk.paise == 210_000
    assert case.state == CaseState.DETECTED


async def test_run_detection_is_a_no_op_the_second_time_over_the_same_raw_event(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        raw_event = await _store_payment_failed_event(
            session,
            provider_event_id="evt-run-detection-replay-1",
            razorpay_customer_id="cust_run_detection_replay_1",
            amount_paise=88_000,
        )
        first = await run_detection(session, _CLOCK, seed=3, raw_event=raw_event)
    assert first is not None

    async with sessionmaker() as session:
        second = await run_detection(session, _CLOCK, seed=3, raw_event=raw_event)
    assert second is None


async def test_run_detection_returns_none_for_an_event_with_no_customer_id(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        await store_raw_event(
            session,
            _CLOCK,
            source=RAZORPAY_SOURCE,
            event_type="payment.failed",
            provider_event_id="evt-no-customer-1",
            payload={"event": "payment.failed", "payload": {"payment": {"entity": {"amount": 1}}}},
        )
        result = await session.execute(
            select(RawEvent).where(RawEvent.provider_event_id == "evt-no-customer-1")
        )
        raw_event = result.scalar_one()
        case = await run_detection(session, _CLOCK, seed=3, raw_event=raw_event)

    assert case is None


async def test_run_detection_returns_none_for_a_payload_with_no_top_level_payload_key(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        await store_raw_event(
            session,
            _CLOCK,
            source=RAZORPAY_SOURCE,
            event_type="payment.failed",
            provider_event_id="evt-no-payload-key-1",
            payload={"event": "payment.failed"},
        )
        result = await session.execute(
            select(RawEvent).where(RawEvent.provider_event_id == "evt-no-payload-key-1")
        )
        raw_event = result.scalar_one()
        case = await run_detection(session, _CLOCK, seed=3, raw_event=raw_event)

    assert case is None


async def test_run_detection_returns_none_when_the_wrapper_has_no_entity(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        await store_raw_event(
            session,
            _CLOCK,
            source=RAZORPAY_SOURCE,
            event_type="payment.failed",
            provider_event_id="evt-no-entity-1",
            payload={"event": "payment.failed", "payload": {"payment": {}}},
        )
        result = await session.execute(
            select(RawEvent).where(RawEvent.provider_event_id == "evt-no-entity-1")
        )
        raw_event = result.scalar_one()
        case = await run_detection(session, _CLOCK, seed=3, raw_event=raw_event)

    assert case is None


async def test_run_detection_returns_none_for_an_event_no_detector_recognizes(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        await store_raw_event(
            session,
            _CLOCK,
            source=RAZORPAY_SOURCE,
            event_type="payment.captured",
            provider_event_id="evt-uninteresting-1",
            payload={
                "event": "payment.captured",
                "payload": {"payment": {"entity": {"customer_id": "cust_x", "amount": 1000}}},
            },
        )
        result = await session.execute(
            select(RawEvent).where(RawEvent.provider_event_id == "evt-uninteresting-1")
        )
        raw_event = result.scalar_one()
        case = await run_detection(session, _CLOCK, seed=3, raw_event=raw_event)

    assert case is None
