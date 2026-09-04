"""Integration tests for `recoup.execution.suppression.suppress_case`
(T2.10, A2.8) against a real Postgres.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.diagnosis.engine import stub_diagnose
from recoup.domain.case import Arm, CaseState, IllegalTransition
from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import CaseId, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.execution.suppression import suppress_case
from recoup.planning.planner import build_plan, select_playbook
from recoup.planning.playbooks.loader import load_playbooks
from recoup.planning.repository import persist_plan
from recoup.platform.clock import FrozenClock
from recoup.platform.models import AuditEventRow, CaseRow, OutcomeRow, ScheduledActionRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 4, 10, 12, 0, tzinfo=UTC))
_PLAYBOOKS = load_playbooks()


async def _seed_executing_case(
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
            decline=DeclineCategory.INSUFFICIENT_FUNDS,
            context=SignalContext(),
        )
        case = await open_case_for_signal(session, _CLOCK, seed=1, signal=signal)
    assert case is not None
    case.arm = Arm.TREATMENT
    async with sessionmaker() as session:
        await session.execute(
            update(CaseRow).where(CaseRow.id == case.id).values(arm=Arm.TREATMENT.value)
        )
        await session.commit()

    diagnosis = stub_diagnose(case.id, DeclineCategory.INSUFFICIENT_FUNDS, _CLOCK)
    playbook = select_playbook(_PLAYBOOKS, diagnosis, LeakClass.L1_FAILED_ONE_TIME_PAYMENT)
    assert playbook is not None
    result = build_plan(case, playbook, _CLOCK)

    async with sessionmaker() as session:
        await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result)
        await session.commit()
    return case.id


async def test_suppress_case_cancels_pending_steps_and_closes_the_case(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_executing_case(sessionmaker, "cust_suppress_basic")

    async with sessionmaker() as session:
        cancelled = await suppress_case(
            session, _CLOCK, case_id=case_id, reason_code="customer_opt_out"
        )
        await session.commit()

    assert cancelled == 2  # both retry and payment_link were still pending

    async with sessionmaker() as session:
        case_row = (
            await session.execute(select(CaseRow).where(CaseRow.id == case_id))
        ).scalar_one()
        scheduled_rows = (
            (
                await session.execute(
                    select(ScheduledActionRow).where(ScheduledActionRow.case_id == case_id)
                )
            )
            .scalars()
            .all()
        )
        outcome = (
            await session.execute(select(OutcomeRow).where(OutcomeRow.case_id == case_id))
        ).scalar_one()

    assert case_row.state == CaseState.SUPPRESSED.value
    assert case_row.resolved_at is not None
    assert {row.status for row in scheduled_rows} == {"cancelled"}
    assert outcome.kind == "suppressed"
    assert outcome.reason_code == "customer_opt_out"
    assert outcome.recovered_paise == 0


async def test_suppress_case_leaves_an_already_done_step_alone(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_executing_case(sessionmaker, "cust_suppress_partial")

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(ScheduledActionRow)
                    .where(ScheduledActionRow.case_id == case_id)
                    .order_by(ScheduledActionRow.due_at)
                )
            )
            .scalars()
            .all()
        )
        done_id = rows[0].id
        await session.execute(
            update(ScheduledActionRow)
            .where(ScheduledActionRow.id == done_id)
            .values(status="done", executed_at=_CLOCK.now())
        )
        await session.commit()

    async with sessionmaker() as session:
        cancelled = await suppress_case(
            session, _CLOCK, case_id=case_id, reason_code="customer_opt_out"
        )
        await session.commit()

    assert cancelled == 1  # only the still-pending step

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(ScheduledActionRow).where(ScheduledActionRow.case_id == case_id)
                )
            )
            .scalars()
            .all()
        )
    statuses = {row.id: row.status for row in rows}
    assert statuses[done_id] == "done"  # untouched -- it already ran


async def test_suppress_case_writes_a_gapless_audit_chain(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_executing_case(sessionmaker, "cust_suppress_audit")

    async with sessionmaker() as session:
        await suppress_case(session, _CLOCK, case_id=case_id, reason_code="customer_opt_out")
        await session.commit()

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(AuditEventRow)
                    .where(AuditEventRow.case_id == case_id)
                    .order_by(AuditEventRow.seq)
                )
            )
            .scalars()
            .all()
        )

    # 3 (case creation) + 1 (plan_created) + 2 (this suppression).
    assert [row.kind for row in rows][-2:] == ["stopping_rule_fired", "case_resolved"]
    assert rows[-2].payload["reason_code"] == "customer_opt_out"
    assert rows[-2].payload["cancelled_steps"] == 2


async def test_suppress_case_is_illegal_once_the_case_is_terminal(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_executing_case(sessionmaker, "cust_suppress_twice")

    async with sessionmaker() as session:
        await suppress_case(session, _CLOCK, case_id=case_id, reason_code="customer_opt_out")
        await session.commit()

    async with sessionmaker() as session:
        with pytest.raises(IllegalTransition):
            await suppress_case(
                session, _CLOCK, case_id=case_id, reason_code="customer_opt_out_again"
            )
