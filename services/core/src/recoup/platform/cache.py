"""Async Redis client.

Backs idempotency keys, rate limits, and the kill switch (ARCHITECTURE section 3).
One client per process, created lazily -- see db.py for why.
"""

from functools import lru_cache

from redis.asyncio import Redis

from recoup.platform.config import get_settings


@lru_cache
def get_redis() -> Redis:
    settings = get_settings()
    return Redis.from_url(settings.redis_url, decode_responses=True)


async def ping() -> bool:
    """Used by /health/ready. Raises on failure -- callers decide what that means."""
    return await get_redis().ping()
