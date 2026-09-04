"""`Detector` (TR-6) and the payload-shape helpers every L1-L3 detector
shares -- Razorpay nests the entity that actually failed one level deeper
than the envelope (`payload.payment.entity`, `payload.subscription.entity`),
and that unwrapping is identical regardless of which leak class is asking.
"""

from __future__ import annotations

from typing import Any, Protocol

from recoup.detection.events import DetectionSnapshot, InboundEvent
from recoup.domain.money import Currency, Money
from recoup.domain.signal import Signal, SignalContext
from recoup.platform.clock import Clock

__all__ = ["Detector"]


class Detector(Protocol):
    def __call__(
        self, event: InboundEvent, snapshot: DetectionSnapshot, clock: Clock
    ) -> Signal | None: ...


def payment_entity(payload: dict[str, Any]) -> dict[str, Any] | None:
    """`payload.payment.entity`, Razorpay's payment-failure envelope shape
    (RAZORPAY-INTEGRATION SS4.2's example, extended one level): present on
    `payment.failed` and, per RAZORPAY-INTEGRATION SS4.1's mapping, a
    failed `subscription.charged` too, since a charge attempt against a
    mandate is still a `Payment` underneath."""
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return None
    wrapper = inner.get("payment")
    if not isinstance(wrapper, dict):
        return None
    entity = wrapper.get("entity")
    return entity if isinstance(entity, dict) else None


def subscription_entity(payload: dict[str, Any]) -> dict[str, Any] | None:
    """`payload.subscription.entity` -- present on every subscription
    lifecycle event, `subscription.halted` included."""
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return None
    wrapper = inner.get("subscription")
    if not isinstance(wrapper, dict):
        return None
    entity = wrapper.get("entity")
    return entity if isinstance(entity, dict) else None


def amount_from(entity: dict[str, Any], key: str = "amount") -> Money | None:
    """Razorpay amounts are already integer paise on the wire -- no unit
    conversion, just the same float/bool exclusion every paise value in
    this codebase applies at its boundary."""
    value = entity.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return Money(value, Currency.INR)


def context_from(entity: dict[str, Any]) -> SignalContext:
    """Only the fields actually present make it in -- `SignalContext`'s
    fields are all optional exactly because a UPI payment has no `issuer`
    and a netbanking one has no `bin`."""

    def _str_or_none(key: str) -> str | None:
        value = entity.get(key)
        return value if isinstance(value, str) else None

    return SignalContext(
        issuer=_str_or_none("bank") or _str_or_none("issuer"),
        bin=_str_or_none("bin") or _str_or_none("card_id"),
        psp=_str_or_none("vpa"),
        instrument=_str_or_none("wallet"),
        method=_str_or_none("method"),
    )
