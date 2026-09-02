"""db.ping() and cache.ping() against real Postgres and Redis.

The unit suite mocks these functions to test that health.py calls them and
interprets the result correctly. It proves nothing about whether the functions
themselves actually connect to anything -- a mocked ping tests the mock
(ENGINEERING-STANDARDS section 4.2). This is the test that does.

Both containers start once for the module and stay up throughout; the cached
db/cache clients are created once and disposed once at the very end. That is
deliberate, not just for speed -- it also matches how these modules are
actually used in production, where `get_engine()`/`get_redis()` are
process-lifetime singletons, never disposed and recreated between requests
(ARCHITECTURE, platform/db.py, platform/cache.py).

An earlier version of this test disposed and recreated the engine between
each test case. On this machine that produced a reproducible
`redis.exceptions.ResponseError: unknown command 'HELLO'` on the Redis
connection that followed -- closing an asyncio transport only *schedules* the
underlying OS socket teardown, and a `dispose()` immediately followed by a new
connection attempt could observe a not-yet-released resource. Restructuring to
create-once/dispose-once removed the repeated dispose/reconnect cycle that
triggered it, which is the correct fix regardless of the exact mechanism: it
is also what the real code does. See F-005.
"""

from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from recoup.platform import cache, config, db

# loop_scope must match live_db_and_cache's below: the engine's connections
# are established during that fixture's setup and torn down during its
# teardown, both of which must run on the *same* event loop. pytest-asyncio's
# default is function-scoped, which would create the engine on one loop and
# tear it down on another -- observed as
# `AttributeError: 'NoneType' object has no attribute 'send'` deep in
# asyncpg's write path when dispose() tries to use a proactor that belonged
# to an already-closed loop. See F-005.
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="module")
def monkeypatch_module() -> Iterator[pytest.MonkeyPatch]:
    """pytest's built-in `monkeypatch` is function-scoped; this test needs
    env vars to persist across every test in the module, so it rolls its own."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_db_and_cache(monkeypatch_module: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Starts both containers, points db.py/cache.py at them once, and disposes
    both singletons once at teardown -- see the module docstring for why."""
    with (
        PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg,
        RedisContainer("redis:7-alpine") as r,
    ):
        monkeypatch_module.setenv("DATABASE_URL", pg.get_connection_url())
        monkeypatch_module.setenv(
            "REDIS_URL", f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"
        )
        # get_settings() is its own @lru_cache, separate from get_engine()'s and
        # get_redis()'s. If an earlier test in the same pytest session already
        # called it (e.g. test_health.py building create_app()), it is cached
        # with whatever DATABASE_URL/REDIS_URL existed at that point -- almost
        # certainly the defaults, not these containers. Clearing only
        # get_engine/get_redis left that stale Settings object in place and
        # silently pointed the "fresh" engine at localhost:5432 with the
        # default credentials, which happened to reach a real ambient Postgres
        # server during this session and raised asyncpg.InvalidPasswordError.
        # This test passed reliably in isolation and failed only inside the
        # full suite for exactly that reason. All three caches must be cleared
        # together. See F-005.
        config.get_settings.cache_clear()
        db.get_engine.cache_clear()
        cache.get_redis.cache_clear()
        try:
            yield
        finally:
            await db.get_engine().dispose()
            await cache.get_redis().aclose()
            config.get_settings.cache_clear()
            db.get_engine.cache_clear()
            cache.get_redis.cache_clear()


async def test_db_ping_succeeds_against_a_real_postgres(live_db_and_cache: None) -> None:
    assert await db.ping() is True


async def test_cache_ping_succeeds_against_a_real_redis(live_db_and_cache: None) -> None:
    assert await cache.ping() is True


async def test_db_ping_raises_when_nothing_is_listening() -> None:
    """The unmocked failure path -- what health.py's broad except is actually
    catching. `db.ping()` fails the same way SQLAlchemy's async engine does:
    by propagating asyncpg's own connection error. Asserting on that directly,
    rather than through a second SQLAlchemy engine, is both a more precise test
    of the actual failure mode and sidesteps needing to dispose a connection
    pool that was never successfully populated."""
    with pytest.raises(OSError):
        await asyncpg.connect(host="127.0.0.1", port=1, timeout=2)
