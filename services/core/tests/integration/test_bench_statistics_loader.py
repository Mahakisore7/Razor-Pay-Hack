"""Integration tests for `recoup.bench.statistics.load_case_outcomes`
(T3.6): the repository half that assembles `CaseOutcome`s from a real
benchmark run's own rows -- everything downstream of it
(`recoup.bench.statistics`'s pure functions) is tested without a
database in `tests/unit/test_bench_statistics.py`.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from testcontainers.community.redis import RedisContainer

from recoup.bench.runner import run_benchmark
from recoup.bench.statistics import compute_statistics, load_case_outcomes
from recoup.domain.case import Arm

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_START = datetime(2026, 4, 10, tzinfo=UTC)


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


async def test_load_case_outcomes_returns_one_row_per_opened_case(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=901, size=150, start_at=_START)

    async with sessionmaker() as session:
        outcomes = await load_case_outcomes(session, summary.run_id)

    assert len(outcomes) == summary.cases_opened
    assert {o.arm for o in outcomes} == set(Arm)


async def test_load_case_outcomes_control_cases_have_zero_cost_and_no_contacts(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=902, size=150, start_at=_START)

    async with sessionmaker() as session:
        outcomes = await load_case_outcomes(session, summary.run_id)

    control = [o for o in outcomes if o.arm is Arm.CONTROL]
    assert control
    assert all(o.cost.paise == 0 for o in control)
    assert all(o.contact_events == () for o in control)


async def test_load_case_outcomes_feeds_compute_statistics_end_to_end(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    """The whole T3.6 pipeline, one real run to compute_statistics's
    output -- not a strict A3.7 statistical check (that needs a run
    large enough for significance), just that loader output shape
    matches what the pure statistics functions expect.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    seed = 903
    summary = await run_benchmark(sessionmaker, redis_client, seed=seed, size=150, start_at=_START)

    async with sessionmaker() as session:
        outcomes = await load_case_outcomes(session, summary.run_id)

    stats = compute_statistics(outcomes, rng_seed=seed, bootstrap_resamples=200)
    assert set(stats.per_arm) == set(Arm)
    assert stats.per_arm[Arm.CONTROL].case_count > 0
    # Baseline's dunning_email is the only currently-costed step this
    # phase -- a strictly-positive total confirms real, policy-allowed
    # ActionRow.cost_paise values reached the loader, not just zeros
    # throughout (this caught a real bug: before the runner seeded
    # consent and kept `Case.cost_ceiling`/`cost_spent` in sync, R4 and
    # then R8 denied every email step, and this total was always 0).
    assert stats.economics.total_cost.paise > 0
