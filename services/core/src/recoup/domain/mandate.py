"""Mandate -- a scarce resource (DOMAIN-MODEL SS11).

Re-presentation budget behaves like a currency: finite per cycle,
non-transferable, and spent whether or not the retry succeeds. Modelled as
a frozen value object, like the rest of the domain -- `with_representation_
used()` returns a new `Mandate` rather than mutating in place, the same
immutable-update pattern `dataclasses.replace` exists for.

Debiting above `max_amount` or outside `[valid_from, valid_until]` is
refused here, before it can reach the gateway.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from recoup.domain.errors import RecoupError
from recoup.domain.identifiers import CustomerRef
from recoup.domain.money import Money

__all__ = [
    "Frequency",
    "Mandate",
    "MandateAmountExceededError",
    "MandateBudgetExhaustedError",
    "MandateNotValidError",
    "MandateRail",
    "MandateStatus",
]


class MandateRail(StrEnum):
    UPI_AUTOPAY = "upi_autopay"
    ENACH = "enach"
    EMANDATE = "emandate"
    CARD = "card"


class Frequency(StrEnum):
    """Razorpay's mandate/subscription charge frequencies."""

    AS_PRESENTED = "as_presented"  # UPI Autopay only: charged when presented, no fixed cadence
    DAILY = "daily"
    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY = "monthly"
    BIMONTHLY = "bimonthly"
    QUARTERLY = "quarterly"
    HALF_YEARLY = "half_yearly"
    YEARLY = "yearly"


class MandateStatus(StrEnum):
    PENDING = "pending"  # created, not yet authorised by the customer
    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"
    EXPIRED = "expired"


class MandateAmountExceededError(RecoupError):
    def __init__(self, mandate_id: str, amount: Money, max_amount: Money) -> None:
        self.mandate_id = mandate_id
        self.amount = amount
        self.max_amount = max_amount
        super().__init__(
            f"mandate {mandate_id}: debit {amount.paise}p exceeds max_amount {max_amount.paise}p"
        )


class MandateNotValidError(RecoupError):
    def __init__(self, mandate_id: str, at: date, valid_from: date, valid_until: date) -> None:
        self.mandate_id = mandate_id
        self.at = at
        self.valid_from = valid_from
        self.valid_until = valid_until
        super().__init__(
            f"mandate {mandate_id}: {at} is outside validity [{valid_from}, {valid_until}]"
        )


class MandateBudgetExhaustedError(RecoupError):
    def __init__(self, mandate_id: str, representation_cap: int) -> None:
        self.mandate_id = mandate_id
        self.representation_cap = representation_cap
        super().__init__(
            f"mandate {mandate_id}: representation cap {representation_cap} already reached"
        )


@dataclass(frozen=True, slots=True)
class Mandate:
    id: str
    customer: CustomerRef
    rail: MandateRail
    max_amount: Money
    frequency: Frequency
    valid_from: date
    valid_until: date
    status: MandateStatus
    representations_used_this_cycle: int
    representation_cap: int

    @property
    def remaining_representations(self) -> int:
        return self.representation_cap - self.representations_used_this_cycle

    def authorize_debit(self, amount: Money, at: date) -> None:
        """Raise if `amount` at `at` cannot be debited against this mandate.

        A pure check, not a mutation -- refusal is the domain layer's job;
        actually spending the representation budget is `with_representation_
        used()`, a separate step, since a debit can be authorised and still
        fail at the gateway.
        """
        if amount > self.max_amount:
            raise MandateAmountExceededError(self.id, amount, self.max_amount)
        if not (self.valid_from <= at <= self.valid_until):
            raise MandateNotValidError(self.id, at, self.valid_from, self.valid_until)

    def with_representation_used(self) -> Mandate:
        """Return a new `Mandate` with one representation spent.

        Raises if the cycle's cap is already reached -- spent whether or not
        the retry succeeds, so this is called once the attempt is made, not
        once it succeeds.
        """
        if self.representations_used_this_cycle >= self.representation_cap:
            raise MandateBudgetExhaustedError(self.id, self.representation_cap)
        return replace(
            self, representations_used_this_cycle=self.representations_used_this_cycle + 1
        )
