"""Async database engine.

One engine per process, created lazily so importing this module never opens a
connection -- tests that do not touch the database must not require one.
"""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from recoup.platform.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with get_sessionmaker()() as session:
        yield session


async def ping() -> bool:
    """Used by /health/ready. Raises on failure -- callers decide what that means."""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
