"""PromiseToPay -- a customer's commitment to a future payment date
(POLICY-ENGINE SS3, R2's `promise_to_pay_active`).

A value object, not an entity: recorded once (by a human agent or an IVR
flow, in whichever phase builds that capture path) and read by the policy
engine as a fact about the world, never mutated by it. `is_active` is the
only behaviour the rule needs -- "a date still in the future" -- kept here
rather than duplicated as an inline comparison in `policy.rules.stopping`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from recoup.domain.identifiers import CaseId

__all__ = ["PromiseToPay"]


@dataclass(frozen=True, slots=True)
class PromiseToPay:
    case_id: CaseId
    committed_date: date
    recorded_at: datetime

    def is_active(self, now: datetime) -> bool:
        return self.committed_date > now.date()
