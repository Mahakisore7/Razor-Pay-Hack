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

__all__ = ["NON_CONTACT_CHANNELS", "Action", "ActionCategory", "ActionPayload", "Channel"]


class Channel(StrEnum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    VOICE = "voice"
    PAYMENT_RETRY = "payment_retry"
    LINK = "link"
    HUMAN_REVIEW = "human_review"


# `payment_retry` is a machine-to-machine gateway re-presentation and
# `link` only generates a Razorpay payment link without delivering it
# (execution/channels/link.py) -- neither one is a message a customer
# perceives, so neither is "contact" for any policy rule keyed on that
# question (consent, DND, quiet hours, frequency cap). Shared here since
# R4/R7 and the executor's contact-history writer all need the identical
# set kept in sync.
NON_CONTACT_CHANNELS: frozenset[Channel] = frozenset({Channel.PAYMENT_RETRY, Channel.LINK})


class ActionCategory(StrEnum):
    """POLICY-ENGINE SS3 R5: transactional notifications (a pre-debit
    notice, a payment receipt) are exempt from DND by regulation;
    promotional ones are not. Declared by the playbook step that produces
    the action (schema.py), the same way `channel` is."""

    TRANSACTIONAL = "transactional"
    PROMOTIONAL = "promotional"


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
    category: ActionCategory
    # R9 (POLICY-ENGINE SS3): read straight off the playbook step that
    # produced this action, the same way `category` is. Defaults to
    # `False`, mirroring `PlaybookStep.consumes_mandate_budget`'s own
    # default -- most steps do not touch a mandate's representation
    # budget at all, so leaving it unset is a real, common answer, not
    # an oversight the way an unset `channel`/`category` would be.
    consumes_mandate_budget: bool
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
        category: ActionCategory,
        payload: ActionPayload,
        cost: Money,
        due_at: datetime,
        consumes_mandate_budget: bool = False,
    ) -> None:
        if attempt < 1:
            raise ValueError(f"Action.attempt must be >= 1, got {attempt}")
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "case_id", case_id)
        object.__setattr__(self, "step_id", step_id)
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "consumes_mandate_budget", consumes_mandate_budget)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "cost", cost)
        object.__setattr__(self, "due_at", due_at)
        # `category`/`consumes_mandate_budget` are deliberately not part
        # of the hash (DOMAIN-MODEL SS7): the same logical action
        # recomputed after a crash must produce the same key so the
        # duplicate is suppressed, which only holds if nothing but
        # `case_id`, `step_id`, and `attempt` can influence it.
        key = hashlib.sha256(f"{case_id}|{step_id}|{attempt}".encode()).hexdigest()
        object.__setattr__(self, "idempotency_key", key)
