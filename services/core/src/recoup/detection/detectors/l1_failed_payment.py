"""L1 -- failed one-time payment, from `payment.failed`
(PHASE-02-closed-loop T2.2, RAZORPAY-INTEGRATION SS4.1).
"""

from __future__ import annotations

from recoup.detection.detectors.base import amount_from, context_from, payment_entity
from recoup.detection.events import DetectionSnapshot, InboundEvent
from recoup.domain.identifiers import SignalId, uuid7
from recoup.domain.signal import LeakClass, Signal
from recoup.platform.clock import Clock

__all__ = ["detect"]


def detect(event: InboundEvent, snapshot: DetectionSnapshot, clock: Clock) -> Signal | None:
    if snapshot.already_detected or event.event_type != "payment.failed":
        return None
    entity = payment_entity(event.payload)
    if entity is None:
        return None
    at_risk = amount_from(entity)
    if at_risk is None:
        return None
    return Signal(
        id=SignalId(uuid7()),
        leak_class=LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
        customer=snapshot.customer,
        at_risk=at_risk,
        detected_at=clock.now(),
        source_event_ids=(event.provider_event_id,),
        decline=event.decline_category,
        context=context_from(entity),
    )
