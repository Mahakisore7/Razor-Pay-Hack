"""Action -- the only thing that touches the world (DOMAIN-MODEL SS7).

`idempotency_key` is derived, never accepted from the caller: the same
logical action recomputed after a crash must produce the same key so the
duplicate is suppressed, which only holds if nothing but `case_id`,
`step_id`, and `attempt` can influence it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from recoup.domain.identifiers import ActionId, CaseId
from recoup.domain.money import Money

__all__ = ["Action", "ActionPayload", "Channel"]


class Channel(StrEnum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    VOICE = "voice"
    PAYMENT_RETRY = "payment_retry"
    LINK = "link"
    HUMAN_REVIEW = "human_review"


@dataclass(frozen=True, slots=True)
class ActionPayload:
    """Channel-specific content. Generic on purpose -- what a payload needs
    varies by channel, and the channel implementations that consume this
    (execution/channels/*) are Phase 2+."""

    template: str | None = None
    variables: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True, init=False)
class Action:
    id: ActionId
    case_id: CaseId
    step_id: str
    attempt: int
    channel: Channel
    idempotency_key: str
    payload: ActionPayload
    cost: Money
    due_at: datetime

    def __init__(
        self,
        *,
        id: ActionId,  # noqa: A002 -- matches DOMAIN-MODEL's `id` field name on every entity
        case_id: CaseId,
        step_id: str,
        attempt: int,
        channel: Channel,
        payload: ActionPayload,
        cost: Money,
        due_at: datetime,
    ) -> None:
        if attempt < 1:
            raise ValueError(f"Action.attempt must be >= 1, got {attempt}")
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "case_id", case_id)
        object.__setattr__(self, "step_id", step_id)
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "cost", cost)
        object.__setattr__(self, "due_at", due_at)
        key = hashlib.sha256(f"{case_id}|{step_id}|{attempt}".encode()).hexdigest()
        object.__setattr__(self, "idempotency_key", key)
