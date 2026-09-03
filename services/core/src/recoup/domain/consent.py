"""Consent -- folded from a ledger, not stored as a boolean (DOMAIN-MODEL SS12).

Compliance does not ask "is this customer opted in now" -- it asks "were
they opted in when you contacted them." A boolean column cannot answer that
after the fact; an append-only ledger can, if it is folded correctly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from recoup.domain.action import Channel
from recoup.domain.identifiers import CustomerRef

__all__ = ["ConsentEvent", "ConsentSource", "consent_at"]


class ConsentSource(StrEnum):
    CHECKOUT = "checkout"
    SMS_STOP = "sms_stop"
    DASHBOARD = "dashboard"
    DND_SYNC = "dnd_sync"


@dataclass(frozen=True, slots=True)
class ConsentEvent:
    customer: CustomerRef
    channel: Channel
    granted: bool
    source: ConsentSource
    occurred_at: datetime


def consent_at(events: Sequence[ConsentEvent], channel: Channel, when: datetime) -> bool:
    """Consent state on `channel` as of `when`. Absence of a record means refusal.

    Picks the relevant event with the latest `occurred_at`, not the last
    element of `events` -- a caller is not guaranteed to hand this an
    already-sorted ledger (an unordered DB fetch, events merged from more
    than one source), and a wrong answer here is a compliance failure, not
    a cosmetic one.
    """
    relevant = [event for event in events if event.channel == channel and event.occurred_at <= when]
    if not relevant:
        return False
    return max(relevant, key=lambda event: event.occurred_at).granted
