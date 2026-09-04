"""Integration tests for `recoup.bench.holdout.persist_holdout` (T3.4,
DOMAIN-MODEL I3/I7) against a real Postgres: a control-arm case reaches
`HOLDOUT` with zero `PlanRow`/`ActionRow`/`ScheduledActionRow` rows, and
attribution's pre-existing TR-30 handling (T2.8) picks it up correctly
once it does.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from recoup.attribution.engine import attribute_payment
from recoup.bench.holdout import persist_holdout
from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.diagnosis.engine import stub_diagnose
from recoup.domain.case import Arm, Case, CaseState, IllegalTransition
from recoup.domain.decline import DeclineCategory
from recoup.domain.diagnosis import Diagnosis
from recoup.domain.identifiers import SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.outcome import OutcomeKind
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.gateway.interface import Payment as GatewayPayment
from recoup.gateway.interface import PaymentStatus
from recoup.platform.clock import FrozenClock
from recoup.platform.models import (
    ActionRow,
    AuditEventRow,
    CaseRow,
    OutcomeRow,
    PlanRow,
    ScheduledActionRow,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 4, 10, 12, 0, tzinfo=UTC))


async def _seed_control_case(
    sessionmaker: async_sessionmaker[AsyncSession],
    razorpay_customer_id: str,
    *,
    arm: Arm = Arm.CONTROL,
) -> tuple[Case, Diagnosis]:
    async with sessionmaker() as session:
        customer = await resolve_customer(session, razorpay_customer_id)
        signal = Signal(
            id=SignalId(uuid7()),
            leak_class=LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
            customer=customer,
            at_risk=Money(500_000, Currency.INR),
            detected_at=_CLOCK.now(),
            source_event_ids=(f"evt-{uuid.uuid4()}",),
            decline=DeclineCategory.INSUFFICIENT_FUNDS,
            context=SignalContext(),
        )
        case = await open_case_for_signal(session, _CLOCK, seed=1, signal=signal)
    assert case is not None

    case.arm = arm
    async with sessionmaker() as session:
        await session.execute(update(CaseRow).where(CaseRow.id == case.id).values(arm=arm.value))
        await session.commit()

    diagnosis = stub_diagnose(case.id, DeclineCategory.INSUFFICIENT_FUNDS, _CLOCK)
    return case, diagnosis


async def test_persist_holdout_advances_the_case_to_holdout(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, diagnosis = await _seed_control_case(sessionmaker, "cust_holdout_basic")

    async with sessionmaker() as session:
        await persist_holdout(session, _CLOCK, case=case, diagnosis=diagnosis)
        await session.commit()

    assert case.state == CaseState.HOLDOUT
    async with sessionmaker() as session:
        row = (await session.execute(select(CaseRow).where(CaseRow.id == case.id))).scalar_one()
    assert row.state == CaseState.HOLDOUT.value


async def test_persist_holdout_writes_zero_plan_and_action_rows(engine: AsyncEngine) -> None:
    """I3: a case in HOLDOUT has zero executed actions -- proven here at
    the strongest level, zero rows exist at all, not merely zero *done*
    ones.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, diagnosis = await _seed_control_case(sessionmaker, "cust_holdout_zero_rows")

    async with sessionmaker() as session:
        await persist_holdout(session, _CLOCK, case=case, diagnosis=diagnosis)
        await session.commit()

    async with sessionmaker() as session:
        plan_rows = (
            (await session.execute(select(PlanRow).where(PlanRow.case_id == case.id)))
            .scalars()
            .all()
        )
        action_rows = (
            (await session.execute(select(ActionRow).where(ActionRow.case_id == case.id)))
            .scalars()
            .all()
        )
        scheduled_rows = (
            (
                await session.execute(
                    select(ScheduledActionRow).where(ScheduledActionRow.case_id == case.id)
                )
            )
            .scalars()
            .all()
        )

    assert plan_rows == []
    assert action_rows == []
    assert scheduled_rows == []


async def test_persist_holdout_writes_diagnosis_completed_then_case_held_out(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, diagnosis = await _seed_control_case(sessionmaker, "cust_holdout_audit")

    async with sessionmaker() as session:
        await persist_holdout(session, _CLOCK, case=case, diagnosis=diagnosis)
        await session.commit()

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(AuditEventRow)
                    .where(AuditEventRow.case_id == case.id)
                    .order_by(AuditEventRow.seq)
                )
            )
            .scalars()
            .all()
        )

    assert [row.kind for row in rows] == [
        "signal_detected",
        "case_opened",
        "arm_assigned",
        "diagnosis_completed",
        "case_held_out",
    ]
    assert rows[-2].payload["root_cause"] == diagnosis.root_cause
    assert rows[-1].payload == {"reason": "arm_is_control"}


async def test_persist_holdout_rejects_a_non_control_arm_case(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, diagnosis = await _seed_control_case(
        sessionmaker, "cust_holdout_wrong_arm", arm=Arm.TREATMENT
    )

    async with sessionmaker() as session:
        with pytest.raises(ValueError, match="arm==control"):
            await persist_holdout(session, _CLOCK, case=case, diagnosis=diagnosis)


async def test_persist_holdout_is_illegal_a_second_time(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, diagnosis = await _seed_control_case(sessionmaker, "cust_holdout_twice")

    async with sessionmaker() as session:
        await persist_holdout(session, _CLOCK, case=case, diagnosis=diagnosis)
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(IllegalTransition):
            await persist_holdout(session, _CLOCK, case=case, diagnosis=diagnosis)


async def test_a_held_out_case_is_still_attributable_anchored_to_case_creation(
    engine: AsyncEngine,
) -> None:
    """TR-30 (T3.4's own checklist): attribution anchors a holdout case's
    match window to `opened_at`, not to an executed action -- there is
    none. `attribution.engine._load_candidates` has carried this since
    T2.8; this proves a case that reaches HOLDOUT through the real
    `persist_holdout` path (rather than a hand-seeded row) is picked up
    by it correctly.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, diagnosis = await _seed_control_case(sessionmaker, "cust_holdout_attributed")

    async with sessionmaker() as session:
        await persist_holdout(session, _CLOCK, case=case, diagnosis=diagnosis)
        await session.commit()

    payment = GatewayPayment(
        id=f"pay_{uuid.uuid4()}",
        order_id="order_1",
        customer_id="cust_holdout_attributed",
        amount=case.at_risk,
        status=PaymentStatus.CAPTURED,
        method="upi",
        issuer="HDFC",
        error_reason=None,
        created_at=_CLOCK.now(),
    )

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id == case.id
    async with sessionmaker() as session:
        row = (await session.execute(select(CaseRow).where(CaseRow.id == case.id))).scalar_one()
        outcome = (
            await session.execute(select(OutcomeRow).where(OutcomeRow.case_id == case.id))
        ).scalar_one()
    assert row.state == CaseState.RECOVERED.value
    assert outcome.kind == OutcomeKind.RECOVERED.value
    assert (
        outcome.attributed_step_id is None
    )  # no action to attribute to -- anchored to case creation
