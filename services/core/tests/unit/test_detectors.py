"""Golden fixture tests for the L1-L3 detectors (T2.2, TR-6) -- each is a
pure function of `(event, snapshot, clock)`, so every case here is
in-memory: no database, no mocking, just literal `InboundEvent` and
`DetectionSnapshot` values in and a `Signal | None` out.
"""

from datetime import UTC, datetime

import pytest

from recoup.detection.detectors import l1_failed_payment, l2_failed_mandate_debit
from recoup.detection.detectors import l3_halted_subscription as l3
from recoup.detection.detectors.base import Detector
from recoup.detection.events import DetectionSnapshot, InboundEvent
from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import CustomerRef
from recoup.domain.signal import LeakClass
from recoup.platform.clock import FrozenClock

_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
_CUSTOMER = CustomerRef(id="cust-internal-1", razorpay_customer_id="cust_1", contact_hash="h1")
_NOT_YET_DETECTED = DetectionSnapshot(customer=_CUSTOMER, already_detected=False)
_ALREADY_DETECTED = DetectionSnapshot(customer=_CUSTOMER, already_detected=True)


def _event(
    event_type: str, payload: dict[str, object], decline: DeclineCategory | None = None
) -> InboundEvent:
    return InboundEvent(
        provider_event_id="evt-1",
        event_type=event_type,
        payload=payload,
        decline_category=decline,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# --- L1: failed one-time payment -------------------------------------------------


_L1_PAYLOAD: dict[str, object] = {
    "payload": {
        "payment": {
            "entity": {
                "id": "pay_1",
                "amount": 250_000,
                "method": "upi",
                "vpa": "test@okhdfc",
            }
        }
    }
}


def test_l1_detects_a_failed_payment() -> None:
    event = _event("payment.failed", _L1_PAYLOAD, decline=DeclineCategory.INSUFFICIENT_FUNDS)
    signal = l1_failed_payment.detect(event, _NOT_YET_DETECTED, _CLOCK)

    assert signal is not None
    assert signal.leak_class == LeakClass.L1_FAILED_ONE_TIME_PAYMENT
    assert signal.customer == _CUSTOMER
    assert signal.at_risk.paise == 250_000
    assert signal.detected_at == _CLOCK.now()
    assert signal.source_event_ids == ("evt-1",)
    assert signal.decline == DeclineCategory.INSUFFICIENT_FUNDS
    assert signal.context.psp == "test@okhdfc"
    assert signal.context.method == "upi"


def test_l1_ignores_a_different_event_type() -> None:
    event = _event("payment.captured", _L1_PAYLOAD)
    assert l1_failed_payment.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


def test_l1_is_idempotent_over_an_already_detected_event() -> None:
    event = _event("payment.failed", _L1_PAYLOAD)
    assert l1_failed_payment.detect(event, _ALREADY_DETECTED, _CLOCK) is None


def test_l1_ignores_a_payload_with_no_payment_entity() -> None:
    event = _event("payment.failed", {"payload": {}})
    assert l1_failed_payment.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


def test_l1_ignores_a_payload_with_no_top_level_payload_key() -> None:
    event = _event("payment.failed", {})
    assert l1_failed_payment.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


def test_l1_ignores_a_missing_or_non_positive_amount() -> None:
    event = _event("payment.failed", {"payload": {"payment": {"entity": {"amount": 0}}}})
    assert l1_failed_payment.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


# --- L2: failed mandate debit ------------------------------------------------------


_L2_FAILED_PAYLOAD: dict[str, object] = {
    "payload": {
        "payment": {
            "entity": {
                "id": "pay_2",
                "amount": 99_900,
                "status": "failed",
                "method": "card",
                "bank": "HDFC",
            }
        },
        "subscription": {"entity": {"id": "sub_1"}},
    }
}


def test_l2_detects_a_failed_subscription_charge() -> None:
    event = _event(
        "subscription.charged", _L2_FAILED_PAYLOAD, decline=DeclineCategory.MANDATE_REVOKED
    )
    signal = l2_failed_mandate_debit.detect(event, _NOT_YET_DETECTED, _CLOCK)

    assert signal is not None
    assert signal.leak_class == LeakClass.L2_FAILED_MANDATE_DEBIT
    assert signal.at_risk.paise == 99_900
    assert signal.decline == DeclineCategory.MANDATE_REVOKED
    assert signal.context.issuer == "HDFC"


def test_l2_ignores_a_successful_subscription_charge() -> None:
    """The far more common case: a charge that just worked must never
    produce a signal, or every healthy mandate debit would look like a
    leak."""
    payload: dict[str, object] = {
        "payload": {
            "payment": {"entity": {"id": "pay_3", "amount": 99_900, "status": "captured"}},
            "subscription": {"entity": {"id": "sub_1"}},
        }
    }
    event = _event("subscription.charged", payload)
    assert l2_failed_mandate_debit.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


def test_l2_ignores_a_different_event_type() -> None:
    event = _event("payment.failed", _L2_FAILED_PAYLOAD)
    assert l2_failed_mandate_debit.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


def test_l2_is_idempotent_over_an_already_detected_event() -> None:
    event = _event("subscription.charged", _L2_FAILED_PAYLOAD)
    assert l2_failed_mandate_debit.detect(event, _ALREADY_DETECTED, _CLOCK) is None


def test_l2_ignores_a_failed_charge_with_no_positive_amount() -> None:
    payload: dict[str, object] = {
        "payload": {
            "payment": {"entity": {"amount": 0, "status": "failed"}},
            "subscription": {"entity": {"id": "sub_1"}},
        }
    }
    event = _event("subscription.charged", payload)
    assert l2_failed_mandate_debit.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


# --- L3: halted subscription --------------------------------------------------------


_L3_PAYLOAD: dict[str, object] = {
    "payload": {"subscription": {"entity": {"id": "sub_2", "amount": 149_900}}}
}


def test_l3_detects_a_halted_subscription() -> None:
    event = _event("subscription.halted", _L3_PAYLOAD)
    signal = l3.detect(event, _NOT_YET_DETECTED, _CLOCK)

    assert signal is not None
    assert signal.leak_class == LeakClass.L3_HALTED_SUBSCRIPTION
    assert signal.at_risk.paise == 149_900
    assert signal.source_event_ids == ("evt-1",)


def test_l3_ignores_a_different_event_type() -> None:
    event = _event("subscription.pending", _L3_PAYLOAD)
    assert l3.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


def test_l3_is_idempotent_over_an_already_detected_event() -> None:
    event = _event("subscription.halted", _L3_PAYLOAD)
    assert l3.detect(event, _ALREADY_DETECTED, _CLOCK) is None


def test_l3_ignores_a_payload_with_no_subscription_entity() -> None:
    event = _event("subscription.halted", {"payload": {}})
    assert l3.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


def test_l3_ignores_a_missing_or_non_positive_amount() -> None:
    payload: dict[str, object] = {"payload": {"subscription": {"entity": {"id": "sub_3"}}}}
    event = _event("subscription.halted", payload)
    assert l3.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


def test_l3_ignores_a_payload_with_no_top_level_payload_key() -> None:
    event = _event("subscription.halted", {})
    assert l3.detect(event, _NOT_YET_DETECTED, _CLOCK) is None


@pytest.mark.parametrize(
    ("detector", "event_type", "payload"),
    [
        (l1_failed_payment.detect, "payment.failed", _L1_PAYLOAD),
        (l2_failed_mandate_debit.detect, "subscription.charged", _L2_FAILED_PAYLOAD),
        (l3.detect, "subscription.halted", _L3_PAYLOAD),
    ],
)
def test_every_detector_uses_the_injected_clock_not_wall_time(
    detector: Detector, event_type: str, payload: dict[str, object]
) -> None:
    at = datetime(2030, 5, 17, 3, 30, tzinfo=UTC)
    clock = FrozenClock(at)
    event = _event(event_type, payload)
    signal = detector(event, _NOT_YET_DETECTED, clock)
    assert signal is not None
    assert signal.detected_at == at
