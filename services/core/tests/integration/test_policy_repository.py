"""Integration tests for `recoup.policy.repository.persist_decision`
(T2.10) against a real Postgres -- the missing write path `policy/
engine.py` itself never had, since `evaluate` is pure.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.domain.action import Action, ActionCategory, ActionPayload, Channel
from recoup.domain.consent import ConsentSource
from recoup.domain.identifiers import ActionId, CaseId, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.platform.clock import FrozenClock
from recoup.platform.models import ActionRow, AuditEventRow, PolicyDecisionRow
from recoup.policy.repository import load_consent_events, persist_decision, record_consent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 4, 10, 12, 0, tzinfo=UTC))


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


async def _seed_action(sessionmaker: async_sessionmaker[AsyncSession], case_id: CaseId) -> Action:
    action = Action(
        id=ActionId(uuid7()),
        case_id=case_id,
        step_id="retry",
        attempt=1,
        channel=Channel.PAYMENT_RETRY,
        category=ActionCategory.TRANSACTIONAL,
        payload=ActionPayload(),
        cost=Money(0, Currency.INR),
        due_at=_CLOCK.now(),
    )
    async with sessionmaker() as session:
        session.add(
            ActionRow(
                id=action.id,
                case_id=case_id,
                step_id=action.step_id,
                attempt=action.attempt,
                channel=action.channel.value,
                idempotency_key=action.idempotency_key,
                payload={},
                cost_paise=0,
                due_at=action.due_at,
            )
        )
        await session.commit()
    return action


async def test_persist_decision_writes_an_allow(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_policy_repo_allow")
    action = await _seed_action(sessionmaker, case_id)
    decision = PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.ALLOW,
        rule_id=None,
        inputs={},
        defer_until=None,
        decided_at=_CLOCK.now(),
    )

    async with sessionmaker() as session:
        await persist_decision(session, _CLOCK, case_id=case_id, decision=decision)
        await session.commit()

    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(PolicyDecisionRow).where(PolicyDecisionRow.action_id == action.id)
            )
        ).scalar_one()
        audit = (
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

    assert row.verdict == "allow"
    # 3 from case creation, 1 for the evaluation -- ALLOW gets no second event.
    assert [row.kind for row in audit][-1] == "policy_evaluated"
    assert len(audit) == 4


async def test_persist_decision_writes_a_deny_with_its_own_extra_event(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_policy_repo_deny")
    action = await _seed_action(sessionmaker, case_id)
    decision = PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DENY,
        rule_id="kill_switch_active",
        inputs={"global_tripped": True},
        defer_until=None,
        decided_at=_CLOCK.now(),
    )

    async with sessionmaker() as session:
        await persist_decision(session, _CLOCK, case_id=case_id, decision=decision)
        await session.commit()

    async with sessionmaker() as session:
        audit = (
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

    assert [row.kind for row in audit][-2:] == ["policy_evaluated", "policy_denied"]
    assert audit[-1].payload["rule_id"] == "kill_switch_active"


async def test_persist_decision_writes_a_defer_with_its_own_extra_event(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_policy_repo_defer")
    action = await _seed_action(sessionmaker, case_id)
    defer_until = _CLOCK.now()
    decision = PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DEFER,
        rule_id="quiet_hours",
        inputs={},
        defer_until=defer_until,
        decided_at=_CLOCK.now(),
    )

    async with sessionmaker() as session:
        await persist_decision(session, _CLOCK, case_id=case_id, decision=decision)
        await session.commit()

    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(PolicyDecisionRow).where(PolicyDecisionRow.action_id == action.id)
            )
        ).scalar_one()
        audit = (
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

    assert row.defer_until == defer_until
    assert [row.kind for row in audit][-2:] == ["policy_evaluated", "policy_deferred"]


# --- consent (T3.6 regression coverage for the runner's own fix) ------------


async def test_load_consent_events_is_empty_for_a_customer_with_no_ledger(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        customer = await resolve_customer(session, "cust_consent_repo_empty")
        await session.commit()

    async with sessionmaker() as session:
        events = await load_consent_events(session, customer)
    assert events == ()


async def test_record_consent_then_load_round_trips(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        customer = await resolve_customer(session, "cust_consent_repo_roundtrip")
        await record_consent(
            session,
            customer=customer,
            channel=Channel.EMAIL,
            granted=True,
            source=ConsentSource.CHECKOUT,
            occurred_at=_CLOCK.now(),
        )
        await session.commit()

    async with sessionmaker() as session:
        events = await load_consent_events(session, customer)
    assert len(events) == 1
    assert events[0].channel is Channel.EMAIL
    assert events[0].granted is True
    assert events[0].source is ConsentSource.CHECKOUT
    assert events[0].occurred_at == _CLOCK.now()


async def test_load_consent_events_only_returns_the_given_customers_own_events(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        this_customer = await resolve_customer(session, "cust_consent_repo_mine")
        other_customer = await resolve_customer(session, "cust_consent_repo_not_mine")
        await record_consent(
            session,
            customer=this_customer,
            channel=Channel.SMS,
            granted=True,
            source=ConsentSource.CHECKOUT,
            occurred_at=_CLOCK.now(),
        )
        await record_consent(
            session,
            customer=other_customer,
            channel=Channel.SMS,
            granted=True,
            source=ConsentSource.CHECKOUT,
            occurred_at=_CLOCK.now(),
        )
        await session.commit()

    async with sessionmaker() as session:
        events = await load_consent_events(session, this_customer)
    assert len(events) == 1
    assert events[0].customer.id == this_customer.id
