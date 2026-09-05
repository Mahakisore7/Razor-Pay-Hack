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
from recoup.domain.action import ActionCategory, Channel
from recoup.domain.case import Arm, Case, CaseState, IllegalTransition
from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.planning.planner import DroppedStep, PlanningResult, build_plan, select_playbook
from recoup.planning.playbooks.loader import load_playbooks
from recoup.planning.playbooks.schema import Playbook
from recoup.planning.repository import persist_plan
from recoup.platform.clock import FrozenClock
from recoup.platform.models import (
    ActionRow,
    AuditEventRow,
    CaseRow,
    PlannedStepRow,
    PlanRow,
    ScheduledActionRow,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 4, 10, 12, 0, tzinfo=UTC))
_PLAYBOOKS = load_playbooks()


async def _seed_planned_case(
    sessionmaker: async_sessionmaker[AsyncSession],
    razorpay_customer_id: str,
    *,
    arm: Arm = Arm.TREATMENT,
    at_risk: Money = Money(500_000, Currency.INR),
) -> tuple[Case, Playbook, PlanningResult]:
    async with sessionmaker() as session:
        customer = await resolve_customer(session, razorpay_customer_id)
        signal = Signal(
            id=SignalId(uuid7()),
            leak_class=LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
            customer=customer,
            at_risk=at_risk,
            detected_at=_CLOCK.now(),
            source_event_ids=(f"evt-{uuid.uuid4()}",),
            decline=DeclineCategory.INSUFFICIENT_FUNDS,
            context=SignalContext(),
        )
        case = await open_case_for_signal(session, _CLOCK, seed=1, signal=signal)
    assert case is not None

    # Force the requested arm: `assign_arm` is seed-derived, and I7
    # forbids a control-arm case from ever reaching EXECUTING, which
    # `persist_plan` always does -- these tests need a deterministic,
    # plannable arm (or, for the P9 test below, a deterministically
    # unplannable one).
    case.arm = arm
    async with sessionmaker() as session:
        await session.execute(update(CaseRow).where(CaseRow.id == case.id).values(arm=arm.value))
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
    assert all(action.category == ActionCategory.TRANSACTIONAL for action, _ in realised)

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


async def test_persist_plan_routes_a_high_at_risk_case_to_awaiting_approval(
    engine: AsyncEngine,
) -> None:
    """R10 (POLICY-ENGINE SS3, T4.1): `AWAITING_APPROVAL` is only ever
    legal from `PLANNED` (domain.case's own transition table) -- this is
    the one and only place that routing decision can be made, since
    `persist_plan` already owns the `PLANNED -> EXECUTING` hop."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, playbook, result = await _seed_planned_case(
        sessionmaker, "cust_plan_repo_approval", at_risk=Money(3_000_000, Currency.INR)
    )

    async with sessionmaker() as session:
        realised = await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result)
        await session.commit()

    assert case.state == CaseState.AWAITING_APPROVAL
    # Still planned and scheduled as normal -- it is the policy gate
    # (R10, evaluated per action) that keeps these from executing while
    # the case awaits a human, not an absence of rows to execute.
    assert [action.step_id for action, _ in realised] == ["retry", "payment_link"]

    async with sessionmaker() as session:
        row = (await session.execute(select(CaseRow).where(CaseRow.id == case.id))).scalar_one()
        action_rows = (
            (await session.execute(select(ActionRow).where(ActionRow.case_id == case.id)))
            .scalars()
            .all()
        )
    assert row.state == CaseState.AWAITING_APPROVAL.value
    assert len(action_rows) == 2


async def test_persist_plan_audits_approval_requested_for_a_high_at_risk_case(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, playbook, result = await _seed_planned_case(
        sessionmaker, "cust_plan_repo_approval_audit", at_risk=Money(3_000_000, Currency.INR)
    )

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

    assert [row.kind for row in rows][-2:] == ["plan_created", "approval_requested"]
    assert rows[-1].payload == {"at_risk_paise": 3_000_000, "threshold_paise": 2_500_000}


async def test_persist_plan_does_not_require_approval_exactly_at_the_threshold(
    engine: AsyncEngine,
) -> None:
    """Boundary test (T4.8's own philosophy): exactly at the threshold is
    not *above* it (`requires_approval` is a strict `>`)."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, playbook, result = await _seed_planned_case(
        sessionmaker, "cust_plan_repo_approval_boundary", at_risk=Money(2_500_000, Currency.INR)
    )

    async with sessionmaker() as session:
        await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result)
        await session.commit()

    assert case.state == CaseState.EXECUTING


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


async def test_persist_plan_raises_and_writes_nothing_for_a_control_arm_case(
    engine: AsyncEngine,
) -> None:
    """P9 (POLICY-ENGINE SS6.2; PHASE-03 T3.2): a control-arm case
    accumulates zero executed actions under any input sequence.
    `test_case_state_machine.py::control_never_executes` already proves
    this holds for the pure in-memory `Case.transition_to` guard; this
    confirms it holds at the persistence layer too -- `persist_plan`
    calls `case.transition_to(EXECUTING)` *before* adding a single row
    (planning/repository.py), so I7 rejects a control-arm case before
    any `PlanRow`, `ActionRow`, or `ScheduledActionRow` is ever written,
    not merely before one would be claimed or run.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, playbook, result = await _seed_planned_case(
        sessionmaker, "cust_plan_repo_control", arm=Arm.CONTROL
    )

    async with sessionmaker() as session:
        with pytest.raises(IllegalTransition):
            await persist_plan(session, _CLOCK, case=case, playbook=playbook, result=result)

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
        plan_rows = (
            (await session.execute(select(PlanRow).where(PlanRow.case_id == case.id)))
            .scalars()
            .all()
        )

    assert action_rows == []
    assert scheduled_rows == []
    assert plan_rows == []
