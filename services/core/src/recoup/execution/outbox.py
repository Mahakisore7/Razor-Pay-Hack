"""The durable outbox (DATA-MODEL SS3.3, TR-24): claim, reclaim, complete.

Claiming is one atomic `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE
SKIP LOCKED LIMIT :batch) RETURNING *` -- not a `SELECT` followed by a
separate `UPDATE` -- so that the row lock `SKIP LOCKED` takes is held for
the shortest possible window and no second statement round-trip can race
another worker between them. `SKIP LOCKED` is what lets many workers claim
disjoint batches without blocking each other; the `claim_expires_at` TTL
is what makes a worker crash recoverable (TR-56) -- an expired claim
returns to `pending` on the next `reclaim_expired_claims` call, and the
derived idempotency key (TR-23, T2.7's scope) is what keeps that reclaim
from producing a second side effect.

Nothing here executes an action -- claiming and completing are the whole
of this module's job. A future executor (T2.7) is the caller that hands a
claimed batch to a channel and reports back with `mark_done`/`mark_failed`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from recoup.audit.events import Actor, AuditKind
from recoup.audit.repository import record_event
from recoup.domain.identifiers import CaseId
from recoup.platform.clock import Clock
from recoup.platform.logging import current_trace_id
from recoup.platform.models import ActionRow, ScheduledActionRow

__all__ = [
    "DEFAULT_CLAIM_TTL",
    "claim_due_batch",
    "mark_done",
    "mark_failed",
    "reclaim_expired_claims",
]

DEFAULT_CLAIM_TTL = timedelta(minutes=5)


async def claim_due_batch(
    session: AsyncSession,
    clock: Clock,
    *,
    worker_id: str,
    batch_size: int,
    claim_ttl: timedelta = DEFAULT_CLAIM_TTL,
) -> list[ScheduledActionRow]:
    """Claims up to `batch_size` due, pending rows for `worker_id`, and
    returns exactly the rows this call claimed, oldest `due_at` first.

    The subquery's `ORDER BY due_at` picks *which* rows this call claims,
    but `UPDATE ... RETURNING` does not itself preserve that order -- the
    returned rows come back in whatever order Postgres's plan produces
    them, which is not due_at order. The explicit sort below is what
    actually delivers the "oldest first" contract this function promises.
    """
    now = clock.now()
    claimable_ids = (
        select(ScheduledActionRow.id)
        .where(ScheduledActionRow.status == "pending", ScheduledActionRow.due_at <= now)
        .order_by(ScheduledActionRow.due_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(ScheduledActionRow)
        .where(ScheduledActionRow.id.in_(claimable_ids))
        .values(
            status="claimed",
            claimed_by=worker_id,
            claimed_at=now,
            claim_expires_at=now + claim_ttl,
            attempts=ScheduledActionRow.attempts + 1,
        )
        .returning(ScheduledActionRow)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    claimed = sorted(result.scalars().all(), key=lambda row: row.due_at)
    await _record_claims(session, clock, claimed, worker_id=worker_id)
    await session.commit()
    return claimed


async def _record_claims(
    session: AsyncSession, clock: Clock, claimed: list[ScheduledActionRow], *, worker_id: str
) -> None:
    """I4: one `action_claimed` per row, in the same transaction the
    claim itself just landed in. A batch can span many cases, so the
    corresponding `actions` rows (for `channel`/`step_id` on the payload)
    are fetched once for the whole batch rather than one query per row.

    Each `record_event` call locks that case's audit chain tail
    (`recoup.audit.repository`), and two concurrent batches can share a
    case when its due actions land in different workers' claims. Writing
    in `case_id` order -- not `claimed`'s own `due_at` order -- keeps
    every worker acquiring those locks in the same relative order, which
    is what rules out a lock-cycle deadlock between them.
    """
    if not claimed:
        return
    action_rows = await session.execute(
        select(ActionRow).where(ActionRow.id.in_([row.action_id for row in claimed]))
    )
    actions_by_id = {row.id: row for row in action_rows.scalars()}
    trace_id = current_trace_id()
    now = clock.now()
    for row in sorted(claimed, key=lambda r: str(r.case_id)):
        action = actions_by_id[row.action_id]
        await record_event(
            session,
            case_id=CaseId(row.case_id),
            kind=AuditKind.ACTION_CLAIMED,
            payload={
                "action_id": str(row.action_id),
                "channel": action.channel,
                "step_id": action.step_id,
                "attempt": row.attempts,
                "worker_id": worker_id,
            },
            actor=Actor.scheduler(),
            trace_id=trace_id,
            occurred_at=now,
        )


async def reclaim_expired_claims(session: AsyncSession, clock: Clock) -> int:
    """Returns every `claimed` row whose TTL has passed to `pending`, and
    reports how many it reclaimed."""
    now = clock.now()
    reclaimable_ids = (
        select(ScheduledActionRow.id)
        .where(ScheduledActionRow.status == "claimed", ScheduledActionRow.claim_expires_at < now)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(ScheduledActionRow)
        .where(ScheduledActionRow.id.in_(reclaimable_ids))
        .values(status="pending", claimed_by=None, claimed_at=None, claim_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    await session.commit()
    return cast("CursorResult[Any]", result).rowcount


async def mark_done(session: AsyncSession, clock: Clock, scheduled_action_id: UUID) -> bool:
    """Only transitions a row that is still `claimed` -- if its TTL
    already expired and another worker reclaimed it in the meantime, this
    returns `False` rather than overwriting whatever that worker is now
    doing with it.

    `clock.now()` becomes `executed_at` -- attribution's 72-hour window
    (T2.8, METRICS-AND-KPIS SS6) anchors to it, so it is captured here,
    the one place a scheduled action actually completes, rather than
    reconstructed later from `due_at` (the *planned* time, not the
    actual one).
    """
    return await _complete(session, scheduled_action_id, status="done", executed_at=clock.now())


async def mark_failed(session: AsyncSession, scheduled_action_id: UUID, *, error: str) -> bool:
    return await _complete(session, scheduled_action_id, status="failed", last_error=error)


async def _complete(
    session: AsyncSession,
    scheduled_action_id: UUID,
    *,
    status: str,
    last_error: str | None = None,
    executed_at: datetime | None = None,
) -> bool:
    stmt = (
        update(ScheduledActionRow)
        .where(
            ScheduledActionRow.id == scheduled_action_id,
            ScheduledActionRow.status == "claimed",
        )
        .values(status=status, last_error=last_error, executed_at=executed_at)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    await session.commit()
    return cast("CursorResult[Any]", result).rowcount > 0
