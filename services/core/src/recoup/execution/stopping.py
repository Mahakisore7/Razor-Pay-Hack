"""The mechanical side of POLICY-ENGINE R2 (PHASE-04 T4.2).

`policy.rules.stopping.evaluate` only ever decides -- it returns a DENY
or DEFER `PolicyDecision` with a `rule_id` naming which of R2's eight
conditions fired, the same shape as every other rule, and never touches
the database (POLICY-ENGINE SS1's third guarantee). `apply_stopping_rule`
is the caller's other half: once a gate call comes back with one of
those eight `rule_id`s, this is what actually cancels or defers the
case's remaining scheduled actions and, for the six with a terminal or
escalated outcome, closes the case.

Generalises `execution.suppression.suppress_case`, which already drew
exactly this decide/act split for `customer_opt_out` alone (T2.10) --
`suppress_case` is untouched and still the right call for a caller that
already knows the reason without going through the policy engine at
all; this module is for a caller (`bench.runner` today, a future live
worker) that needs to handle any of the eight from a `PolicyDecision`.
The two are independent, not layered -- `apply_stopping_rule` does not
call `suppress_case` even for `customer_opt_out`'s own `SUPPRESSED`
outcome, since their audit payloads key the reason differently
(`reason_code` there, matching `suppress_case`'s own pre-existing
callers; `rule_id` here, matching every other stopping-rule outcome).
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
from recoup.domain.policy_decision import PolicyDecision
from recoup.execution.outbox import cancel_pending_and_claimed, defer_pending_and_claimed
from recoup.platform.clock import Clock
from recoup.platform.logging import current_trace_id
from recoup.platform.models import CaseRow, Customer, OutcomeRow
from recoup.policy.rules.stopping import STOPPING_OUTCOMES

__all__ = ["apply_stopping_rule"]


async def apply_stopping_rule(
    session: AsyncSession, clock: Clock, *, case_id: CaseId, decision: PolicyDecision
) -> int:
    """Dispatches on `decision.rule_id` (must be one of `STOPPING_OUTCOMES`'
    keys) and returns however many scheduled actions were cancelled (0
    for `promise_to_pay_active`, which defers rather than cancels).
    Commits on every path -- there is no further work for the caller to
    fold into the same transaction, unlike `suppress_case`, whose caller
    is itself mid-transaction closing the case for an unrelated reason.
    """
    rule_id = decision.rule_id
    assert rule_id in STOPPING_OUTCOMES, f"not a stopping rule: {rule_id!r}"

    if rule_id == "promise_to_pay_active":
        assert decision.defer_until is not None
        deferred = await defer_pending_and_claimed(session, case_id, until=decision.defer_until)
        await record_event(
            session,
            case_id=case_id,
            kind=AuditKind.STOPPING_RULE_FIRED,
            payload={
                "rule_id": rule_id,
                "deferred_steps": deferred,
                "until": decision.defer_until.isoformat(),
            },
            actor=Actor.system(),
            trace_id=current_trace_id(),
            occurred_at=clock.now(),
        )
        await session.commit()
        return deferred

    cancelled = await cancel_pending_and_claimed(session, case_id)
    target_state = STOPPING_OUTCOMES[rule_id]
    if target_state is None:
        # already_paid: attribution has already resolved the case (it is
        # the one that wrote `case_resolved`) -- this call's only job is
        # sweeping up scheduled actions attribution itself does not
        # cancel, and giving the audit trail the precise reason rather
        # than letting a later claim fall through to `domain_guard`.
        await record_event(
            session,
            case_id=case_id,
            kind=AuditKind.STOPPING_RULE_FIRED,
            payload={"rule_id": rule_id, "cancelled_steps": cancelled},
            actor=Actor.system(),
            trace_id=current_trace_id(),
            occurred_at=clock.now(),
        )
        await session.commit()
        return cancelled

    await _close_case(
        session,
        clock,
        case_id=case_id,
        target_state=target_state,
        rule_id=rule_id,
        cancelled=cancelled,
    )
    return cancelled


async def _close_case(
    session: AsyncSession,
    clock: Clock,
    *,
    case_id: CaseId,
    target_state: CaseState,
    rule_id: str,
    cancelled: int,
) -> None:
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
    case.transition_to(target_state)

    now: datetime = clock.now()
    row.state = case.state.value
    # Set together, before `record_event`'s own query below can trigger
    # an autoflush: the `resolved_iff_terminal` check constraint rejects
    # `state`/`resolved_at` landing in the database one at a time, and a
    # flush does not know or care that a second assignment is coming.
    if case.is_terminal:
        row.resolved_at = now
    trace_id = current_trace_id()
    # `max_case_age` gets the specific `case_expired` kind rather than
    # the generic `stopping_rule_fired` every other condition writes --
    # DOMAIN-MODEL SS10.1 lists it as its own audit kind, the same way
    # attribution has its own `payment_attributed` ahead of the shared
    # `case_resolved` every closing path ends with.
    fired_kind = (
        AuditKind.CASE_EXPIRED
        if target_state is CaseState.EXPIRED
        else AuditKind.STOPPING_RULE_FIRED
    )
    await record_event(
        session,
        case_id=case_id,
        kind=fired_kind,
        payload={"rule_id": rule_id, "cancelled_steps": cancelled},
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=now,
    )

    if case.is_terminal:
        await record_event(
            session,
            case_id=case_id,
            kind=AuditKind.CASE_RESOLVED,
            payload={"kind": target_state.value, "reason_code": rule_id},
            actor=Actor.system(),
            trace_id=trace_id,
            occurred_at=now,
        )
        session.add(
            OutcomeRow(
                id=uuid.uuid4(),
                case_id=case_id,
                kind=target_state.value,
                recovered_paise=0,
                attributed_payment_id=None,
                attributed_step_id=None,
                reason_code=rule_id,
                resolved_at=now,
            )
        )
    # ESCALATED is not terminal (domain.case.TERMINAL_STATES) -- no
    # `resolved_at`, no `Outcome`, no `case_resolved`. A human (or a
    # future re-mandate flow) moves it on from here; T4.2's own scope
    # stops at "cancel the stale actions and flag it."

    await session.commit()
