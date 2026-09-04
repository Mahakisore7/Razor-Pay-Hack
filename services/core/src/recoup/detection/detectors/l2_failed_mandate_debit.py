"""L2 -- failed mandate debit, from a failed `subscription.charged`
(PHASE-02-closed-loop T2.2, RAZORPAY-INTEGRATION SS4.1).

A charge attempt against a mandate is still a `Payment` underneath, so a
failed `subscription.charged` carries the same nested
`payload.payment.entity` shape a one-time `payment.failed` does -- only
the top-level `event` name and the fact that it is scoped to a mandate
differ. A *successful* `subscription.charged` has the same shape with
`entity.status != "failed"`, which is exactly what distinguishes L2 from
the far more common non-event of a mandate debit that just worked.
"""

from __future__ import annotations

from recoup.detection.detectors.base import amount_from, context_from, payment_entity
from recoup.detection.events import DetectionSnapshot, InboundEvent
from recoup.domain.identifiers import SignalId, uuid7
from recoup.domain.signal import LeakClass, Signal
from recoup.platform.clock import Clock

__all__ = ["detect"]


def detect(event: InboundEvent, snapshot: DetectionSnapshot, clock: Clock) -> Signal | None:
    if snapshot.already_detected or event.event_type != "subscription.charged":
        return None
    entity = payment_entity(event.payload)
    if entity is None or entity.get("status") != "failed":
        return None
    at_risk = amount_from(entity)
    if at_risk is None:
        return None
    payment_id = entity.get("id")
    return Signal(
        id=SignalId(uuid7()),
        leak_class=LeakClass.L2_FAILED_MANDATE_DEBIT,
        customer=snapshot.customer,
        at_risk=at_risk,
        detected_at=clock.now(),
        source_event_ids=(event.provider_event_id,),
        decline=event.decline_category,
        context=context_from(entity),
        source_payment_id=payment_id if isinstance(payment_id, str) else None,
    )
