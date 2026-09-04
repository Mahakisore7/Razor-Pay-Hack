"""The scheduler loop (T2.6): on each tick, reclaim any expired claims and
then claim newly-due work. Claiming is the loop's entire job -- nothing
here executes an action; a future executor (T2.7) is the caller that
would hand a claimed batch to a channel.

TR-25 requires the kill switch to take effect within one scheduler tick
(<=5s). That bound is a property of whatever `tick_interval` the caller
passes to `run_scheduler_loop`, not of this module -- the loop only paces
itself by it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from recoup.execution.outbox import DEFAULT_CLAIM_TTL, claim_due_batch, reclaim_expired_claims
from recoup.platform.clock import Clock
from recoup.platform.models import ScheduledActionRow

__all__ = ["run_scheduler_loop", "run_scheduler_tick"]


async def run_scheduler_tick(
    sessionmaker: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    worker_id: str,
    batch_size: int,
    claim_ttl: timedelta = DEFAULT_CLAIM_TTL,
) -> list[ScheduledActionRow]:
    """One reclaim-then-claim cycle, in its own session -- reclaiming
    first so a just-expired claim is immediately reclaimable in the same
    tick that noticed it, rather than waiting a full extra interval.
    """
    async with sessionmaker() as session:
        await reclaim_expired_claims(session, clock)
        return await claim_due_batch(
            session, clock, worker_id=worker_id, batch_size=batch_size, claim_ttl=claim_ttl
        )


async def run_scheduler_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    worker_id: str,
    batch_size: int,
    tick_interval: timedelta,
    claim_ttl: timedelta = DEFAULT_CLAIM_TTL,
    max_ticks: int | None = None,
) -> int:
    """Ticks forever (`max_ticks=None`, the real process boundary's call)
    or exactly `max_ticks` times (deterministic in tests). Returns the
    total number of rows claimed across every tick.
    """
    claimed_total = 0
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        batch = await run_scheduler_tick(
            sessionmaker, clock, worker_id=worker_id, batch_size=batch_size, claim_ttl=claim_ttl
        )
        claimed_total += len(batch)
        ticks += 1
        if max_ticks is None or ticks < max_ticks:
            await asyncio.sleep(tick_interval.total_seconds())
    return claimed_total
