"""Pure-function tests for `recoup.gateway.ingestion` -- everything here
runs with no database (T2.1): parsing, decline-reason extraction, and
taxonomy normalization are all deterministic functions of their input.
`store_raw_event` itself needs a real Postgres for its `ON CONFLICT DO
NOTHING` dedup to mean anything, so it is covered separately in
`tests/integration/test_webhook_ingestion.py`.
"""

import json

import pytest

from recoup.gateway.ingestion import (
    UnparseableEventError,
    extract_decline_reason,
    normalize_decline_category,
    parse_razorpay_event,
)

_PAYMENT_FAILED = {
    "entity": "event",
    "event": "payment.failed",
    "contains": ["payment"],
    "payload": {
        "payment": {
            "entity": {
                "id": "pay_123",
                "error_reason": "payment_failed_due_to_insufficient_funds",
            }
        }
    },
    "created_at": 1_700_000_000,
}

_PAYMENT_CAPTURED = {
    "entity": "event",
    "event": "payment.captured",
    "contains": ["payment"],
    "payload": {"payment": {"entity": {"id": "pay_456"}}},
    "created_at": 1_700_000_100,
}

_SUBSCRIPTION_CHARGE_FAILED = {
    "entity": "event",
    "event": "subscription.charged",
    "contains": ["payment", "subscription"],
    "payload": {
        "payment": {
            "entity": {"id": "pay_789", "error_reason": "mandate_revoked"},
        },
        "subscription": {"entity": {"id": "sub_1"}},
    },
    "created_at": 1_700_000_200,
}

_SUBSCRIPTION_HALTED = {
    "entity": "event",
    "event": "subscription.halted",
    "contains": ["subscription"],
    "payload": {"subscription": {"entity": {"id": "sub_1", "customer_id": "cust_1"}}},
    "created_at": 1_700_000_300,
}


# --- parse_razorpay_event -----------------------------------------------------


def test_parse_razorpay_event_returns_type_and_full_payload() -> None:
    event_type, payload = parse_razorpay_event(json.dumps(_PAYMENT_FAILED).encode())
    assert event_type == "payment.failed"
    assert payload == _PAYMENT_FAILED


def test_parse_razorpay_event_raises_on_invalid_json() -> None:
    with pytest.raises(UnparseableEventError):
        parse_razorpay_event(b"{not json")


def test_parse_razorpay_event_raises_on_missing_event_field() -> None:
    with pytest.raises(UnparseableEventError):
        parse_razorpay_event(json.dumps({"payload": {}}).encode())


def test_parse_razorpay_event_raises_on_non_string_event_field() -> None:
    with pytest.raises(UnparseableEventError):
        parse_razorpay_event(json.dumps({"event": 123}).encode())


def test_parse_razorpay_event_raises_when_body_is_a_json_list() -> None:
    with pytest.raises(UnparseableEventError):
        parse_razorpay_event(b"[1, 2, 3]")


# --- extract_decline_reason ----------------------------------------------------


def test_extract_decline_reason_reads_the_nested_error_reason() -> None:
    reason = extract_decline_reason("payment.failed", _PAYMENT_FAILED)
    assert reason == "payment_failed_due_to_insufficient_funds"


def test_extract_decline_reason_is_none_when_no_payment_entity_is_present() -> None:
    """`subscription.halted` carries no nested payment entity at all --
    the structural `.get`/`isinstance` chain returns None rather than
    raising on a payload shaped nothing like `payload.payment.entity`."""
    assert extract_decline_reason("subscription.halted", _SUBSCRIPTION_HALTED) is None


def test_extract_decline_reason_reads_it_from_a_failed_subscription_charge() -> None:
    """A failed `subscription.charged` carries the same nested payment
    shape a one-time `payment.failed` does (RAZORPAY-INTEGRATION SS4.1's
    L2 mapping) -- this is not gated on `event_type` any more than the
    payment-entity extraction itself is."""
    reason = extract_decline_reason("subscription.charged", _SUBSCRIPTION_CHARGE_FAILED)
    assert reason == "mandate_revoked"


def test_extract_decline_reason_is_none_when_absent_from_a_payment_event() -> None:
    assert extract_decline_reason("payment.captured", _PAYMENT_CAPTURED) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"event": "payment.failed"},
        {"event": "payment.failed", "payload": "not a dict"},
        {"event": "payment.failed", "payload": {"payment": "not a dict"}},
        {"event": "payment.failed", "payload": {"payment": {"entity": "not a dict"}}},
        {"event": "payment.failed", "payload": {"payment": {"entity": {"error_reason": 123}}}},
    ],
)
def test_extract_decline_reason_tolerates_malformed_shapes(payload: dict[str, object]) -> None:
    assert extract_decline_reason("payment.failed", payload) is None


# --- normalize_decline_category -------------------------------------------------


def test_normalize_decline_category_maps_a_known_reason() -> None:
    assert normalize_decline_category("payment.failed", _PAYMENT_FAILED) == "INSUFFICIENT_FUNDS"


def test_normalize_decline_category_is_none_without_a_reason_to_categorize() -> None:
    assert normalize_decline_category("payment.captured", _PAYMENT_CAPTURED) is None


def test_normalize_decline_category_is_unknown_for_an_unmapped_reason() -> None:
    payload = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"error_reason": "some_new_unmapped_reason"}}},
    }
    assert normalize_decline_category("payment.failed", payload) == "UNKNOWN"
