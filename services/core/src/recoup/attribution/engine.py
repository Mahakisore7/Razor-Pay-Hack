"""Orchestrates attribution (T2.8): loads the open cases a captured
payment could belong to, applies the pure matcher, and persists the
result -- the `payments` row (`case_id` set on a win), the `outcomes`
row, and the winning case's own transition to a terminal state.

Idempotent by construction: a payment whose `payments` row already
carries a `case_id` is treated as already attributed and short-circuits
before the matcher ever runs again, so replaying the same payment (a
retried caller, a re-delivered event once T2.10 wires a trigger to this
function) can never move it to a second case.

This module does not assign `LOST` or `EXPIRED`. Both are stopping-rule
outcomes (`max_attempts_reached`, `max_case_age`) owned by the policy
engine's R2 (POLICY-ENGINE SS3, PHASE-04 T4.2), not attribution -- this
module only ever writes `RECOVERED` or `PARTIALLY_RECOVERED`, the two
kinds `Outcome` itself allows without a `reason_code`.

I4 (T2.9): the winning case gets `payment_attributed` (why) then
`case_resolved` (the state transition itself), and each losing case in a
contended match gets its own `attribution_ambiguous` -- on its own
chain, since that case's state does not change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from recoup.attribution.matcher import CaseCandidate, PaymentInfo, match_payment
from recoup.audit.events import Actor, AuditKind
from recoup.audit.repository import record_event
from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.identifiers import CaseId, CustomerRef, SignalId
from recoup.domain.money import Currency, Money
from recoup.domain.outcome import OutcomeKind
from recoup.gateway.interface import Payment as GatewayPayment
from recoup.gateway.interface import PaymentStatus
from recoup.platform.clock import Clock
from recoup.platform.logging import current_trace_id
from recoup.platform.models import ActionRow, CaseRow, Customer, OutcomeRow, ScheduledActionRow
from recoup.platform.models import Payment as PaymentRow

__all__ = ["AttributionResult", "attribute_payment"]

# The states the ARCHITECTURE state diagram draws an edge into
# AWAITING_OUTCOME from (HOLDOUT, EXECUTING), plus AWAITING_OUTCOME
# itself -- exactly the cases that have had a chance to be paid for in
# this pipeline's own terms. A case still DETECTED/PLANNED/etc. has no
# window to anchor to yet.
_ELIGIBLE_STATES = frozenset(
    {CaseState.HOLDOUT.value, CaseState.EXECUTING.value, CaseState.AWAITING_OUTCOME.value}
)


@dataclass(frozen=True, slots=True)
class AttributionResult:
    matched_case_id: CaseId | None
    ambiguous_case_ids: tuple[CaseId, ...]


async def attribute_payment(
    session: AsyncSession, clock: Clock, *, payment: GatewayPayment
) -> AttributionResult:
    if payment.status is not PaymentStatus.CAPTURED:
        return AttributionResult(matched_case_id=None, ambiguous_case_ids=())

    already = await session.get(PaymentRow, payment.id)
    if already is not None and already.case_id is not None:
        return AttributionResult(matched_case_id=CaseId(already.case_id), ambiguous_case_ids=())

    candidates = await _load_candidates(session, payment.customer_id)
    info = PaymentInfo(
        id=payment.id,
        razorpay_customer_id=payment.customer_id,
        amount=payment.amount,
        captured_at=payment.created_at,
    )
    result = match_payment(info, candidates)

    await _upsert_payment_row(session, payment, case_id=result.winner)

    if result.winner is None:
        await session.commit()
        return AttributionResult(matched_case_id=None, ambiguous_case_ids=())

    assert result.kind is not None
    assert result.recovered is not None
    await _resolve_case(
        session,
        clock,
        case_id=result.winner,
        kind=result.kind,
        recovered=result.recovered,
        payment_id=payment.id,
        step_id=result.step_id,
    )
    if result.ambiguous_with:
        await _record_ambiguity(
            session,
            clock,
            payment_id=payment.id,
            winner=result.winner,
            losers=result.ambiguous_with,
        )
    await session.commit()
    return AttributionResult(
        matched_case_id=result.winner, ambiguous_case_ids=result.ambiguous_with
    )


async def _record_ambiguity(
    session: AsyncSession,
    clock: Clock,
    *,
    payment_id: str,
    winner: CaseId,
    losers: tuple[CaseId, ...],
) -> None:
    """`_resolve_case` (the winner) always runs before this (the
    losers), and `match_payment`'s contention rule (TR-29) always picks
    the *same* older case as winner for a given set of candidate cases,
    regardless of which payment or which caller triggers the match -- so
    two concurrent calls that touch an overlapping set of cases always
    lock winner-then-loser in the same order too, `recoup.audit.
    repository`'s per-case chain-tail lock included. That is what rules
    out a deadlock here without needing an explicit sort, unlike the
    outbox's multi-case batch claim.
    """
    trace_id = current_trace_id()
    now = clock.now()
    for loser_id in losers:
        await record_event(
            session,
            case_id=loser_id,
            kind=AuditKind.ATTRIBUTION_AMBIGUOUS,
            payload={"payment_id": payment_id, "winning_case_id": str(winner)},
            actor=Actor.system(),
            trace_id=trace_id,
            occurred_at=now,
        )


async def _load_candidates(session: AsyncSession, razorpay_customer_id: str) -> list[CaseCandidate]:
    result = await session.execute(
        select(CaseRow)
        .join(Customer, Customer.id == CaseRow.customer_id)
        .where(
            Customer.razorpay_customer_id == razorpay_customer_id,
            CaseRow.state.in_(_ELIGIBLE_STATES),
        )
    )
    rows = result.scalars().all()
    if not rows:
        return []

    non_holdout_ids = [row.id for row in rows if row.state != CaseState.HOLDOUT.value]
    anchors = await _most_recent_executions(session, non_holdout_ids)

    candidates: list[CaseCandidate] = []
    for row in rows:
        at_risk = Money(row.at_risk_paise, Currency.INR)
        if row.state == CaseState.HOLDOUT.value:
            # TR-30: a holdout case has no action to anchor to -- the
            # window starts at case creation, so the two arms are
            # measured on the same clock.
            candidates.append(
                CaseCandidate(
                    case_id=CaseId(row.id),
                    razorpay_customer_id=razorpay_customer_id,
                    at_risk=at_risk,
                    opened_at=row.opened_at,
                    window_anchor=row.opened_at,
                    window_step_id=None,
                )
            )
            continue

        anchor = anchors.get(row.id)
        if anchor is None:
            continue  # no action has completed yet -- no window has started
        executed_at, step_id = anchor
        candidates.append(
            CaseCandidate(
                case_id=CaseId(row.id),
                razorpay_customer_id=razorpay_customer_id,
                at_risk=at_risk,
                opened_at=row.opened_at,
                window_anchor=executed_at,
                window_step_id=step_id,
            )
        )
    return candidates


async def _most_recent_executions(
    session: AsyncSession, case_ids: list[UUID]
) -> dict[UUID, tuple[datetime, str]]:
    """One most-recent `done` action per case, in a single query -- a
    Postgres `DISTINCT ON` ordered by `executed_at DESC`, rather than one
    round trip per candidate case.
    """
    if not case_ids:
        return {}
    stmt = (
        select(ScheduledActionRow.case_id, ScheduledActionRow.executed_at, ActionRow.step_id)
        .distinct(ScheduledActionRow.case_id)
        .join(ActionRow, ActionRow.id == ScheduledActionRow.action_id)
        .where(
            ScheduledActionRow.case_id.in_(case_ids),
            ScheduledActionRow.status == "done",
            ScheduledActionRow.executed_at.is_not(None),
        )
        .order_by(ScheduledActionRow.case_id, ScheduledActionRow.executed_at.desc())
    )
    result = await session.execute(stmt)
    return {row.case_id: (row.executed_at, row.step_id) for row in result}


async def _upsert_payment_row(
    session: AsyncSession, payment: GatewayPayment, *, case_id: CaseId | None
) -> None:
    stmt = pg_insert(PaymentRow).values(
        id=payment.id,
        case_id=case_id,
        amount_paise=payment.amount.paise,
        method=payment.method,
        captured_at=payment.created_at,
    )
    if case_id is None:
        # Record the sighting without clobbering a case_id a concurrent
        # attribution run may have just set.
        stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
    else:
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={"case_id": case_id})
    await session.execute(stmt)


async def _resolve_case(
    session: AsyncSession,
    clock: Clock,
    *,
    case_id: CaseId,
    kind: OutcomeKind,
    recovered: Money,
    payment_id: str,
    step_id: str | None,
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
    if case.state is not CaseState.AWAITING_OUTCOME:
        case.transition_to(CaseState.AWAITING_OUTCOME)
    case.transition_to(CaseState(kind.value))

    now = clock.now()
    row.state = case.state.value
    row.resolved_at = now
    trace_id = current_trace_id()
    await record_event(
        session,
        case_id=case_id,
        kind=AuditKind.PAYMENT_ATTRIBUTED,
        payload={
            "payment_id": payment_id,
            "kind": kind.value,
            "recovered_paise": recovered.paise,
            "step_id": step_id,
        },
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=now,
    )
    await record_event(
        session,
        case_id=case_id,
        kind=AuditKind.CASE_RESOLVED,
        payload={"kind": kind.value, "recovered_paise": recovered.paise},
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=now,
    )
    session.add(
        OutcomeRow(
            id=uuid.uuid4(),
            case_id=case_id,
            kind=kind.value,
            recovered_paise=recovered.paise,
            attributed_payment_id=payment_id,
            attributed_step_id=step_id,
            reason_code=None,
            resolved_at=now,
        )
    )
