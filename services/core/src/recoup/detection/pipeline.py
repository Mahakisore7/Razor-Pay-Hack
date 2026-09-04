"""Wires the pure L1-L3 detectors (TR-6) to the database: customer
resolution, the already-detected idempotency check, and case-opening with
arm assignment (PHASE-02-closed-loop T2.2).

Deliberately not called from anywhere yet -- T2.1 (ingestion) and T2.2
(detection) are separate PRs precisely because the two are not wired
together end to end until T2.10's full-pipeline test. `run_detection` is
the seam a future caller (a worker, or the webhook route itself) hooks
into.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from recoup.audit.events import Actor, AuditKind
from recoup.audit.repository import record_event
from recoup.detection.detectors import l1_failed_payment, l2_failed_mandate_debit
from recoup.detection.detectors import l3_halted_subscription as l3_detector
from recoup.detection.detectors.base import Detector
from recoup.detection.events import DetectionSnapshot, InboundEvent
from recoup.domain.case import Case, CaseState, assign_arm
from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import CaseId, CustomerRef, SignalId, uuid7
from recoup.domain.money import Money
from recoup.domain.signal import Signal
from recoup.platform.clock import Clock
from recoup.platform.logging import current_trace_id
from recoup.platform.models import CaseRow, Customer, RawEvent, SignalRow

__all__ = [
    "already_detected",
    "open_case_for_signal",
    "resolve_customer",
    "run_detection",
]

_DETECTORS: tuple[Detector, ...] = (
    l1_failed_payment.detect,
    l2_failed_mandate_debit.detect,
    l3_detector.detect,
)


def _raw_customer_id(payload: dict[str, Any]) -> str | None:
    """A payment- or subscription-shaped envelope both carry `customer_id`
    on their nested entity -- whichever is present is the one this event
    is about."""
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return None
    for wrapper_key in ("payment", "subscription"):
        wrapper = inner.get(wrapper_key)
        if not isinstance(wrapper, dict):
            continue
        entity = wrapper.get("entity")
        if not isinstance(entity, dict):
            continue
        customer_id = entity.get("customer_id")
        if isinstance(customer_id, str) and customer_id:
            return customer_id
    return None


async def resolve_customer(session: AsyncSession, razorpay_customer_id: str) -> CustomerRef:
    """Find-or-create by Razorpay's customer id.

    `contact_hash` is a placeholder derived from `razorpay_customer_id`
    itself: a webhook's payment/subscription entity carries no phone or
    email, only the id, so there is nothing real to hash yet for a
    newly-seen customer. A later customer/PII sync (out of this phase's
    scope) is expected to backfill the real value; nothing downstream
    treats this placeholder as if it were one -- DATA-MODEL's PII
    isolation already keeps `contact_hash` and actual PII in separate
    tables, so this is a gap in accuracy, not a leak.
    """
    existing = await session.execute(
        select(Customer).where(Customer.razorpay_customer_id == razorpay_customer_id)
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = Customer(
            id=uuid.uuid4(),
            razorpay_customer_id=razorpay_customer_id,
            contact_hash=hashlib.sha256(razorpay_customer_id.encode()).hexdigest(),
        )
        session.add(row)
        await session.flush()
    return CustomerRef(
        id=str(row.id),
        razorpay_customer_id=row.razorpay_customer_id,
        contact_hash=row.contact_hash,
    )


async def already_detected(session: AsyncSession, provider_event_id: str) -> bool:
    """TR-4: re-running detection over an already-processed raw event must
    not produce a second signal. `source_event_ids` is a JSONB array;
    `?` is Postgres's "does this array contain this string" operator."""
    result = await session.execute(
        select(SignalRow.id).where(SignalRow.source_event_ids.op("?")(provider_event_id)).limit(1)
    )
    return result.first() is not None


def _to_inbound_event(raw_event: RawEvent) -> InboundEvent:
    decline_category = (
        DeclineCategory[raw_event.decline_category] if raw_event.decline_category else None
    )
    return InboundEvent(
        provider_event_id=raw_event.provider_event_id,
        event_type=raw_event.event_type,
        payload=raw_event.payload,
        decline_category=decline_category,
        received_at=raw_event.received_at,
    )


def _context_json(signal: Signal) -> dict[str, str | None]:
    return {
        "issuer": signal.context.issuer,
        "bin": signal.context.bin,
        "psp": signal.context.psp,
        "instrument": signal.context.instrument,
        "method": signal.context.method,
    }


async def open_case_for_signal(
    session: AsyncSession, clock: Clock, seed: int, signal: Signal
) -> Case | None:
    """Persists the `Signal`, then opens a `Case` from it with an arm
    assigned before diagnosis ever runs (T2.2's ordering requirement).

    Returns `None` -- instead of raising -- when `cases_open_dedup`
    (TR-8: one open case per `(customer, at_risk_paise)`) rejects the
    insert: a customer already has an open case at this exact amount, so
    this detection is a no-op, the same shape as a webhook replay's
    no-op in T2.1. The just-flushed `Signal` row is rolled back with it
    rather than kept orphaned with no case -- re-running detection over
    the same raw event lands on the identical, harmless no-op again
    (TR-4), so nothing is lost by not keeping it.

    `cost_ceiling` starts at zero: no playbook has been chosen yet (that
    is planning's job, T2.4), and `playbook.cost_ceiling_pct * at_risk`
    is what actually funds it. `cost_spent <= cost_ceiling` (I2) still
    holds trivially at 0 <= 0.

    I4 (T2.9): the dedup no-op path writes no audit event -- nothing was
    created, so there is nothing to have a chain. A real case gets three,
    `signal_detected` / `case_opened` / `arm_assigned`, sharing one
    `trace_id` and committed in the same transaction as the rows above.
    """
    customer_id = uuid.UUID(signal.customer.id)
    signal_row = SignalRow(
        id=signal.id,
        leak_class=signal.leak_class.value,
        customer_id=customer_id,
        at_risk_paise=signal.at_risk.paise,
        detected_at=signal.detected_at,
        source_event_ids=list(signal.source_event_ids),
        decline_category=signal.decline.name if signal.decline is not None else None,
        context=_context_json(signal),
    )
    session.add(signal_row)
    await session.flush()

    case_id = CaseId(uuid7())
    arm = assign_arm(seed, case_id)
    case_row = CaseRow(
        id=case_id,
        signal_id=signal_row.id,
        customer_id=customer_id,
        state=CaseState.DETECTED.value,
        arm=arm.value,
        at_risk_paise=signal.at_risk.paise,
        cost_ceiling_paise=0,
        opened_at=clock.now(),
    )
    session.add(case_row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return None

    trace_id = current_trace_id()
    await record_event(
        session,
        case_id=case_id,
        kind=AuditKind.SIGNAL_DETECTED,
        payload={
            "leak_class": signal.leak_class.value,
            "at_risk_paise": signal.at_risk.paise,
            "decline_category": signal.decline.name if signal.decline is not None else None,
        },
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=clock.now(),
    )
    await record_event(
        session,
        case_id=case_id,
        kind=AuditKind.CASE_OPENED,
        payload={"signal_id": str(signal_row.id), "at_risk_paise": signal.at_risk.paise},
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=clock.now(),
    )
    await record_event(
        session,
        case_id=case_id,
        kind=AuditKind.ARM_ASSIGNED,
        payload={"arm": arm.value, "seed": seed},
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=clock.now(),
    )

    await session.commit()
    return Case(
        id=case_id,
        signal_id=SignalId(signal_row.id),
        customer=signal.customer,
        at_risk=signal.at_risk,
        state=CaseState.DETECTED,
        arm=arm,
        opened_at=case_row.opened_at,
        cost_spent=Money(0, signal.at_risk.currency),
        cost_ceiling=Money(0, signal.at_risk.currency),
    )


async def run_detection(
    session: AsyncSession, clock: Clock, seed: int, raw_event: RawEvent
) -> Case | None:
    """The end-to-end T2.2 entry point: resolve the customer, run each
    detector until one fires, and open a case from whatever it produces.
    `None` covers every "nothing to do" outcome uniformly -- no customer
    id on the payload, no detector matched, or a dedup no-op -- since
    none of them is an error."""
    event = _to_inbound_event(raw_event)
    raw_customer_id = _raw_customer_id(event.payload)
    if raw_customer_id is None:
        return None

    customer = await resolve_customer(session, raw_customer_id)
    detected = await already_detected(session, event.provider_event_id)
    snapshot = DetectionSnapshot(customer=customer, already_detected=detected)

    signal: Signal | None = None
    for detector in _DETECTORS:
        signal = detector(event, snapshot, clock)
        if signal is not None:
            break
    if signal is None:
        return None

    return await open_case_for_signal(session, clock, seed, signal)
