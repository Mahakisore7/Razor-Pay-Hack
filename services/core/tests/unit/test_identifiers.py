"""CaseId/SignalId/etc. are UUIDv7 at runtime, distinct types under mypy.

DOMAIN-MODEL SS2.2: CustomerRef carries only a contact_hash, never a raw
phone number, so an accidental log of a domain object cannot leak PII.
"""

from uuid import UUID

import pytest

from recoup.domain.identifiers import CaseId, CustomerRef, hash_contact, uuid7


def test_uuid7_generates_valid_uuid() -> None:
    generated = uuid7()
    assert isinstance(generated, UUID)
    assert generated.version == 7


def test_uuid7_generates_unique_values() -> None:
    values = {uuid7() for _ in range(1000)}
    assert len(values) == 1000


def test_case_id_is_a_plain_uuid_at_runtime() -> None:
    raw = uuid7()
    case_id = CaseId(raw)
    assert isinstance(case_id, UUID)
    assert case_id == raw


def test_hash_contact_is_stable_sha256() -> None:
    first = hash_contact("+919876543210")
    second = hash_contact("+919876543210")
    assert first == second
    assert len(first) == 64  # sha256 hex digest length


def test_hash_contact_never_contains_the_raw_number() -> None:
    digest = hash_contact("+919876543210")
    assert "9876543210" not in digest


def test_hash_contact_rejects_non_e164() -> None:
    with pytest.raises(ValueError, match=r"E\.164"):
        hash_contact("9876543210")


def test_customer_ref_holds_only_hash_no_raw_contact() -> None:
    ref = CustomerRef(
        id="c_123",
        razorpay_customer_id="cust_abc",
        contact_hash=hash_contact("+919876543210"),
    )
    assert ref.contact_hash != "+919876543210"
