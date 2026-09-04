"""The input contract detectors run over (TR-6).

Detectors are pure functions of `(event, snapshot, clock)` -- decoupled
from the SQLAlchemy `RawEvent` row and from however a customer's identity
gets resolved, so a golden-fixture test builds these directly with no
database in reach.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import CustomerRef

__all__ = ["DetectionSnapshot", "InboundEvent"]


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """The subset of a stored `RawEvent` a detector needs (TR-4:
    interpretation runs over what is already stored, not a re-fetch)."""

    provider_event_id: str
    event_type: str
    payload: dict[str, Any]
    decline_category: DeclineCategory | None
    received_at: datetime


@dataclass(frozen=True, slots=True)
class DetectionSnapshot:
    """Read-only repository state a detector needs beyond the event
    itself -- resolved by the caller before the detector runs, so the
    detector never touches a database (TR-6).

    `customer` is already-resolved: whichever party calls a detector has
    already turned the payload's raw Razorpay customer id into an
    internal `CustomerRef` (a database lookup), so the detector deals only
    in domain identity, never a provider's raw string id.

    `already_detected` makes detection idempotent over a replayed or
    re-run raw event (TR-4) without every detector re-deriving that check
    from a signals-table query itself.
    """

    customer: CustomerRef
    already_detected: bool
