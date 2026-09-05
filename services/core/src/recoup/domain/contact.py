"""ContactEvent -- every outbound contact to a customer, independent of
consent (DATA-MODEL SS3.4-adjacent `contact_events` table). What R7's
frequency cap (POLICY-ENGINE SS3) counts against: consent answers "may we
contact this customer on this channel," frequency cap answers "have we
contacted them too often regardless" -- a customer can have full consent
and still be over-contacted.

Deliberately carries no `case_id`: R7 is counted across *all* of a
customer's cases, not per case, so the fact this rule needs is "we
contacted this customer," not "this case contacted this customer."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from recoup.domain.action import Channel
from recoup.domain.identifiers import CustomerRef

__all__ = ["ContactEvent"]


@dataclass(frozen=True, slots=True)
class ContactEvent:
    customer: CustomerRef
    channel: Channel
    occurred_at: datetime
