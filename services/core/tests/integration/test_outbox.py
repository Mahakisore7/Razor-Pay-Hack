"""Integration tests for `recoup.execution.outbox` (T2.6) against a real,
migrated Postgres -- `FOR UPDATE SKIP LOCKED` correctness is a database
behaviour no mock can prove (the same reasoning `test_webhook_ingestion.py`
applies to `ON CONFLICT DO NOTHING`, and `test_detection_pipeline.py` to
`cases_open_dedup`).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.domain.action import Channel
from recoup.domain.identifiers import CaseId, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.execution.outbox import claim_due_batch, mark_done, mark_failed, reclaim_expired_claims
from recoup.platform.clock import FrozenClock
from recoup.platform.models import ActionRow, ScheduledActionRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_outbox_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """`engine` is module-scoped -- every test in this file shares one
    Postgres instance. Without this, a row an earlier test left `claimed`
    (a transient state a real worker would eventually resolve, but no test
    here has an executor to do that) is fair game for a *later* test's own
    `reclaim_expired_claims` call, since reclaiming is correctly global
    (no case or test scoping) and different tests advance their clocks by
    different amounts to simulate TTL expiry. That cross-test reclaim has
    nothing to do with the behaviour under test, so each test starts from
    a table with no rows left over from any other.
    """
    async with engine.begin() as conn:
        await conn.execute(delete(ScheduledActionRow))
        await conn.execute(delete(ActionRow))
    yield


_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
SessionmakerT = async_sessionmaker[AsyncSession]


async def _seed_case(sessionmaker: SessionmakerT, razorpay_customer_id: str) -> CaseId:
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
    sessionmaker: SessionmakerT, case_id: CaseId, *, count: int, due_at: datetime
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with sessionmaker() as session:
        for i in range(count):
            action_id = uuid.uuid4()
            session.add(
                ActionRow(
                    id=action_id,
                    case_id=case_id,
                    step_id=f"step-{i}-{uuid.uuid4()}",
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


async def _fetch(sessionmaker: SessionmakerT, scheduled_id: uuid.UUID) -> ScheduledActionRow:
    async with sessionmaker() as session:
        result = await session.execute(
            select(ScheduledActionRow).where(ScheduledActionRow.id == scheduled_id)
        )
        row: ScheduledActionRow = result.scalar_one()
        return row


# --- claim_due_batch -------------------------------------------------------------


async def test_claim_due_batch_claims_a_due_pending_row(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_claim_basic")
    (scheduled_id,) = await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now()
    )

    async with sessionmaker() as session:
        claimed = await claim_due_batch(session, _CLOCK, worker_id="worker-1", batch_size=10)

    assert [row.id for row in claimed] == [scheduled_id]
    assert claimed[0].status == "claimed"
    assert claimed[0].claimed_by == "worker-1"
    assert claimed[0].claimed_at == _CLOCK.now()
    assert claimed[0].claim_expires_at == _CLOCK.now() + timedelta(minutes=5)
    assert claimed[0].attempts == 1


async def test_claim_due_batch_ignores_rows_not_yet_due(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_not_due")
    await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now() + timedelta(hours=1)
    )

    async with sessionmaker() as session:
        claimed = await claim_due_batch(session, _CLOCK, worker_id="worker-1", batch_size=10)

    assert claimed == []


async def test_claim_due_batch_does_not_reclaim_an_already_claimed_row(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_already_claimed")
    (scheduled_id,) = await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now()
    )
    async with sessionmaker() as session:
        await claim_due_batch(session, _CLOCK, worker_id="worker-1", batch_size=10)

    async with sessionmaker() as session:
        second = await claim_due_batch(session, _CLOCK, worker_id="worker-2", batch_size=10)

    assert second == []
    row = await _fetch(sessionmaker, scheduled_id)
    assert row.claimed_by == "worker-1"  # unchanged by worker-2's no-op claim attempt


async def test_claim_due_batch_respects_batch_size_and_due_at_order(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_batch_order")
    ids: list[uuid.UUID] = []
    async with sessionmaker() as session:
        for i in range(5):
            action_id = uuid.uuid4()
            due_at = _CLOCK.now() - timedelta(minutes=5 - i)  # earlier i => earlier due_at
            session.add(
                ActionRow(
                    id=action_id,
                    case_id=case_id,
                    step_id=f"order-{i}",
                    attempt=1,
                    channel=Channel.PAYMENT_RETRY.value,
                    idempotency_key=f"idem-order-{i}",
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

    async with sessionmaker() as session:
        claimed = await claim_due_batch(session, _CLOCK, worker_id="worker-1", batch_size=3)

    assert [row.id for row in claimed] == ids[:3]  # oldest due_at first


async def test_claim_due_batch_increments_attempts_on_each_claim(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_attempts")
    (scheduled_id,) = await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now()
    )
    async with sessionmaker() as session:
        first = await claim_due_batch(session, _CLOCK, worker_id="worker-1", batch_size=10)
    assert first[0].attempts == 1

    # simulate expiry and reclaim, then claim again
    async with sessionmaker() as session:
        await reclaim_expired_claims(session, FrozenClock(_CLOCK.now() + timedelta(hours=1)))
    async with sessionmaker() as session:
        second = await claim_due_batch(session, _CLOCK, worker_id="worker-2", batch_size=10)

    assert second[0].id == scheduled_id
    assert second[0].attempts == 2


# --- reclaim_expired_claims --------------------------------------------------------


async def test_reclaim_expired_claims_returns_an_expired_claim_to_pending(
    engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_reclaim")
    (scheduled_id,) = await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now()
    )
    async with sessionmaker() as session:
        await claim_due_batch(
            session, _CLOCK, worker_id="worker-1", batch_size=10, claim_ttl=timedelta(minutes=1)
        )

    later = FrozenClock(_CLOCK.now() + timedelta(minutes=5))
    async with sessionmaker() as session:
        reclaimed_count = await reclaim_expired_claims(session, later)

    assert reclaimed_count == 1
    row = await _fetch(sessionmaker, scheduled_id)
    assert row.status == "pending"
    assert row.claimed_by is None
    assert row.claimed_at is None
    assert row.claim_expires_at is None


async def test_reclaim_expired_claims_ignores_claims_still_within_ttl(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_reclaim_within_ttl")
    (scheduled_id,) = await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now()
    )
    async with sessionmaker() as session:
        await claim_due_batch(
            session, _CLOCK, worker_id="worker-1", batch_size=10, claim_ttl=timedelta(minutes=10)
        )

    async with sessionmaker() as session:
        reclaimed_count = await reclaim_expired_claims(session, _CLOCK)

    assert reclaimed_count == 0
    row = await _fetch(sessionmaker, scheduled_id)
    assert row.status == "claimed"


async def test_reclaimed_work_is_claimable_again(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_reclaim_then_claim")
    (scheduled_id,) = await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now()
    )
    async with sessionmaker() as session:
        await claim_due_batch(
            session, _CLOCK, worker_id="worker-1", batch_size=10, claim_ttl=timedelta(minutes=1)
        )

    later = FrozenClock(_CLOCK.now() + timedelta(minutes=5))
    async with sessionmaker() as session:
        await reclaim_expired_claims(session, later)
    async with sessionmaker() as session:
        reclaimed_batch = await claim_due_batch(session, later, worker_id="worker-2", batch_size=10)

    assert [row.id for row in reclaimed_batch] == [scheduled_id]
    assert reclaimed_batch[0].claimed_by == "worker-2"


# --- mark_done / mark_failed --------------------------------------------------------


async def test_mark_done_transitions_a_claimed_row_to_done(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_mark_done")
    (scheduled_id,) = await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now()
    )
    async with sessionmaker() as session:
        await claim_due_batch(session, _CLOCK, worker_id="worker-1", batch_size=10)

    async with sessionmaker() as session:
        updated = await mark_done(session, _CLOCK, scheduled_id)

    assert updated is True
    row = await _fetch(sessionmaker, scheduled_id)
    assert row.status == "done"
    assert row.executed_at == _CLOCK.now()


async def test_mark_done_returns_false_when_the_row_is_not_claimed(engine: AsyncEngine) -> None:
    case_id = await _seed_case(
        async_sessionmaker(engine, expire_on_commit=False), "cust_mark_done_pending"
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    (scheduled_id,) = await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now()
    )
    # never claimed -- still "pending"

    async with sessionmaker() as session:
        updated = await mark_done(session, _CLOCK, scheduled_id)

    assert updated is False
    row = await _fetch(sessionmaker, scheduled_id)
    assert row.status == "pending"
    assert row.executed_at is None


async def test_mark_failed_records_the_error_and_transitions_to_failed(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    case_id = await _seed_case(sessionmaker, "cust_mark_failed")
    (scheduled_id,) = await _seed_scheduled_actions(
        sessionmaker, case_id, count=1, due_at=_CLOCK.now()
    )
    async with sessionmaker() as session:
        await claim_due_batch(session, _CLOCK, worker_id="worker-1", batch_size=10)

    async with sessionmaker() as session:
        updated = await mark_failed(session, scheduled_id, error="gateway timeout")

    assert updated is True
    row = await _fetch(sessionmaker, scheduled_id)
    assert row.status == "failed"
    assert row.last_error == "gateway timeout"


# --- concurrency: N workers, disjoint claims, no double-claim ----------------------


async def test_concurrent_workers_never_double_claim(migrated_database_url: str) -> None:
    """TR-24, PHASE-02 A2.5: many workers claiming from the same pending
    pool must end up with disjoint batches that partition it exactly. A
    dedicated, wider-pooled engine is used here (not the shared module
    fixture) so `batch_size` workers truly run concurrent transactions
    rather than queueing for a handful of pooled connections."""
    concurrent_engine = create_async_engine(migrated_database_url, pool_size=12, max_overflow=0)
    try:
        sessionmaker = async_sessionmaker(concurrent_engine, expire_on_commit=False)
        case_id = await _seed_case(sessionmaker, "cust_concurrency")
        scheduled_ids = await _seed_scheduled_actions(
            sessionmaker, case_id, count=50, due_at=_CLOCK.now()
        )

        async def _claim(worker_id: str) -> Sequence[uuid.UUID]:
            async with sessionmaker() as session:
                claimed = await claim_due_batch(session, _CLOCK, worker_id=worker_id, batch_size=20)
                return [row.id for row in claimed]

        results = await asyncio.gather(*(_claim(f"worker-{i}") for i in range(5)))
    finally:
        await concurrent_engine.dispose()

    all_claimed = [row_id for batch in results for row_id in batch]
    assert len(all_claimed) == 50
    assert len(set(all_claimed)) == 50  # no id claimed twice -- no double-claim
    assert set(all_claimed) == set(scheduled_ids)
