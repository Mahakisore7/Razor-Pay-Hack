"""Outcome -- how a case ended, and why (DOMAIN-MODEL SS9).

`reason_code` is mandatory on every non-recovery outcome. This is what
populates the exception list in the benchmark report: a case that ends
without a reason is a case we cannot explain, and the constructor rejects it
rather than let the report go silent on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from recoup.domain.identifiers import CaseId
from recoup.domain.money import Money

__all__ = ["Outcome", "OutcomeKind"]


class OutcomeKind(StrEnum):
    RECOVERED = "recovered"
    PARTIALLY_RECOVERED = "partially_recovered"
    LOST = "lost"
    EXPIRED = "expired"
    SUPPRESSED = "suppressed"
    ESCALATED = "escalated"


_RECOVERY_KINDS = frozenset({OutcomeKind.RECOVERED, OutcomeKind.PARTIALLY_RECOVERED})


@dataclass(frozen=True, slots=True, init=False)
class Outcome:
    case_id: CaseId
    kind: OutcomeKind
    recovered: Money  # zero for non-recovery outcomes
    attributed_payment_id: str | None
    attributed_step_id: str | None  # which step gets credit
    reason_code: str | None
    resolved_at: datetime

    def __init__(
        self,
        *,
        case_id: CaseId,
        kind: OutcomeKind,
        recovered: Money,
        resolved_at: datetime,
        attributed_payment_id: str | None = None,
        attributed_step_id: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        # DOMAIN-MODEL's prose calls out SUPPRESSED/EXPIRED as examples; the
        # invariant is applied to every non-recovery kind (LOST, EXPIRED,
        # SUPPRESSED, ESCALATED) for the same reason it applies to those two.
        if kind not in _RECOVERY_KINDS and reason_code is None:
            raise ValueError(f"Outcome.reason_code is required for a non-recovery kind {kind!r}")
        object.__setattr__(self, "case_id", case_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "recovered", recovered)
        object.__setattr__(self, "attributed_payment_id", attributed_payment_id)
        object.__setattr__(self, "attributed_step_id", attributed_step_id)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "resolved_at", resolved_at)
