"""Shared fixtures for tests that need a real, migrated Postgres.

Split out once a second integration test file needed the exact same
non-trivial setup -- a duplicated `subprocess.run(["alembic", ...])` call
is two copies of the F-005 lesson (see `migrated_database_url`'s
docstring) to keep in sync instead of one.
"""

import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.community.postgres import PostgresContainer

_SERVICE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def migrated_database_url() -> Iterator[str]:
    """Starts Postgres and applies the real Alembic migration -- not
    `Base.metadata.create_all()`, which would skip the hand-written
    triggers that exist only in the migration, not in the SQLAlchemy
    metadata. Runs `alembic upgrade` as a subprocess deliberately:
    invoking it in-process would nest its own `asyncio.run()` inside
    pytest-asyncio's already-running loop (see F-005 in FAILURE-LOG for
    this codebase's prior history with exactly that class of bug).
    """
    uv_path = shutil.which("uv")
    assert uv_path is not None, "uv must be on PATH to run this integration test"
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        subprocess.run(  # noqa: S603 -- uv_path is resolved via shutil.which, args are hardcoded
            [uv_path, "run", "alembic", "upgrade", "head"],
            cwd=_SERVICE_ROOT,
            env={**os.environ, "DATABASE_URL": url},
            check=True,
            capture_output=True,
            text=True,
        )
        yield url


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database_url)
    try:
        yield eng
    finally:
        await eng.dispose()
