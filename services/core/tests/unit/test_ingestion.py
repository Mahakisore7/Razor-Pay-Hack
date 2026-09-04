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


def test_extract_decline_reason_is_none_for_a_non_payment_event() -> None:
    assert extract_decline_reason("subscription.halted", _PAYMENT_FAILED) is None


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
