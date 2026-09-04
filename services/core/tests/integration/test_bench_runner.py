"""Integration tests for `recoup.bench.runner.run_benchmark` (T3.5): the
first place the cohort generator (T3.1), arm assignment (T3.2), the
baseline/control arms (T3.3/T3.4), and Phase 2's closed loop all run
together, in simulated time, over a real Postgres and Redis.

Small cohorts throughout -- these are correctness tests, not the
2,000-case/10-minute performance target (TR-45), which is T3.8's own
concern (a dedicated CI job at a size actually chosen to bound runtime).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from testcontainers.community.redis import RedisContainer

from recoup.audit.events import Actor, ActorKind, AuditEvent, AuditKind
from recoup.audit.verify import verify_chain
from recoup.bench.cohort import generate_cohort, load_default_cohort_config
from recoup.bench.runner import run_benchmark
from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.diagnosis.engine import stub_diagnose
from recoup.domain.case import Arm, CaseState
from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import AuditEventId, CaseId, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.planning.planner import build_plan, select_playbook
from recoup.planning.playbooks.loader import load_playbooks
from recoup.planning.repository import persist_plan
from recoup.platform.clock import FrozenClock
from recoup.platform.models import AuditEventRow, BenchRun, CaseRow, ScheduledActionRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_START = datetime(2026, 4, 10, 0, 0, tzinfo=UTC)


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


async def test_run_benchmark_records_a_bench_run(engine: AsyncEngine, redis_client: Redis) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=101, size=30, start_at=_START)

    async with sessionmaker() as session:
        row = await session.get(BenchRun, summary.run_id)
    assert row is not None
    assert row.seed == 101
    assert row.started_at == _START
    assert row.completed_at is not None
    assert row.completed_at >= row.started_at


async def test_run_benchmark_opens_cases_across_all_three_arms(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    # A large enough cohort that all three arms (10/10/80 split) are
    # near-certain to draw at least one case each.
    summary = await run_benchmark(sessionmaker, redis_client, seed=202, size=150, start_at=_START)

    assert summary.cases_opened > 0
    assert summary.cases_by_arm[Arm.CONTROL.value] > 0
    assert summary.cases_by_arm[Arm.BASELINE.value] > 0
    assert summary.cases_by_arm[Arm.TREATMENT.value] > 0
    assert sum(summary.cases_by_arm.values()) == summary.cases_opened

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(CaseRow).where(CaseRow.bench_run_id == summary.run_id)))
            .scalars()
            .all()
        )
    assert len(rows) == summary.cases_opened


async def test_control_arm_cases_accumulate_zero_executed_actions(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    """I3, at the scale of a whole run: not one control-arm case out of
    however many the cohort drew has a single scheduled action row."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=303, size=150, start_at=_START)
    assert summary.cases_by_arm[Arm.CONTROL.value] > 0

    async with sessionmaker() as session:
        control_case_ids = (
            (
                await session.execute(
                    select(CaseRow.id).where(
                        CaseRow.bench_run_id == summary.run_id, CaseRow.arm == Arm.CONTROL.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert control_case_ids
        scheduled_rows = (
            (
                await session.execute(
                    select(ScheduledActionRow).where(
                        ScheduledActionRow.case_id.in_(control_case_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert scheduled_rows == []


async def test_treatment_and_baseline_cases_produce_real_execution_activity(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=404, size=150, start_at=_START)

    async with sessionmaker() as session:
        non_control_case_ids = (
            (
                await session.execute(
                    select(CaseRow.id).where(
                        CaseRow.bench_run_id == summary.run_id, CaseRow.arm != Arm.CONTROL.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert non_control_case_ids
        scheduled_rows = (
            (
                await session.execute(
                    select(ScheduledActionRow).where(
                        ScheduledActionRow.case_id.in_(non_control_case_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
    # At least the treatment/baseline plans were realised into scheduled
    # rows -- some may still be `done`/`failed` depending on due_at vs.
    # the run's own final clock position, but real rows must exist.
    assert scheduled_rows


async def test_some_retries_actually_recover_a_case(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    """Not a strict A3.7 statistical check (T3.6/T3.8's own job) -- just
    that the machinery genuinely closes the loop: at least one case in a
    150-case run reaches RECOVERED via a real, non-dry-run retry.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=505, size=150, start_at=_START)

    async with sessionmaker() as session:
        recovered = (
            (
                await session.execute(
                    select(CaseRow).where(
                        CaseRow.bench_run_id == summary.run_id,
                        CaseRow.state.in_(
                            (CaseState.RECOVERED.value, CaseState.PARTIALLY_RECOVERED.value)
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert recovered


async def test_every_case_has_a_gapless_verifying_audit_chain(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=606, size=60, start_at=_START)

    async with sessionmaker() as session:
        case_ids = (
            (
                await session.execute(
                    select(CaseRow.id).where(CaseRow.bench_run_id == summary.run_id)
                )
            )
            .scalars()
            .all()
        )
        assert case_ids
        for case_id in case_ids:
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
            events = [
                AuditEvent(
                    id=AuditEventId(row.id),
                    case_id=CaseId(row.case_id),
                    seq=row.seq,
                    kind=AuditKind(row.kind),
                    payload=row.payload,
                    actor=Actor(ActorKind(row.actor_type), row.actor_id),
                    trace_id=row.trace_id,
                    occurred_at=row.occurred_at,
                    prev_hash=row.prev_hash,
                )
                for row in rows
            ]
            assert (
                events
            )  # every opened case gets at least signal_detected/case_opened/arm_assigned
            assert verify_chain(events) is None


async def test_a_cohort_case_colliding_with_an_existing_open_case_is_skipped_not_crashed(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    """TR-8 (T2.2): one open case per (customer, at_risk_paise). A cohort
    is generated independently of whatever else already exists in the
    database, so a collision is possible in principle -- the runner must
    treat it as the same harmless no-op `open_case_for_signal` already
    does, not crash or lose the rest of the cohort.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    seed, size = 707, 5
    cohort = generate_cohort(load_default_cohort_config(), seed=seed, size=size, start_at=_START)
    colliding = cohort.cases[0]

    clock = FrozenClock(_START)
    async with sessionmaker() as session:
        customer = await resolve_customer(session, colliding.razorpay_customer_id)
        pre_existing = await open_case_for_signal(
            session,
            clock,
            seed,
            Signal(
                id=SignalId(uuid7()),
                leak_class=colliding.ground_truth.leak_class,
                customer=customer,
                at_risk=colliding.amount,
                detected_at=_START,
                source_event_ids=("pre-existing",),
                decline=colliding.ground_truth.decline_category,
                context=SignalContext(),
            ),
        )
    assert pre_existing is not None

    summary = await run_benchmark(sessionmaker, redis_client, seed=seed, size=size, start_at=_START)
    assert summary.cases_opened == size - 1  # the colliding cohort case was skipped, not crashed


async def test_a_scheduled_action_from_outside_this_run_is_ignored_not_executed(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    """`claim_due_batch` claims from `scheduled_actions` globally, not
    scoped to a `bench_run_id` -- a row belonging to a case this run
    never opened (a different run's leftover, or live traffic sharing
    the same database) must be claimed (so it isn't stuck `pending`
    forever) but left alone, not executed through this run's own
    in-memory action map.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    seed_at = _START - timedelta(days=1)
    clock = FrozenClock(seed_at)
    playbooks = load_playbooks()

    async with sessionmaker() as session:
        customer = await resolve_customer(session, "cust_foreign_action")
        case = await open_case_for_signal(
            session,
            clock,
            999,
            Signal(
                id=SignalId(uuid7()),
                leak_class=LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
                customer=customer,
                at_risk=Money(500_000, Currency.INR),
                detected_at=seed_at,
                source_event_ids=(f"foreign-{uuid.uuid4()}",),
                decline=DeclineCategory.INSUFFICIENT_FUNDS,
                context=SignalContext(),
            ),
        )
    assert case is not None
    case.arm = Arm.TREATMENT
    async with sessionmaker() as session:
        await session.execute(
            update(CaseRow).where(CaseRow.id == case.id).values(arm=Arm.TREATMENT.value)
        )
        await session.commit()

    diagnosis = stub_diagnose(case.id, DeclineCategory.INSUFFICIENT_FUNDS, clock)
    playbook = select_playbook(playbooks, diagnosis, LeakClass.L1_FAILED_ONE_TIME_PAYMENT)
    assert playbook is not None
    result = build_plan(case, playbook, clock)
    async with sessionmaker() as session:
        # due_at lands well before _START, so any due-batch claim inside
        # the benchmark run below sweeps this row up as a side effect.
        await persist_plan(session, clock, case=case, playbook=playbook, result=result)
        await session.commit()

    summary = await run_benchmark(sessionmaker, redis_client, seed=808, size=40, start_at=_START)
    assert summary.cases_opened > 0  # the run itself proceeded normally

    async with sessionmaker() as session:
        foreign_rows = (
            (
                await session.execute(
                    select(ScheduledActionRow).where(ScheduledActionRow.case_id == case.id)
                )
            )
            .scalars()
            .all()
        )
    # Claimed (so it is not stuck pending), but never marked done/failed --
    # this run's own executor never touched it.
    assert {row.status for row in foreign_rows} == {"claimed"}
