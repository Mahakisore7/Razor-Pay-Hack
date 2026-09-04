"""Integration tests for `recoup.execution.scheduler` (T2.6) against a
real, migrated Postgres.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.domain.action import Channel
from recoup.domain.identifiers import CaseId, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.execution.outbox import claim_due_batch
from recoup.execution.scheduler import run_scheduler_loop, run_scheduler_tick
from recoup.platform.clock import FrozenClock
from recoup.platform.models import ActionRow, ScheduledActionRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_outbox_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """See `test_outbox.py`'s fixture of the same name -- `engine` is
    module-scoped, so without this, one test's `claimed` rows are fair
    game for a later test's own reclaim call."""
    async with engine.begin() as conn:
        await conn.execute(delete(ScheduledActionRow))
        await conn.execute(delete(ActionRow))
    yield


async def _seed_case(
    sessionmaker: async_sessionmaker[AsyncSession], razorpay_customer_id: str
) -> CaseId:
    async with sessionmaker() as session:
        customer = await resolve_customer(session, razorpay_customer_id)
        signal = Signal(
            id=SignalId(uuid7()),
            leak_class=LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
            customer=customer,
            at_risk=Money(100_000, Currency.INR),
            detected_at=_CLOCK.now(),
            source_event_ids=(f"evt-{uuid.uuid4()}",),
            decline=None,
            context=SignalContext(),
        )
        case = await open_case_for_signal(session, _CLOCK, seed=1, signal=signal)
    assert case is not None
    return case.id


async def _seed_scheduled_actions(
    sessionmaker: async_sessionmaker[AsyncSession], case_id: CaseId, *, count: int, due_at: datetime
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with sessionmaker() as session:
        for i in range(count):
            action_id = uuid.uuid4()
            session.add(
                ActionRow(
                    id=action_id,
                    case_id=case_id,
                    step_id=f"step-{uuid.uuid4()}-{i}",
                    attempt=1,
                    channel=Channel.PAYMENT_RETRY.value,
                    idempotency_key=f"idem-{uuid.uuid4()}",
                    payload={},
                    cost_paise=0,
                    due_at=due_at,
                )
            )
            scheduled_id = uuid.uuid4()
            session.add(
                ScheduledActionRow(
                    id=scheduled_id, action_id=action_id, case_id=case_id, due_at=due_at
                )
            )
            ids.append(scheduled_id)
        await session.commit()
    return ids


async def test_run_scheduler_tick_claims_due_work(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_tick_basic")
    ids = await _seed_scheduled_actions(sessionmaker, case_id, count=3, due_at=_CLOCK.now())

    claimed = await run_scheduler_tick(sessionmaker, _CLOCK, worker_id="worker-1", batch_size=10)

    assert {row.id for row in claimed} == set(ids)
    assert all(row.status == "claimed" for row in claimed)


async def test_run_scheduler_tick_reclaims_expired_claims_before_claiming(
    engine: AsyncEngine,
) -> None:
    """Reclaim-then-claim within one tick: a claim that just expired must
    be immediately claimable again in the same tick that notices it."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_tick_reclaim")
    (scheduled_id,) = await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now()
    )
    async with sessionmaker() as session:
        await claim_due_batch(
            session, _CLOCK, worker_id="worker-1", batch_size=10, claim_ttl=timedelta(minutes=1)
        )

    later = FrozenClock(_CLOCK.now() + timedelta(minutes=5))
    claimed = await run_scheduler_tick(sessionmaker, later, worker_id="worker-2", batch_size=10)

    assert [row.id for row in claimed] == [scheduled_id]
    assert claimed[0].claimed_by == "worker-2"
    assert claimed[0].attempts == 2  # incremented on both the original claim and the reclaim


async def test_run_scheduler_loop_claims_across_multiple_ticks(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_loop_multi_tick")
    await _seed_scheduled_actions(sessionmaker, case_id, count=25, due_at=_CLOCK.now())

    total_claimed = await run_scheduler_loop(
        sessionmaker,
        _CLOCK,
        worker_id="worker-1",
        batch_size=10,
        tick_interval=timedelta(seconds=0),
        max_ticks=3,
    )

    assert total_claimed == 25  # 10 + 10 + 5, the pool exhausted on the third tick

    async with sessionmaker() as session:
        result = await session.execute(
            select(ScheduledActionRow).where(ScheduledActionRow.case_id == case_id)
        )
        rows = result.scalars().all()
    assert all(row.status == "claimed" for row in rows)
    assert all(row.claimed_by == "worker-1" for row in rows)


async def test_run_scheduler_loop_stops_after_max_ticks_with_nothing_left_to_claim(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_loop_empty")
    await _seed_scheduled_actions(sessionmaker, case_id, count=2, due_at=_CLOCK.now())

    total_claimed = await run_scheduler_loop(
        sessionmaker,
        _CLOCK,
        worker_id="worker-1",
        batch_size=10,
        tick_interval=timedelta(seconds=0),
        max_ticks=3,
    )

    assert total_claimed == 2  # everything claimed on tick 1; ticks 2 and 3 find nothing due
