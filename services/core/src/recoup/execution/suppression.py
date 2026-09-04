"""Suppresses a case (T2.10, A2.8): cancels its remaining scheduled
steps and closes it `SUPPRESSED` with a mandatory `reason_code`.

This is the mechanical operation, not the rule that decides when to
call it. `customer_opt_out -> SUPPRESSED` is a stopping rule owned by
the policy engine's R2 (POLICY-ENGINE SS3, PHASE-04 T4.2) -- reading the
consent ledger and firing this automatically is that phase's scope, the
same way `execution.suppression` is deliberately not the module that
decides *when* to fire, only what happens once something has. Until
T4.2 lands, a caller invokes `suppress_case` directly, the same way this
module's own tests and T2.10's end-to-end test do.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recoup.audit.events import Actor, AuditKind
from recoup.audit.repository import record_event
from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.identifiers import CaseId, CustomerRef, SignalId
from recoup.domain.money import Currency, Money
from recoup.execution.outbox import cancel_pending_and_claimed
from recoup.platform.clock import Clock
from recoup.platform.logging import current_trace_id
from recoup.platform.models import CaseRow, Customer, OutcomeRow

__all__ = ["suppress_case"]


async def suppress_case(
    session: AsyncSession, clock: Clock, *, case_id: CaseId, reason_code: str
) -> int:
    """Cancels every `pending`/`claimed` scheduled action for the case,
    transitions it to `SUPPRESSED`, and writes the closing `Outcome` --
    all in the caller's transaction, one commit. Returns the number of
    steps cancelled.
    """
    cancelled = await cancel_pending_and_claimed(session, case_id)

    result = await session.execute(select(CaseRow).where(CaseRow.id == case_id).with_for_update())
    row = result.scalar_one()
    customer_result = await session.execute(select(Customer).where(Customer.id == row.customer_id))
    customer_row = customer_result.scalar_one()

    case = Case(
        id=CaseId(row.id),
        signal_id=SignalId(row.signal_id),
        customer=CustomerRef(
            id=str(customer_row.id),
            razorpay_customer_id=customer_row.razorpay_customer_id,
            contact_hash=customer_row.contact_hash,
        ),
        at_risk=Money(row.at_risk_paise, Currency.INR),
        state=CaseState(row.state),
        arm=Arm(row.arm),
        opened_at=row.opened_at,
        cost_spent=Money(row.cost_spent_paise, Currency.INR),
        cost_ceiling=Money(row.cost_ceiling_paise, Currency.INR),
    )
    case.transition_to(CaseState.SUPPRESSED)

    now: datetime = clock.now()
    row.state = case.state.value
    row.resolved_at = now

    trace_id = current_trace_id()
    await record_event(
        session,
        case_id=case_id,
        kind=AuditKind.STOPPING_RULE_FIRED,
        payload={"reason_code": reason_code, "cancelled_steps": cancelled},
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=now,
    )
    await record_event(
        session,
        case_id=case_id,
        kind=AuditKind.CASE_RESOLVED,
        payload={"kind": CaseState.SUPPRESSED.value, "reason_code": reason_code},
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=now,
    )
    session.add(
        OutcomeRow(
            id=uuid.uuid4(),
            case_id=case_id,
            kind=CaseState.SUPPRESSED.value,
            recovered_paise=0,
            attributed_payment_id=None,
            attributed_step_id=None,
            reason_code=reason_code,
            resolved_at=now,
        )
    )
    return cancelled
