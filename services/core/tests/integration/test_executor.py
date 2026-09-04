"""Integration tests for `recoup.execution.executor` (T2.7) -- A2.3, the
phase gate this entire product's claims rest on: "An action with no
ALLOW raises rather than executing." Needs a real Postgres (the ALLOW
lookup, cost accounting, outbox status) and a real Redis (`SET NX`
idempotency -- a mocked `SET NX` tests the mock, the same reasoning
`test_outbox.py` applies to `FOR UPDATE SKIP LOCKED`).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.redis import RedisContainer

from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.domain.action import Action, ActionPayload, Channel
from recoup.domain.case import Case
from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import ActionId, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.execution.executor import (
    ExecutionStatus,
    NoAllowDecisionError,
    execute,
    idempotency_key,
)
from recoup.gateway.interface import Payment, PaymentQuery, PaymentStatus
from recoup.gateway.simulator.simulator import RazorpaySimulator
from recoup.platform.clock import FrozenClock
from recoup.platform.models import (
    ActionRow,
    AuditEventRow,
    CaseRow,
    PolicyDecisionRow,
    ScheduledActionRow,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
_SEED_AT = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def redis_client() -> AsyncIterator[Redis]:
    with RedisContainer("redis:7-alpine") as r:
        client: Redis = Redis(
            host=r.get_container_host_ip(),
            port=int(r.get_exposed_port(6379)),
            decode_responses=True,
        )
        try:
            yield client
        finally:
            await client.aclose()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_state(engine: AsyncEngine, redis_client: Redis) -> AsyncIterator[None]:
    """`engine` and `redis_client` are both module-scoped -- see
    `test_outbox.py`'s fixture of the same shape for why every test needs
    a table wiped in front of it rather than trusting the last test left
    nothing behind."""
    async with engine.begin() as conn:
        await conn.execute(delete(ScheduledActionRow))
        await conn.execute(delete(PolicyDecisionRow))
        await conn.execute(delete(ActionRow))
    await redis_client.flushdb()
    yield


def _find_failed_payment(sim: RazorpaySimulator, *, retryable: bool, tries: int = 500) -> Payment:
    """Same pattern `test_simulator.py` uses -- the simulator's outcome is
    seed-and-input-driven, not directly forceable, so this tries distinct
    customers until one fails the way this test needs."""
    for i in range(tries):
        payment = sim.seed_payment(
            customer_id=f"cust_{i}",
            amount=Money(250_000),
            method="card",
            issuer="HDFC",
            at=_SEED_AT,
        )
        if payment.status != PaymentStatus.FAILED:
            continue
        assert payment.error_reason is not None
        decline = DeclineCategory(payment.error_reason)
        if decline.retryable is retryable:
            return payment
    kind = "retryable" if retryable else "non-retryable"
    raise AssertionError(f"no {kind} failure found in {tries} tries")


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    razorpay_customer_id: str,
    channel: Channel = Channel.PAYMENT_RETRY,
    action_cost_paise: int = 0,
    cost_ceiling_paise: int = 100_000,
    cost_spent_paise: int = 0,
    payload_variables: dict[str, str] | None = None,
    verdict: str | None = "allow",
    attempt: int = 1,
) -> tuple[Case, Action, uuid.UUID]:
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
        domain_case = await open_case_for_signal(session, _CLOCK, seed=1, signal=signal)
    assert domain_case is not None
    case_id = domain_case.id

    async with sessionmaker() as session:
        await session.execute(
            update(CaseRow)
            .where(CaseRow.id == case_id)
            .values(cost_ceiling_paise=cost_ceiling_paise, cost_spent_paise=cost_spent_paise)
        )
        await session.commit()

    action_id = uuid.uuid4()
    step_id = f"step-{uuid.uuid4()}"
    variables = payload_variables or {}
    action = Action(
        id=ActionId(action_id),
        case_id=case_id,
        step_id=step_id,
        attempt=attempt,
        channel=channel,
        payload=ActionPayload(variables=variables),
        cost=Money(action_cost_paise, Currency.INR),
        due_at=_CLOCK.now(),
    )

    scheduled_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            ActionRow(
                id=action_id,
                case_id=case_id,
                step_id=step_id,
                attempt=attempt,
                channel=channel.value,
                idempotency_key=action.idempotency_key,
                payload=dict(variables),
                cost_paise=action_cost_paise,
                due_at=_CLOCK.now(),
            )
        )
        session.add(
            ScheduledActionRow(
                id=scheduled_id,
                action_id=action_id,
                case_id=case_id,
                due_at=_CLOCK.now(),
                status="claimed",
                claimed_by="worker-1",
                claimed_at=_CLOCK.now(),
                claim_expires_at=_CLOCK.now() + timedelta(minutes=5),
                attempts=1,
            )
        )
        if verdict is not None:
            session.add(
                PolicyDecisionRow(
                    id=uuid.uuid4(),
                    action_id=action_id,
                    attempt=attempt,
                    verdict=verdict,
                    rule_id=None if verdict == "allow" else "kill_switch_active",
                    inputs={},
                    defer_until=None,
                    decided_at=_CLOCK.now(),
                )
            )
        await session.commit()

    case = Case(
        id=case_id,
        signal_id=domain_case.signal_id,
        customer=customer,
        at_risk=domain_case.at_risk,
        state=domain_case.state,
        arm=domain_case.arm,
        opened_at=domain_case.opened_at,
        cost_spent=Money(cost_spent_paise, Currency.INR),
        cost_ceiling=Money(cost_ceiling_paise, Currency.INR),
    )
    return case, action, scheduled_id


async def _case_row(sessionmaker: async_sessionmaker[AsyncSession], case_id: uuid.UUID) -> CaseRow:
    async with sessionmaker() as session:
        result = await session.execute(select(CaseRow).where(CaseRow.id == case_id))
        row: CaseRow = result.scalar_one()
        return row


async def _scheduled_row(
    sessionmaker: async_sessionmaker[AsyncSession], scheduled_id: uuid.UUID
) -> ScheduledActionRow:
    async with sessionmaker() as session:
        result = await session.execute(
            select(ScheduledActionRow).where(ScheduledActionRow.id == scheduled_id)
        )
        row: ScheduledActionRow = result.scalar_one()
        return row


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


# --- A2.3: no ALLOW, no execution ------------------------------------------------


async def test_execute_raises_when_no_decision_is_recorded(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, action, scheduled_id = await _seed(
        sessionmaker, razorpay_customer_id="cust_no_decision", verdict=None
    )
    sim = RazorpaySimulator(seed=1)

    async with sessionmaker() as session:
        with pytest.raises(NoAllowDecisionError) as exc_info:
            await execute(
                session,
                redis_client,
                sim,
                _CLOCK,
                action=action,
                case=case,
                scheduled_action_id=scheduled_id,
            )

    assert exc_info.value.action_id == action.id
    assert exc_info.value.attempt == action.attempt


async def test_execute_raises_when_the_decision_is_a_deny(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case, action, scheduled_id = await _seed(
        sessionmaker, razorpay_customer_id="cust_deny", verdict="deny"
    )
    sim = RazorpaySimulator(seed=1)

    async with sessionmaker() as session:
        with pytest.raises(NoAllowDecisionError):
            await execute(
                session,
                redis_client,
                sim,
                _CLOCK,
                action=action,
                case=case,
                scheduled_action_id=scheduled_id,
            )


# --- happy path: executes, accounts cost, marks done -----------------------------


async def test_execute_calls_the_channel_records_cost_and_marks_done(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    sim = RazorpaySimulator(seed=1)
    original = _find_failed_payment(sim, retryable=True)

    case, action, scheduled_id = await _seed(
        sessionmaker,
        razorpay_customer_id="cust_happy_path",
        channel=Channel.PAYMENT_RETRY,
        action_cost_paise=0,
        cost_spent_paise=1_000,
        cost_ceiling_paise=100_000,
        payload_variables={"payment_id": original.id},
    )

    async with sessionmaker() as session:
        result = await execute(
            session,
            redis_client,
            sim,
            _CLOCK,
            action=action,
            case=case,
            scheduled_action_id=scheduled_id,
        )

    assert result.status == ExecutionStatus.EXECUTED
    assert result.channel_success is not None

    row = await _case_row(sessionmaker, case.id)
    assert row.cost_spent_paise == 1_000 + action.cost.paise

    scheduled = await _scheduled_row(sessionmaker, scheduled_id)
    assert scheduled.status == "done"

    assert await redis_client.exists(idempotency_key(action)) == 1

    # I4 (T2.9): seq 1-3 are case creation's own chain (`_seed` calls
    # `open_case_for_signal`); the execution itself is seq 4.
    audit = await _audit_rows(sessionmaker, case.id)
    assert [row.kind for row in audit] == [
        "signal_detected",
        "case_opened",
        "arm_assigned",
        "action_executed",
    ]
    assert audit[3].prev_hash == audit[2].hash
    assert audit[3].payload["channel"] == "payment_retry"
    assert audit[3].payload["dry_run"] is False


async def test_execute_calls_the_link_channel(engine: AsyncEngine, redis_client: Redis) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    sim = RazorpaySimulator(seed=2)
    case, action, scheduled_id = await _seed(
        sessionmaker, razorpay_customer_id="cust_link", channel=Channel.LINK, action_cost_paise=0
    )

    async with sessionmaker() as session:
        result = await execute(
            session,
            redis_client,
            sim,
            _CLOCK,
            action=action,
            case=case,
            scheduled_action_id=scheduled_id,
        )

    assert result.status == ExecutionStatus.EXECUTED
    assert result.channel_success is True


async def test_execute_calls_a_stubbed_messaging_channel_and_still_accounts_cost(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    sim = RazorpaySimulator(seed=3)
    case, action, scheduled_id = await _seed(
        sessionmaker,
        razorpay_customer_id="cust_sms",
        channel=Channel.SMS,
        action_cost_paise=18,
        cost_spent_paise=0,
    )

    async with sessionmaker() as session:
        result = await execute(
            session,
            redis_client,
            sim,
            _CLOCK,
            action=action,
            case=case,
            scheduled_action_id=scheduled_id,
        )

    assert result.status == ExecutionStatus.EXECUTED
    assert result.channel_success is True
    row = await _case_row(sessionmaker, case.id)
    assert row.cost_spent_paise == 18


# --- duplicate key: suppressed, never calls the channel twice --------------------


async def test_execute_suppresses_a_duplicate_call_with_the_same_idempotency_key(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    sim = RazorpaySimulator(seed=4)
    original = _find_failed_payment(sim, retryable=True)
    case, action, scheduled_id = await _seed(
        sessionmaker,
        razorpay_customer_id="cust_duplicate",
        channel=Channel.PAYMENT_RETRY,
        payload_variables={"payment_id": original.id},
    )

    before = await sim.list_payments(PaymentQuery(count=1000))

    async with sessionmaker() as session:
        first = await execute(
            session,
            redis_client,
            sim,
            _CLOCK,
            action=action,
            case=case,
            scheduled_action_id=scheduled_id,
        )
    async with sessionmaker() as session:
        second = await execute(
            session,
            redis_client,
            sim,
            _CLOCK,
            action=action,
            case=case,
            scheduled_action_id=scheduled_id,
        )

    after = await sim.list_payments(PaymentQuery(count=1000))

    assert first.status == ExecutionStatus.EXECUTED
    assert second.status == ExecutionStatus.SUPPRESSED
    assert second.channel_success is None
    assert len(after.items) == len(before.items) + 1  # the channel ran exactly once

    row = await _case_row(sessionmaker, case.id)
    assert row.cost_spent_paise == action.cost.paise  # not double-counted

    # The suppressed replay wrote no second `action_executed` -- the
    # channel did not run again, so there is nothing new to record.
    audit = await _audit_rows(sessionmaker, case.id)
    assert [row.kind for row in audit].count("action_executed") == 1


# --- crash mid-action: reclaimed, still not duplicated ----------------------------


async def test_a_reclaimed_action_is_suppressed_not_re_executed(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    """Simulates a worker that set the idempotency key and then crashed
    before finishing: the key survives in Redis (it has its own TTL,
    independent of the outbox), the scheduled action's claim expires and
    is reclaimed by a second worker, and that second worker's own
    `execute()` call must still not touch the channel a second time.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    sim = RazorpaySimulator(seed=5)
    original = _find_failed_payment(sim, retryable=True)
    case, action, scheduled_id = await _seed(
        sessionmaker,
        razorpay_customer_id="cust_crash",
        channel=Channel.PAYMENT_RETRY,
        payload_variables={"payment_id": original.id},
    )

    # The "crashed" first attempt got far enough to acquire the idempotency
    # key before dying -- simulated directly, since the point of this test
    # is what happens *after* the crash, not the crash itself.
    await redis_client.set(idempotency_key(action), "1", nx=True, ex=3600)

    # The claim expired and was reclaimed by a different worker.
    async with sessionmaker() as session:
        await session.execute(
            update(ScheduledActionRow)
            .where(ScheduledActionRow.id == scheduled_id)
            .values(claimed_by="worker-2", attempts=2)
        )
        await session.commit()

    before = await sim.list_payments(PaymentQuery(count=1000))

    async with sessionmaker() as session:
        result = await execute(
            session,
            redis_client,
            sim,
            _CLOCK,
            action=action,
            case=case,
            scheduled_action_id=scheduled_id,
        )

    after = await sim.list_payments(PaymentQuery(count=1000))

    assert result.status == ExecutionStatus.SUPPRESSED
    assert len(after.items) == len(before.items)  # the channel was never called

    scheduled = await _scheduled_row(sessionmaker, scheduled_id)
    assert scheduled.status == "done"  # reclaimed and resolved, not left dangling


# --- channel failure: marked failed, no cost added --------------------------------


async def test_a_channel_exception_marks_the_action_failed_without_adding_cost(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    sim = RazorpaySimulator(seed=6)
    case, action, scheduled_id = await _seed(
        sessionmaker,
        razorpay_customer_id="cust_channel_error",
        channel=Channel.PAYMENT_RETRY,
        action_cost_paise=50,
        payload_variables={},  # missing "payment_id" -- ChannelPayloadError
    )

    async with sessionmaker() as session:
        result = await execute(
            session,
            redis_client,
            sim,
            _CLOCK,
            action=action,
            case=case,
            scheduled_action_id=scheduled_id,
        )

    assert result.status == ExecutionStatus.FAILED
    assert result.channel_success is None

    row = await _case_row(sessionmaker, case.id)
    assert row.cost_spent_paise == 0  # a failed attempt never got as far as billing

    scheduled = await _scheduled_row(sessionmaker, scheduled_id)
    assert scheduled.status == "failed"
    assert scheduled.last_error is not None and "payment_id" in scheduled.last_error

    audit = await _audit_rows(sessionmaker, case.id)
    assert audit[3].kind == "action_failed"
    error = audit[3].payload["error"]
    assert isinstance(error, str) and "payment_id" in error


# --- dry-run: every stage except the channel call ---------------------------------


async def test_dry_run_never_touches_the_gateway_but_still_accounts_cost(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    sim = RazorpaySimulator(seed=7)
    case, action, scheduled_id = await _seed(
        sessionmaker,
        razorpay_customer_id="cust_dry_run",
        channel=Channel.PAYMENT_RETRY,
        action_cost_paise=0,
        payload_variables={},  # would raise for real -- proves the channel never ran
    )

    async with sessionmaker() as session:
        result = await execute(
            session,
            redis_client,
            sim,
            _CLOCK,
            action=action,
            case=case,
            scheduled_action_id=scheduled_id,
            dry_run=True,
        )

    assert result.status == ExecutionStatus.EXECUTED
    assert result.channel_success is True

    scheduled = await _scheduled_row(sessionmaker, scheduled_id)
    assert scheduled.status == "done"
