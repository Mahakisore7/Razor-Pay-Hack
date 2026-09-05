"""T3.8 (PHASE-03-measurement.md), A3.2's own phase gate: two runs of the
*same* benchmark seed must produce byte-identical summaries -- "a
benchmark a reviewer cannot re-run is not evidence."

Deliberately uses two fully isolated Postgres + Redis instances, not two
calls against one shared database like every other bench integration
test in this suite: TR-8 dedup (one open case per (customer,
at_risk_paise)) is *global*, not scoped to a bench run, so re-running the
identical cohort against a database that still has the first run's cases
open would mostly collide with them and open almost nothing the second
time -- a real, correct product behaviour (a customer can't have two open
cases at once), but it would confound "does the same seed reproduce" with
"does dedup correctly prevent double-representation" (already covered by
`test_detection_pipeline.py`/`test_bench_runner.py`'s own dedup tests).
Isolating the two runs is what actually answers the reproducibility
question this file exists to answer.

200 cases (not the 2,000-case/10-minute TR-45 target) -- this is a
correctness gate, sized to bound CI runtime, per PHASE-03's own note on
its other integration tests.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from recoup.bench.runner import BenchmarkRunSummary, run_benchmark
from recoup.bench.statistics import compute_statistics, load_case_outcomes

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_START = datetime(2026, 4, 10, tzinfo=UTC)
_SEED = 42
_SIZE = 200
_SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _start_migrated_postgres() -> tuple[PostgresContainer, str]:
    """The blocking half (container start + `alembic upgrade` subprocess)
    -- run off the event loop via `asyncio.to_thread` by `_isolated_
    engine` below (ASYNC221), the same way `conftest.py`'s own
    `migrated_database_url` gets away with blocking calls by being a
    plain sync fixture rather than an `async def` one.
    """
    container = PostgresContainer("postgres:16-alpine", driver="asyncpg")
    container.start()
    url = container.get_connection_url()
    uv_path = shutil.which("uv")
    assert uv_path is not None, "uv must be on PATH to run this integration test"
    subprocess.run(  # noqa: S603 -- uv_path resolved via shutil.which, args hardcoded
        [uv_path, "run", "alembic", "upgrade", "head"],
        cwd=_SERVICE_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
        text=True,
    )
    return container, url


@asynccontextmanager
async def _isolated_engine() -> AsyncIterator[AsyncEngine]:
    """A fresh, migrated Postgres of its own -- this file's whole point
    is comparing two *independent* runs, so it cannot reuse the module-
    scoped `engine` fixture every other integration test file shares
    (mirrors `conftest.py`'s own `migrated_database_url`/`engine` pair,
    duplicated rather than parametrised since a fixture can't easily
    hand back two live instances to one test at once).
    """
    container, url = await asyncio.to_thread(_start_migrated_postgres)
    try:
        engine = create_async_engine(url)
        try:
            yield engine
        finally:
            await engine.dispose()
    finally:
        await asyncio.to_thread(container.stop)


@asynccontextmanager
async def _isolated_redis() -> AsyncIterator[Redis]:
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


async def _run(engine: AsyncEngine, redis: Redis) -> BenchmarkRunSummary:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return await run_benchmark(sessionmaker, redis, seed=_SEED, size=_SIZE, start_at=_START)


async def test_two_isolated_runs_of_the_same_seed_produce_byte_identical_summaries() -> None:
    """A3.2's own phase gate, at the level of `BenchmarkRunSummary` --
    `run_id` is the one field expected to differ (a fresh, globally-
    unique row identity, by design; DATA-MODEL SS7 retains every run
    indefinitely specifically so more than one can exist for the same
    seed), everything else must match exactly.
    """
    async with (
        _isolated_engine() as engine1,
        _isolated_engine() as engine2,
        _isolated_redis() as redis1,
        _isolated_redis() as redis2,
    ):
        summary1 = await _run(engine1, redis1)
        summary2 = await _run(engine2, redis2)

    assert summary1.run_id != summary2.run_id
    assert summary1.seed == summary2.seed == _SEED
    assert summary1.size == summary2.size == _SIZE
    assert summary1.started_at == summary2.started_at
    assert summary1.finished_at == summary2.finished_at
    assert summary1.cases_opened == summary2.cases_opened
    assert summary1.cases_by_arm == summary2.cases_by_arm


async def test_two_isolated_runs_of_the_same_seed_produce_byte_identical_statistics() -> None:
    """The deeper claim A3.2's prose actually cares about: not just the
    case counts, but the headline numbers a reviewer would read out of
    the report -- amount-weighted recovery rates, the incremental
    comparisons (including their bootstrap cross-check), economics, and
    guardrails -- must reproduce exactly, not just approximately.
    `BenchmarkStatistics` and everything nested in it are plain frozen
    dataclasses, so `==` is a real structural comparison, not identity.
    """
    async with (
        _isolated_engine() as engine1,
        _isolated_engine() as engine2,
        _isolated_redis() as redis1,
        _isolated_redis() as redis2,
    ):
        summary1 = await _run(engine1, redis1)
        summary2 = await _run(engine2, redis2)

        sessionmaker1 = async_sessionmaker(engine1, expire_on_commit=False)
        sessionmaker2 = async_sessionmaker(engine2, expire_on_commit=False)
        async with sessionmaker1() as session:
            cases1 = await load_case_outcomes(session, summary1.run_id)
        async with sessionmaker2() as session:
            cases2 = await load_case_outcomes(session, summary2.run_id)

    stats1 = compute_statistics(cases1, rng_seed=_SEED)
    stats2 = compute_statistics(cases2, rng_seed=_SEED)
    assert stats1 == stats2
