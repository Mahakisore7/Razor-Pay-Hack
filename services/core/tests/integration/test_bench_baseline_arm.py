"""Integration proof that the baseline-arm playbook (T3.3) is an
ordinary `Playbook` as far as `planning.planner.build_plan` and
`planning.repository.persist_plan` are concerned -- a baseline-arm case
reaches `EXECUTING` with real, claimable `ScheduledActionRow`s exactly
like a diagnosis-routed treatment-arm case does, no baseline-specific
persistence path required.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from recoup.bench.baseline import load_baseline_playbook
from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.domain.action import Channel
from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.planning.planner import build_plan
from recoup.planning.repository import persist_plan
from recoup.platform.clock import FrozenClock
from recoup.platform.models import ActionRow, AuditEventRow, CaseRow, ScheduledActionRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 4, 10, 12, 0, tzinfo=UTC))


async def _seed_baseline_case(
    sessionmaker: async_sessionmaker[AsyncSession], razorpay_customer_id: str
) -> Case:
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

    # Force BASELINE: `assign_arm` is seed-derived, and this test needs a
    # deterministic arm rather than whatever the seed happens to draw.
    case.arm = Arm.BASELINE
    async with sessionmaker() as session:
        await session.execute(
            update(CaseRow).where(CaseRow.id == case.id).values(arm=Arm.BASELINE.value)
        )
        await session.commit()
    return case


async def test_a_baseline_arm_case_reaches_executing_with_the_fixed_schedule(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case = await _seed_baseline_case(sessionmaker, "cust_baseline_basic")
    playbook = load_baseline_playbook()
    result = build_plan(case, playbook, _CLOCK)

    async with sessionmaker() as session:
        realised = await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result)
        await session.commit()

    assert case.state == CaseState.EXECUTING
    assert {action.step_id for action, _ in realised} == {
        "retry_1",
        "dunning_email",
        "retry_2",
        "retry_3",
    }
    channels_by_step = {action.step_id: action.channel for action, _ in realised}
    assert channels_by_step["retry_1"] == Channel.PAYMENT_RETRY
    assert channels_by_step["dunning_email"] == Channel.EMAIL
    assert channels_by_step["retry_2"] == Channel.PAYMENT_RETRY
    assert channels_by_step["retry_3"] == Channel.PAYMENT_RETRY

    due_at_by_step = {action.step_id: action.due_at for action, _ in realised}
    assert due_at_by_step["retry_1"] == _CLOCK.now() + timedelta(hours=1)
    assert due_at_by_step["dunning_email"] == _CLOCK.now() + timedelta(hours=2)
    assert due_at_by_step["retry_2"] == _CLOCK.now() + timedelta(hours=24)
    assert due_at_by_step["retry_3"] == _CLOCK.now() + timedelta(hours=72)


async def test_a_baseline_arm_case_writes_real_claimable_rows(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case = await _seed_baseline_case(sessionmaker, "cust_baseline_rows")
    playbook = load_baseline_playbook()
    result = build_plan(case, playbook, _CLOCK)

    async with sessionmaker() as session:
        await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result)
        await session.commit()

    async with sessionmaker() as session:
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

    assert len(action_rows) == 4
    assert len(scheduled_rows) == 4
    assert {row.status for row in scheduled_rows} == {"pending"}


async def test_a_baseline_arm_case_writes_the_same_audit_shape_as_treatment(
    engine: AsyncEngine,
) -> None:
    """T3.3 adds no baseline-specific persistence path -- confirmed here
    by the audit chain looking exactly like `test_planning_repository.
    py::test_persist_plan_writes_a_gapless_audit_chain`'s treatment-arm
    case, just against a different playbook.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case = await _seed_baseline_case(sessionmaker, "cust_baseline_audit")
    playbook = load_baseline_playbook()
    result = build_plan(case, playbook, _CLOCK)

    async with sessionmaker() as session:
        await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result)
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
        "plan_created",
    ]
    assert rows[-1].payload["playbook_id"] == "baseline-naive"
