"""L3 -- halted subscription, from `subscription.halted`
(PHASE-02-closed-loop T2.2, RAZORPAY-INTEGRATION SS4.1).

Unlike L1/L2, `subscription.halted` carries no nested payment entity --
it is the subscription's own lifecycle state, not one specific charge
attempt. The amount at risk is therefore the subscription's recurring
charge amount, assumed here to be present on the envelope as
`payload.subscription.entity.amount` (paise, the same units as
everywhere else on the wire) -- RAZORPAY-INTEGRATION.md does not give a
worked example of this specific payload, so this is a documented
assumption for the simulator to match (ADR-0004), not a verified live
shape; correcting it is a one-line change scoped entirely to
`_amount_at_risk` below.
"""

from __future__ import annotations

from recoup.detection.detectors.base import amount_from, subscription_entity
from recoup.detection.events import DetectionSnapshot, InboundEvent
from recoup.domain.identifiers import SignalId, uuid7
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.platform.clock import Clock

__all__ = ["detect"]


def detect(event: InboundEvent, snapshot: DetectionSnapshot, clock: Clock) -> Signal | None:
    if snapshot.already_detected or event.event_type != "subscription.halted":
        return None
    entity = subscription_entity(event.payload)
    if entity is None:
        return None
    at_risk = amount_from(entity)
    if at_risk is None:
        return None
    return Signal(
        id=SignalId(uuid7()),
        leak_class=LeakClass.L3_HALTED_SUBSCRIPTION,
        customer=snapshot.customer,
        at_risk=at_risk,
        detected_at=clock.now(),
        source_event_ids=(event.provider_event_id,),
        decline=event.decline_category,
        context=SignalContext(),
    )
