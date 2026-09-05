"""Persists a `PolicyDecision` (T2.10): the caller `policy/engine.py`
itself never had, since `evaluate` is pure (no session -- POLICY-ENGINE
SS1's replay guarantee depends on it staying that way). T2.5 shipped the
engine and the four rules with no orchestration caller yet (only test
helpers wrote `PolicyDecisionRow` directly); this is that caller's
missing other half.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recoup.audit.events import Actor, AuditKind
from recoup.audit.repository import record_event
from recoup.domain.action import Channel
from recoup.domain.consent import ConsentEvent, ConsentSource
from recoup.domain.identifiers import CaseId, CustomerRef
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.platform.clock import Clock
from recoup.platform.logging import current_trace_id
from recoup.platform.models import ConsentEventRow, PolicyDecisionRow

__all__ = ["load_consent_events", "persist_decision", "record_consent"]


async def persist_decision(
    session: AsyncSession, clock: Clock, *, case_id: CaseId, decision: PolicyDecision
) -> None:
    """`PolicyDecision` carries no `case_id` of its own (POLICY-ENGINE's
    replay contract is scoped to one action, not a case), so the caller
    -- which already has the case in hand, to build `PolicyContext` --
    supplies it here for the audit trail alone.

    Always writes `policy_evaluated`; a `DENY`/`DEFER` verdict adds its
    own more specific kind, since POLICY-ENGINE flags a denial as the
    thing worth a compliance officer's separate attention, not merely
    one more evaluation among many.
    """
    session.add(
        PolicyDecisionRow(
            id=uuid.uuid4(),
            action_id=decision.action_id,
            attempt=decision.attempt,
            verdict=decision.verdict.value,
            rule_id=decision.rule_id,
            inputs=dict(decision.inputs),
            defer_until=decision.defer_until,
            decided_at=decision.decided_at,
        )
    )

    trace_id = current_trace_id()
    occurred_at: datetime = clock.now()
    payload = {
        "action_id": str(decision.action_id),
        "attempt": decision.attempt,
        "verdict": decision.verdict.value,
        "rule_id": decision.rule_id,
    }
    await record_event(
        session,
        case_id=case_id,
        kind=AuditKind.POLICY_EVALUATED,
        payload=payload,
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=occurred_at,
    )
    if decision.verdict is Verdict.DENY:
        await record_event(
            session,
            case_id=case_id,
            kind=AuditKind.POLICY_DENIED,
            payload=payload,
            actor=Actor.system(),
            trace_id=trace_id,
            occurred_at=occurred_at,
        )
    elif decision.verdict is Verdict.DEFER:
        await record_event(
            session,
            case_id=case_id,
            kind=AuditKind.POLICY_DEFERRED,
            payload={**payload, "defer_until": str(decision.defer_until)},
            actor=Actor.system(),
            trace_id=trace_id,
            occurred_at=occurred_at,
        )


async def record_consent(
    session: AsyncSession,
    *,
    customer: CustomerRef,
    channel: Channel,
    granted: bool,
    source: ConsentSource,
    occurred_at: datetime,
) -> None:
    """Appends one row to the consent ledger (DATA-MODEL SS3.4). No audit
    event of its own: I4/POLICY_EVALUATED already covers what a policy
    *decision* did with consent at the moment it mattered -- the grant/
    revoke event itself is `consent_events`' own record, not an action
    outcome.
    """
    session.add(
        ConsentEventRow(
            id=uuid.uuid4(),
            customer_id=uuid.UUID(customer.id),
            channel=channel.value,
            granted=granted,
            source=source.value,
            occurred_at=occurred_at,
        )
    )


async def load_consent_events(
    session: AsyncSession, customer: CustomerRef
) -> tuple[ConsentEvent, ...]:
    """The customer's whole consent ledger, unfolded -- `domain.consent.
    consent_at` does the point-in-time folding a `PolicyContext` actually
    needs; this just hands it the raw rows.
    """
    rows = (
        (
            await session.execute(
                select(ConsentEventRow).where(ConsentEventRow.customer_id == uuid.UUID(customer.id))
            )
        )
        .scalars()
        .all()
    )
    return tuple(
        ConsentEvent(
            customer=customer,
            channel=Channel(row.channel),
            granted=row.granted,
            source=ConsentSource(row.source),
            occurred_at=row.occurred_at,
        )
        for row in rows
    )
