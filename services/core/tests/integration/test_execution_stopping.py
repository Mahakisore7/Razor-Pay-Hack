"""Integration tests for `recoup.execution.stopping.apply_stopping_rule`
(POLICY-ENGINE R2, PHASE-04 T4.2) against a real Postgres -- the
mechanical side of every stopping-rule outcome `policy.rules.stopping`
can name, generalising what `test_suppression.py` already covers for
`customer_opt_out` alone.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.diagnosis.engine import stub_diagnose
from recoup.domain.case import Arm, CaseState
from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import ActionId, CaseId, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.execution.stopping import apply_stopping_rule
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


def _decision(
    *, verdict: Verdict, rule_id: str, defer_until: datetime | None = None
) -> PolicyDecision:
    return PolicyDecision(
        action_id=ActionId(uuid7()),
        attempt=1,
        verdict=verdict,
        rule_id=rule_id,
        inputs={},
        defer_until=defer_until,
        decided_at=_CLOCK.now(),
    )


async def _case_row(sessionmaker: async_sessionmaker[AsyncSession], case_id: CaseId) -> CaseRow:
    async with sessionmaker() as session:
        return (await session.execute(select(CaseRow).where(CaseRow.id == case_id))).scalar_one()


async def _scheduled_rows(
    sessionmaker: async_sessionmaker[AsyncSession], case_id: CaseId
) -> list[ScheduledActionRow]:
    async with sessionmaker() as session:
        result = await session.execute(
            select(ScheduledActionRow).where(ScheduledActionRow.case_id == case_id)
        )
        return list(result.scalars().all())


async def _audit_rows(
    sessionmaker: async_sessionmaker[AsyncSession], case_id: CaseId
) -> list[AuditEventRow]:
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditEventRow)
            .where(AuditEventRow.case_id == case_id)
            .order_by(AuditEventRow.seq)
        )
        return list(result.scalars().all())


# --- terminal outcomes: customer_opt_out / dispute_filed / customer_deceased_or_closed


async def test_apply_stopping_rule_suppresses_the_case_for_customer_opt_out(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_executing_case(sessionmaker, "cust_stop_opt_out")

    async with sessionmaker() as session:
        decision = _decision(verdict=Verdict.DENY, rule_id="customer_opt_out")
        cancelled = await apply_stopping_rule(session, _CLOCK, case_id=case_id, decision=decision)

    assert cancelled == 2
    row = await _case_row(sessionmaker, case_id)
    assert row.state == CaseState.SUPPRESSED.value
    assert row.resolved_at is not None
    scheduled = await _scheduled_rows(sessionmaker, case_id)
    assert {r.status for r in scheduled} == {"cancelled"}

    async with sessionmaker() as session:
        outcome = (
            await session.execute(select(OutcomeRow).where(OutcomeRow.case_id == case_id))
        ).scalar_one()
    assert outcome.kind == "suppressed"
    assert outcome.reason_code == "customer_opt_out"

    audit = await _audit_rows(sessionmaker, case_id)
    assert [row.kind for row in audit][-2:] == ["stopping_rule_fired", "case_resolved"]
    assert audit[-2].payload["rule_id"] == "customer_opt_out"
    assert audit[-2].payload["cancelled_steps"] == 2


# --- max_attempts_reached -> LOST --------------------------------------------------


async def test_apply_stopping_rule_marks_the_case_lost_for_max_attempts_reached(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_executing_case(sessionmaker, "cust_stop_max_attempts")

    async with sessionmaker() as session:
        cancelled = await apply_stopping_rule(
            session,
            _CLOCK,
            case_id=case_id,
            decision=_decision(verdict=Verdict.DENY, rule_id="max_attempts_reached"),
        )

    assert cancelled == 2
    row = await _case_row(sessionmaker, case_id)
    assert row.state == CaseState.LOST.value
    assert row.resolved_at is not None

    async with sessionmaker() as session:
        outcome = (
            await session.execute(select(OutcomeRow).where(OutcomeRow.case_id == case_id))
        ).scalar_one()
    assert outcome.kind == "lost"
    assert outcome.reason_code == "max_attempts_reached"


# --- max_case_age -> EXPIRED, its own `case_expired` audit kind --------------------


async def test_apply_stopping_rule_expires_the_case_for_max_case_age(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_executing_case(sessionmaker, "cust_stop_max_case_age")

    async with sessionmaker() as session:
        decision = _decision(verdict=Verdict.DENY, rule_id="max_case_age")
        await apply_stopping_rule(session, _CLOCK, case_id=case_id, decision=decision)

    row = await _case_row(sessionmaker, case_id)
    assert row.state == CaseState.EXPIRED.value
    assert row.resolved_at is not None

    audit = await _audit_rows(sessionmaker, case_id)
    assert [row.kind for row in audit][-2:] == ["case_expired", "case_resolved"]

    async with sessionmaker() as session:
        outcome = (
            await session.execute(select(OutcomeRow).where(OutcomeRow.case_id == case_id))
        ).scalar_one()
    assert outcome.kind == "expired"


# --- mandate_revoked -> ESCALATED, not terminal ------------------------------------


async def test_apply_stopping_rule_escalates_without_closing_the_case(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_executing_case(sessionmaker, "cust_stop_mandate_revoked")

    async with sessionmaker() as session:
        cancelled = await apply_stopping_rule(
            session,
            _CLOCK,
            case_id=case_id,
            decision=_decision(verdict=Verdict.DENY, rule_id="mandate_revoked"),
        )

    assert cancelled == 2
    row = await _case_row(sessionmaker, case_id)
    assert row.state == CaseState.ESCALATED.value
    # Not terminal (domain.case.TERMINAL_STATES) -- no resolution yet, a
    # human (or a future re-mandate flow) still has to move it on.
    assert row.resolved_at is None

    async with sessionmaker() as session:
        outcome = (
            (await session.execute(select(OutcomeRow).where(OutcomeRow.case_id == case_id)))
            .scalars()
            .all()
        )
    assert outcome == []

    audit = await _audit_rows(sessionmaker, case_id)
    assert audit[-1].kind == "stopping_rule_fired"  # no case_resolved -- the case is not resolved


# --- already_paid: attribution already closed the case, this only sweeps up -------


async def test_apply_stopping_rule_only_cancels_for_already_paid(engine: AsyncEngine) -> None:
    """`already_paid` fires *after* attribution has already transitioned
    the case and written its own `case_resolved`/`Outcome` -- this call's
    only job is cancelling whatever else was still scheduled, not
    duplicating either write (which would violate `outcomes.case_id`'s
    own uniqueness constraint on a second, real attribution-then-stop
    sequence)."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_executing_case(sessionmaker, "cust_stop_already_paid")
    async with sessionmaker() as session:
        await session.execute(
            update(CaseRow)
            .where(CaseRow.id == case_id)
            .values(state=CaseState.RECOVERED.value, resolved_at=_CLOCK.now())
        )
        await session.commit()

    async with sessionmaker() as session:
        decision = _decision(verdict=Verdict.DENY, rule_id="already_paid")
        cancelled = await apply_stopping_rule(session, _CLOCK, case_id=case_id, decision=decision)

    assert cancelled == 2
    row = await _case_row(sessionmaker, case_id)
    assert row.state == CaseState.RECOVERED.value  # untouched -- attribution already set this

    async with sessionmaker() as session:
        outcomes = (
            (await session.execute(select(OutcomeRow).where(OutcomeRow.case_id == case_id)))
            .scalars()
            .all()
        )
    assert outcomes == []  # this call never writes one for already_paid

    audit = await _audit_rows(sessionmaker, case_id)
    assert audit[-1].kind == "stopping_rule_fired"
    assert audit[-1].payload == {"rule_id": "already_paid", "cancelled_steps": 2}


# --- promise_to_pay_active: defer, not terminate -----------------------------------


async def test_apply_stopping_rule_defers_the_whole_case_for_a_promise_to_pay(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_executing_case(sessionmaker, "cust_stop_promise")
    until = _CLOCK.now() + timedelta(days=5)

    async with sessionmaker() as session:
        decision = _decision(
            verdict=Verdict.DEFER, rule_id="promise_to_pay_active", defer_until=until
        )
        deferred = await apply_stopping_rule(session, _CLOCK, case_id=case_id, decision=decision)

    assert deferred == 2
    row = await _case_row(sessionmaker, case_id)
    assert row.state == CaseState.EXECUTING.value  # unchanged -- deferred, not terminated
    assert row.resolved_at is None

    scheduled = await _scheduled_rows(sessionmaker, case_id)
    assert all(r.status == "pending" for r in scheduled)
    assert all(r.due_at == until for r in scheduled)

    audit = await _audit_rows(sessionmaker, case_id)
    assert audit[-1].kind == "stopping_rule_fired"
    assert audit[-1].payload["rule_id"] == "promise_to_pay_active"
    assert audit[-1].payload["deferred_steps"] == 2
