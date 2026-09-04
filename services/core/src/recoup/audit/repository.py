"""Persists `AuditEvent`s (T2.9, DOMAIN-MODEL SS10): looks up a case's
current chain tail and appends the next event -- via `session.add`, never
`session.commit` -- so the audit row lands in whatever transaction the
caller is already in. I4 ("every state transition writes exactly one
audit event, in the same transaction as the transition") is enforced by
convention here, not by this module: a caller records the event and then
commits its own write, the same commit, not two.

PII masking (`recoup.platform.logging.redact_pii`) runs on every payload
here unconditionally -- a caller cannot forget it, the same reasoning
`platform/logging.py` gives for redacting log lines regardless of what
any one call site remembers to do.

Concurrency: `record_event` locks the case's own row (`cases`, `FOR
UPDATE`) before reading the chain's current tail, so two transactions
appending to the *same* case serialise instead of both computing the
same next `seq` and one losing to `audit_seq_unique` at commit. Locking
the tail *audit_events* row instead does not work -- it was tried first,
and still raced `test_concurrent_workers_never_double_claim` (many
workers, one case, each claim writing `action_claimed`): `FOR UPDATE`
only blocks a second transaction that wants to lock the *same, already-
existing* row, and a concurrent INSERT of a brand-new higher-`seq` row
is invisible to that lock entirely -- there is no existing row for it to
contend on. The `cases` row is what both transactions can agree to
contend on, since it exists (this module never writes a case's first
audit event before the case row that owns it is itself flushed) and
never changes shape.

A caller that writes events for *more than one* case in the same
transaction (T2.9's outbox batch claim, attribution's winner-plus-losers)
must still take its own care to lock those cases in a consistent order
across callers, or two transactions touching an overlapping set of cases
in opposite orders can deadlock on these same row locks -- see the
call sites for how each avoids it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recoup.audit.events import Actor, ActorKind, AuditEvent, AuditKind, append_event
from recoup.domain.identifiers import AuditEventId, CaseId, uuid7
from recoup.platform.logging import redact_pii
from recoup.platform.models import AuditEventRow, CaseRow

__all__ = ["record_event"]


async def _latest(session: AsyncSession, case_id: CaseId) -> AuditEvent | None:
    result = await session.execute(
        select(AuditEventRow)
        .where(AuditEventRow.case_id == case_id)
        .order_by(AuditEventRow.seq.desc())
        .limit(1)
    )
    row = result.scalars().first()
    if row is None:
        return None
    return AuditEvent(
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


async def record_event(
    session: AsyncSession,
    *,
    case_id: CaseId,
    kind: AuditKind,
    payload: Mapping[str, Any],
    actor: Actor,
    trace_id: str,
    occurred_at: datetime,
) -> AuditEvent:
    """Appends the next event in `case_id`'s chain and stages it on
    `session`, flushing (never committing) so that a second call for the
    same case within the same still-open transaction sees this one --
    the `_latest` lookup is a plain `SELECT`, and an unflushed `add` is
    invisible to it otherwise. A caller emitting several events for one
    case in a row therefore gets a correctly gapless `seq` without
    needing to flush itself.

    The `cases` row lock taken first is released only at the caller's
    own commit/rollback, exactly matching the audit write's own
    lifetime -- a second `record_event` for a different case never waits
    on it, and a third call for the *same* case in the same transaction
    re-acquires a lock this transaction already holds, which Postgres
    grants immediately.
    """
    await session.execute(select(CaseRow.id).where(CaseRow.id == case_id).with_for_update())
    prior = await _latest(session, case_id)
    event = append_event(
        prior,
        id=AuditEventId(uuid7()),
        case_id=case_id,
        kind=kind,
        payload=redact_pii(payload),
        actor=actor,
        trace_id=trace_id,
        occurred_at=occurred_at,
    )
    session.add(
        AuditEventRow(
            id=event.id,
            case_id=event.case_id,
            seq=event.seq,
            kind=event.kind.value,
            payload=dict(event.payload),
            actor_type=event.actor.kind.value,
            actor_id=event.actor.user_id,
            trace_id=event.trace_id,
            occurred_at=event.occurred_at,
            prev_hash=event.prev_hash,
            hash=event.hash,
        )
    )
    await session.flush()
    return event
