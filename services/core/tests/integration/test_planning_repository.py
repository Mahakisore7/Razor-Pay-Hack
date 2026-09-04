"""Integration tests for `recoup.planning.repository.persist_plan`
(T2.10) against a real Postgres -- the missing write path `planner.py`
itself never had, since `build_plan` is pure.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.diagnosis.engine import stub_diagnose
from recoup.domain.action import Channel
from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.planning.planner import DroppedStep, PlanningResult, build_plan, select_playbook
from recoup.planning.playbooks.loader import load_playbooks
from recoup.planning.playbooks.schema import Playbook
from recoup.planning.repository import persist_plan
from recoup.platform.clock import FrozenClock
from recoup.platform.models import ActionRow, AuditEventRow, CaseRow, PlannedStepRow, PlanRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 4, 10, 12, 0, tzinfo=UTC))
_PLAYBOOKS = load_playbooks()


async def _seed_planned_case(
    sessionmaker: async_sessionmaker[AsyncSession], razorpay_customer_id: str
) -> tuple[Case, Playbook, PlanningResult]:
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

    # Force TREATMENT: `assign_arm` is seed-derived and I7 forbids a
    # control-arm case from ever reaching EXECUTING, which `persist_plan`
    # always does -- these tests need a deterministic, plannable arm.
    case.arm = Arm.TREATMENT
    async with sessionmaker() as session:
        await session.execute(
            update(CaseRow).where(CaseRow.id == case.id).values(arm=Arm.TREATMENT.value)
        )
        await session.commit()

    diagnosis = stub_diagnose(case.id, DeclineCategory.INSUFFICIENT_FUNDS, _CLOCK)
    playbook = select_playbook(_PLAYBOOKS, diagnosis, LeakClass.L1_FAILED_ONE_TIME_PAYMENT)
    assert playbook is not None
    return case, playbook, build_plan(case, playbook, _CLOCK)


async def test_persist_plan_writes_the_plan_and_its_steps(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, playbook, result = await _seed_planned_case(sessionmaker, "cust_plan_repo_basic")

    async with sessionmaker() as session:
        realised = await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result)
        await session.commit()

    assert [action.step_id for action, _ in realised] == ["retry", "payment_link"]
    assert [action.channel for action, _ in realised] == [Channel.PAYMENT_RETRY, Channel.LINK]

    async with sessionmaker() as session:
        plan_row = (
            await session.execute(select(PlanRow).where(PlanRow.case_id == case.id))
        ).scalar_one()
        step_rows = (
            (
                await session.execute(
                    select(PlannedStepRow).where(PlannedStepRow.plan_id == plan_row.id)
                )
            )
            .scalars()
            .all()
        )
        action_rows = (
            (await session.execute(select(ActionRow).where(ActionRow.case_id == case.id)))
            .scalars()
            .all()
        )

    assert plan_row.playbook_id == "insufficient-funds"
    assert plan_row.playbook_version == 1
    assert {row.step_id for row in step_rows} == {"retry", "payment_link"}
    assert {row.step_id for row in action_rows} == {"retry", "payment_link"}
    assert all(row.attempt == 1 for row in action_rows)


async def test_persist_plan_advances_the_case_to_executing(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, playbook, result = await _seed_planned_case(sessionmaker, "cust_plan_repo_state")

    async with sessionmaker() as session:
        await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result)
        await session.commit()

    async with sessionmaker() as session:
        row = (await session.execute(select(CaseRow).where(CaseRow.id == case.id))).scalar_one()
    assert row.state == CaseState.EXECUTING.value
    assert case.state == CaseState.EXECUTING  # the in-memory Case was mutated too


async def test_persist_plan_writes_a_gapless_audit_chain(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, playbook, result = await _seed_planned_case(sessionmaker, "cust_plan_repo_audit")

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

    # 3 from case creation, 1 for the plan -- this playbook drops nothing.
    assert [row.kind for row in rows] == [
        "signal_detected",
        "case_opened",
        "arm_assigned",
        "plan_created",
    ]
    assert rows[-1].payload["playbook_id"] == "insufficient-funds"
    assert rows[-1].payload["step_ids"] == ["retry", "payment_link"]


async def test_persist_plan_audits_each_dropped_step(engine: AsyncEngine) -> None:
    """The shipped playbook's two steps both cost 0 paise, so
    `build_plan` itself never drops one (TR-18's cost-ceiling fit has
    nothing to trim) -- a `DroppedStep` is constructed directly here to
    exercise `persist_plan`'s own write path for it.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, playbook, result = await _seed_planned_case(sessionmaker, "cust_plan_repo_dropped")
    result_with_a_drop = PlanningResult(
        plan=result.plan,
        dropped_steps=(
            DroppedStep(
                step_id="payment_link",
                expected_cost=Money(0, Currency.INR),
                reason="cost_ceiling_exceeded",
            ),
        ),
    )

    async with sessionmaker() as session:
        await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result_with_a_drop)
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

    assert [row.kind for row in rows][-2:] == ["plan_created", "plan_step_dropped"]
    assert rows[-1].payload == {
        "step_id": "payment_link",
        "expected_cost_paise": 0,
        "reason": "cost_ceiling_exceeded",
    }


async def test_persist_plan_realises_steps_due_at_their_own_planned_time(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, playbook, result = await _seed_planned_case(sessionmaker, "cust_plan_repo_due_at")

    async with sessionmaker() as session:
        realised = await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result)
        await session.commit()

    retry_action, _ = realised[0]
    link_action, _ = realised[1]
    assert retry_action.due_at == _CLOCK.now() + timedelta(hours=6)
    assert link_action.due_at == retry_action.due_at + timedelta(hours=24)
    assert realised[0][0].due_at < realised[1][0].due_at  # returned in due_at order
