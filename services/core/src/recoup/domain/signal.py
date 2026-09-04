"""Signal -- produced only by deterministic detectors (DOMAIN-MODEL SS3).

Immutable, and never mutated or deleted after creation: a retracted signal
is superseded by a new one referencing it, not edited in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import CustomerRef, SignalId
from recoup.domain.money import Money

__all__ = ["LeakClass", "Signal", "SignalContext"]


class LeakClass(StrEnum):
    """Where in the payment lifecycle a leak occurs (PRD leak-class table)."""

    L1_FAILED_ONE_TIME_PAYMENT = "L1"
    L2_FAILED_MANDATE_DEBIT = "L2"
    L3_HALTED_SUBSCRIPTION = "L3"
    L4_ABANDONED_CHECKOUT = "L4"
    L5_OVERDUE_RECEIVABLE = "L5"
    L6_SUCCESS_RATE_DEGRADATION = "L6"


@dataclass(frozen=True, slots=True)
class SignalContext:
    """Whatever routing context the raw event carried -- issuer, card BIN, PSP, etc."""

    issuer: str | None = None
    bin: str | None = None
    psp: str | None = None
    instrument: str | None = None
    method: str | None = None


@dataclass(frozen=True, slots=True)
class Signal:
    id: SignalId
    leak_class: LeakClass
    customer: CustomerRef
    at_risk: Money
    detected_at: datetime  # from an injected clock, never datetime.now()
    source_event_ids: tuple[str, ...]
    decline: DeclineCategory | None
    context: SignalContext

    def __post_init__(self) -> None:
        if self.at_risk.paise <= 0:
            raise ValueError(
                "Signal.at_risk must be positive -- a zero-value signal is a detector bug"
            )
        if not self.source_event_ids:
            raise ValueError(
                "Signal.source_event_ids must be non-empty -- every signal traces to raw events"
            )
