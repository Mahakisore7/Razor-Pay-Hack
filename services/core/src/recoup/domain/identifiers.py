"""Domain identifiers and customer references (DOMAIN-MODEL SS2.2).

IDs are ``NewType``-wrapped UUIDs (ENGINEERING-STANDARDS SS2.2): `CaseId` and
`SignalId` are both plain UUIDs at runtime, but mypy treats them as distinct
types, so passing one where the other is expected is a type error caught at
review time, not a runtime mystery discovered days later.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import NewType
from uuid import UUID

import uuid_utils

__all__ = [
    "ActionId",
    "AuditEventId",
    "CaseId",
    "CustomerRef",
    "SignalId",
    "hash_contact",
    "uuid7",
]

CaseId = NewType("CaseId", UUID)
SignalId = NewType("SignalId", UUID)
ActionId = NewType("ActionId", UUID)
AuditEventId = NewType("AuditEventId", UUID)

_E164 = re.compile(r"^\+[1-9]\d{1,14}$")


def uuid7() -> UUID:
    """A time-ordered UUIDv7, as the stdlib `UUID` type the rest of the domain uses."""
    return UUID(bytes=uuid_utils.uuid7().bytes)


def hash_contact(phone_e164: str) -> str:
    """SHA-256 of an E.164 phone number, for dedup without ever storing the number itself."""
    if not _E164.match(phone_e164):
        raise ValueError(f"expected an E.164 phone number, got {phone_e164!r}")
    return hashlib.sha256(phone_e164.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class CustomerRef:
    """An opaque reference to a customer -- never a raw identifier in logs.

    PII (phone, email, name) lives in a separate access-logged
    ``customer_pii`` table; this is what flows through the pipeline, so an
    accidental log of a domain object cannot leak a phone number.
    """

    id: str
    razorpay_customer_id: str | None
    contact_hash: str
