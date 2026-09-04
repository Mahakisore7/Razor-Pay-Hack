"""Integration tests for `recoup.attribution.engine.attribute_payment`
(T2.8). Needs a real Postgres: the candidate query joins `cases` to
`customers`, the anchor query is a `DISTINCT ON` over `scheduled_actions`,
and the terminal-state write depends on `cases_open_dedup` /
`resolved_iff_terminal` behaving exactly as the migration defines them --
the same reasoning `test_outbox.py` and `test_executor.py` already give
for using a real database instead of a mock.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from recoup.attribution.engine import attribute_payment
from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.domain.case import Arm, CaseState
from recoup.domain.identifiers import SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.gateway.interface import Payment as GatewayPayment
from recoup.gateway.interface import PaymentStatus
from recoup.platform.clock import FrozenClock
from recoup.platform.models import (
    ActionRow,
    AuditEventRow,
    CaseRow,
    OutcomeRow,
    ScheduledActionRow,
)
from recoup.platform.models import (
    Payment as PaymentRow,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
_T0 = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_state(engine: AsyncEngine) -> AsyncIterator[None]:
    """`engine` is module-scoped (see `test_outbox.py`'s fixture of the
    same shape) -- every test needs these tables wiped in front of it
    rather than trusting the last test left nothing behind."""
    async with engine.begin() as conn:
        await conn.execute(delete(OutcomeRow))
        await conn.execute(delete(PaymentRow))
        await conn.execute(delete(ScheduledActionRow))
        await conn.execute(delete(ActionRow))
    yield


async def _seed_case(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    razorpay_customer_id: str,
    state: CaseState,
    arm: Arm = Arm.TREATMENT,
    at_risk_paise: int = 500_000,
    opened_at: datetime = _T0,
) -> uuid.UUID:
    async with sessionmaker() as session:
        customer = await resolve_customer(session, razorpay_customer_id)
        signal = Signal(
            id=SignalId(uuid7()),
            leak_class=LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
            customer=customer,
            at_risk=Money(at_risk_paise, Currency.INR),
            detected_at=opened_at,
            source_event_ids=(f"evt-{uuid.uuid4()}",),
            decline=None,
            context=SignalContext(),
        )
        domain_case = await open_case_for_signal(
            session, FrozenClock(opened_at), seed=1, signal=signal
        )
    assert domain_case is not None
    case_id = domain_case.id

    async with sessionmaker() as session:
        await session.execute(
            update(CaseRow)
            .where(CaseRow.id == case_id)
            .values(state=state.value, arm=arm.value, cost_ceiling_paise=100_000)
        )
        await session.commit()
    return case_id


async def _seed_done_action(
    sessionmaker: async_sessionmaker[AsyncSession],
    case_id: uuid.UUID,
    *,
    step_id: str,
    executed_at: datetime,
) -> None:
    action_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            ActionRow(
                id=action_id,
                case_id=case_id,
                step_id=step_id,
                attempt=1,
                channel="payment_retry",
                idempotency_key=f"idem-{action_id}",
                payload={},
                cost_paise=0,
                due_at=executed_at,
            )
        )
        session.add(
            ScheduledActionRow(
                id=uuid.uuid4(),
                action_id=action_id,
                case_id=case_id,
                due_at=executed_at,
                status="done",
                executed_at=executed_at,
                attempts=1,
            )
        )
        await session.commit()


async def _seed_pending_action(
    sessionmaker: async_sessionmaker[AsyncSession], case_id: uuid.UUID
) -> None:
    action_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            ActionRow(
                id=action_id,
                case_id=case_id,
                step_id="step-1",
                attempt=1,
                channel="payment_retry",
                idempotency_key=f"idem-{action_id}",
                payload={},
                cost_paise=0,
                due_at=_T0,
            )
        )
        session.add(
            ScheduledActionRow(
                id=uuid.uuid4(), action_id=action_id, case_id=case_id, due_at=_T0, status="pending"
            )
        )
        await session.commit()


def _payment(
    *,
    customer_id: str,
    amount_paise: int,
    created_at: datetime,
    payment_id: str | None = None,
    status: PaymentStatus = PaymentStatus.CAPTURED,
) -> GatewayPayment:
    return GatewayPayment(
        id=payment_id or f"pay_{uuid.uuid4()}",
        order_id="order_1",
        customer_id=customer_id,
        amount=Money(amount_paise, Currency.INR),
        status=status,
        method="upi",
        issuer="HDFC",
        error_reason=None,
        created_at=created_at,
    )


async def _case_row(sessionmaker: async_sessionmaker[AsyncSession], case_id: uuid.UUID) -> CaseRow:
    async with sessionmaker() as session:
        result = await session.execute(select(CaseRow).where(CaseRow.id == case_id))
        return result.scalar_one()


async def _outcome_count(sessionmaker: async_sessionmaker[AsyncSession], case_id: uuid.UUID) -> int:
    async with sessionmaker() as session:
        result = await session.execute(select(OutcomeRow).where(OutcomeRow.case_id == case_id))
        return len(result.scalars().all())


async def _audit_rows(
    sessionmaker: async_sessionmaker[AsyncSession], case_id: uuid.UUID
) -> list[AuditEventRow]:
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditEventRow)
            .where(AuditEventRow.case_id == case_id)
            .order_by(AuditEventRow.seq)
        )
        return list(result.scalars().all())


# --- ignored inputs ------------------------------------------------------------------


async def test_a_non_captured_payment_is_ignored(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    payment = _payment(
        customer_id="cust_not_captured",
        amount_paise=500_000,
        created_at=_T0,
        status=PaymentStatus.FAILED,
    )

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id is None
    assert result.ambiguous_case_ids == ()


async def test_a_payment_for_an_unknown_customer_is_recorded_unmatched(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    payment = _payment(customer_id="cust_nobody", amount_paise=500_000, created_at=_T0)

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id is None
    async with sessionmaker() as session:
        row = await session.get(PaymentRow, payment.id)
    assert row is not None
    assert row.case_id is None
    assert row.amount_paise == 500_000


# --- TR-30: holdout anchors to case creation ------------------------------------------


async def test_a_holdout_case_matches_within_its_creation_anchored_window(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(
        sessionmaker,
        razorpay_customer_id="cust_holdout_hit",
        state=CaseState.HOLDOUT,
        arm=Arm.CONTROL,
    )
    payment = _payment(
        customer_id="cust_holdout_hit", amount_paise=500_000, created_at=_T0 + timedelta(hours=10)
    )

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id == case_id
    row = await _case_row(sessionmaker, case_id)
    assert row.state == CaseState.RECOVERED.value
    assert row.resolved_at is not None
    assert await _outcome_count(sessionmaker, case_id) == 1

    # I4 (T2.9): the winner gets `payment_attributed` then `case_resolved`,
    # after case creation's own three-event chain.
    audit = await _audit_rows(sessionmaker, case_id)
    assert [row.kind for row in audit][-2:] == ["payment_attributed", "case_resolved"]
    assert audit[-1].payload["kind"] == "recovered"


async def test_a_holdout_case_does_not_match_outside_its_creation_anchored_window(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(
        sessionmaker,
        razorpay_customer_id="cust_holdout_miss",
        state=CaseState.HOLDOUT,
        arm=Arm.CONTROL,
    )
    payment = _payment(
        customer_id="cust_holdout_miss", amount_paise=500_000, created_at=_T0 + timedelta(hours=73)
    )

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id is None
    row = await _case_row(sessionmaker, case_id)
    assert row.state == CaseState.HOLDOUT.value


# --- non-holdout anchors to the most recent executed action --------------------------


async def test_an_executing_case_anchors_to_its_most_recent_executed_action_not_creation(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(
        sessionmaker,
        razorpay_customer_id="cust_anchor",
        state=CaseState.EXECUTING,
        arm=Arm.TREATMENT,
    )
    action_at = _T0 + timedelta(hours=48)  # well after case creation
    await _seed_done_action(sessionmaker, case_id, step_id="retry-1", executed_at=action_at)

    # Inside the action's window but outside a creation-anchored one.
    payment = _payment(
        customer_id="cust_anchor", amount_paise=500_000, created_at=action_at + timedelta(hours=10)
    )

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id == case_id


async def test_an_executing_case_does_not_match_before_its_action_ran(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(
        sessionmaker,
        razorpay_customer_id="cust_too_early",
        state=CaseState.EXECUTING,
        arm=Arm.TREATMENT,
    )
    action_at = _T0 + timedelta(hours=48)
    await _seed_done_action(sessionmaker, case_id, step_id="retry-1", executed_at=action_at)

    # Inside a creation-anchored window, but before the action's own anchor.
    payment = _payment(
        customer_id="cust_too_early", amount_paise=500_000, created_at=_T0 + timedelta(hours=10)
    )

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id is None


async def test_the_anchor_is_the_latest_of_several_executed_actions(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(
        sessionmaker, razorpay_customer_id="cust_latest_action", state=CaseState.EXECUTING
    )
    await _seed_done_action(
        sessionmaker, case_id, step_id="retry-1", executed_at=_T0 + timedelta(hours=1)
    )
    latest_at = _T0 + timedelta(hours=50)
    await _seed_done_action(sessionmaker, case_id, step_id="link-2", executed_at=latest_at)

    payment = _payment(
        customer_id="cust_latest_action",
        amount_paise=500_000,
        created_at=latest_at + timedelta(hours=1),
    )

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id == case_id
    async with sessionmaker() as session:
        outcome = (
            await session.execute(select(OutcomeRow).where(OutcomeRow.case_id == case_id))
        ).scalar_one()
    assert outcome.attributed_step_id == "link-2"  # credit goes to the step whose window this was


async def test_an_executing_case_with_no_completed_action_is_not_a_candidate(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(
        sessionmaker, razorpay_customer_id="cust_no_completed", state=CaseState.EXECUTING
    )
    await _seed_pending_action(sessionmaker, case_id)
    payment = _payment(customer_id="cust_no_completed", amount_paise=500_000, created_at=_T0)

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id is None


async def test_a_case_already_awaiting_outcome_skips_the_extra_transition(
    engine: AsyncEngine,
) -> None:
    """A case whose plan is already exhausted has already made the
    EXECUTING -> AWAITING_OUTCOME hop by the time attribution sees it --
    `_resolve_case` must not attempt that transition a second time."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(
        sessionmaker, razorpay_customer_id="cust_already_awaiting", state=CaseState.AWAITING_OUTCOME
    )
    action_at = _T0 + timedelta(hours=5)
    await _seed_done_action(sessionmaker, case_id, step_id="retry-1", executed_at=action_at)
    payment = _payment(
        customer_id="cust_already_awaiting",
        amount_paise=500_000,
        created_at=action_at + timedelta(hours=1),
    )

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id == case_id
    row = await _case_row(sessionmaker, case_id)
    assert row.state == CaseState.RECOVERED.value


# --- TR-29: contention -----------------------------------------------------------------


async def test_contention_resolves_to_the_older_case_and_reports_the_younger(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    older_id = await _seed_case(
        sessionmaker,
        razorpay_customer_id="cust_contended",
        state=CaseState.HOLDOUT,
        arm=Arm.CONTROL,
        at_risk_paise=500_000,
        opened_at=_T0,
    )
    younger_id = await _seed_case(
        sessionmaker,
        razorpay_customer_id="cust_contended",
        state=CaseState.HOLDOUT,
        arm=Arm.CONTROL,
        at_risk_paise=502_000,  # different amount -- distinct from `older` under cases_open_dedup
        opened_at=_T0 + timedelta(hours=1),
    )
    # Within tolerance of both at_risk amounts (500_000 and 502_000).
    payment = _payment(
        customer_id="cust_contended", amount_paise=501_000, created_at=_T0 + timedelta(hours=2)
    )

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id == older_id
    assert result.ambiguous_case_ids == (younger_id,)
    older_row = await _case_row(sessionmaker, older_id)
    younger_row = await _case_row(sessionmaker, younger_id)
    assert older_row.state == CaseState.RECOVERED.value
    assert younger_row.state == CaseState.HOLDOUT.value  # untouched -- never attributed to both

    # I4 (T2.9): the winner's own chain, plus the loser's own
    # `attribution_ambiguous` on ITS chain -- the loser's state never
    # changed, so it gets no `case_resolved`.
    older_audit = await _audit_rows(sessionmaker, older_id)
    younger_audit = await _audit_rows(sessionmaker, younger_id)
    assert [row.kind for row in older_audit][-2:] == ["payment_attributed", "case_resolved"]
    assert [row.kind for row in younger_audit][-1] == "attribution_ambiguous"
    assert younger_audit[-1].payload["winning_case_id"] == str(older_id)


# --- idempotency and never-twice ---------------------------------------------------------


async def test_replaying_the_same_payment_is_a_no_op(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(
        sessionmaker, razorpay_customer_id="cust_replay", state=CaseState.HOLDOUT, arm=Arm.CONTROL
    )
    payment = _payment(
        customer_id="cust_replay", amount_paise=500_000, created_at=_T0, payment_id="pay_replay"
    )

    async with sessionmaker() as session:
        first = await attribute_payment(session, _CLOCK, payment=payment)
    async with sessionmaker() as session:
        second = await attribute_payment(session, _CLOCK, payment=payment)

    assert first.matched_case_id == case_id
    assert second.matched_case_id == case_id
    assert await _outcome_count(sessionmaker, case_id) == 1  # not re-inserted


async def test_a_second_payment_can_never_attribute_an_already_resolved_case(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(
        sessionmaker,
        razorpay_customer_id="cust_no_double",
        state=CaseState.HOLDOUT,
        arm=Arm.CONTROL,
    )
    first_payment = _payment(
        customer_id="cust_no_double", amount_paise=500_000, created_at=_T0, payment_id="pay_first"
    )
    second_payment = _payment(
        customer_id="cust_no_double",
        amount_paise=500_000,
        created_at=_T0 + timedelta(hours=1),
        payment_id="pay_second",
    )

    async with sessionmaker() as session:
        first = await attribute_payment(session, _CLOCK, payment=first_payment)
    async with sessionmaker() as session:
        second = await attribute_payment(session, _CLOCK, payment=second_payment)

    assert first.matched_case_id == case_id
    assert second.matched_case_id is None  # the case is terminal -- no longer a candidate
    assert await _outcome_count(sessionmaker, case_id) == 1


# --- partial recovery ------------------------------------------------------------------


async def test_an_underpayment_beyond_tolerance_is_partially_recovered(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(
        sessionmaker,
        razorpay_customer_id="cust_partial",
        state=CaseState.HOLDOUT,
        arm=Arm.CONTROL,
        at_risk_paise=500_000,
    )
    payment = _payment(customer_id="cust_partial", amount_paise=300_000, created_at=_T0)

    async with sessionmaker() as session:
        result = await attribute_payment(session, _CLOCK, payment=payment)

    assert result.matched_case_id == case_id
    row = await _case_row(sessionmaker, case_id)
    assert row.state == CaseState.PARTIALLY_RECOVERED.value
    async with sessionmaker() as session:
        outcome = (
            await session.execute(select(OutcomeRow).where(OutcomeRow.case_id == case_id))
        ).scalar_one()
    assert outcome.kind == "partially_recovered"
    assert outcome.recovered_paise == 300_000  # the actual amount, not rounded up
    assert outcome.reason_code is None
