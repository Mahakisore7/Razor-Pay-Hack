"""PolicyContext (POLICY-ENGINE SS4) -- everything a rule needs, gathered
once and passed in as values, never a repository a rule could query itself.
A rule that touches the database is a rule that cannot be unit-tested or
exactly replayed from a `PolicyDecision`'s recorded `inputs`.

Scoped to exactly what T2.5/T4.1's rules (kill switch, domain guards,
consent, DND, quiet hours, frequency cap, cost ceiling, mandate budget,
approval threshold, rate limits) consume. POLICY-ENGINE SS4's full
contract also carries `promise_to_pay` -- that arrives with R2 (stopping
rules), in whichever phase builds it, not before. Notably absent for the
same reason: a `playbook: Playbook` field. None of the rules here need
more than the playbook's id (R1's per-playbook kill switch), and
`recoup.planning` sits above `recoup.policy` in the layering contract --
policy cannot import it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from recoup.domain.action import Channel
from recoup.domain.case import Case
from recoup.domain.consent import ConsentEvent
from recoup.domain.contact import ContactEvent
from recoup.domain.mandate import Mandate
from recoup.domain.money import Money

__all__ = ["DndStatus", "KillSwitchState", "PolicyContext"]


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    """Read from Redis with no cache, before `evaluate` is ever called --
    R1 (kill_switch.py) only ever sees this value, never touches Redis
    itself (POLICY-ENGINE SS3, R1)."""

    global_tripped: bool
    tripped_playbooks: frozenset[str]


@dataclass(frozen=True, slots=True)
class DndStatus:
    """Whether this customer is on the DND/NCPR registry, as of `PolicyContext.
    now` -- a single per-customer flag, not per-channel: TRAI's registry is
    keyed on the phone number itself, not on which channel a merchant picks
    (POLICY-ENGINE SS3, R5). Gathered by whatever builds a `PolicyContext`,
    the same way `KillSwitchState` is read from Redis before `evaluate` is
    ever called -- `dnd.py` only ever sees this value.
    """

    registered: bool


@dataclass(frozen=True, slots=True)
class PolicyContext:
    now: datetime
    case: Case
    playbook_id: str
    consent_events: tuple[ConsentEvent, ...]
    dnd_status: DndStatus
    # R6 (POLICY-ENGINE SS3): evaluated in the customer's own timezone,
    # not the server's -- P3 requires this to hold for any timezone.
    customer_timezone: ZoneInfo
    # R7: all of the customer's cases, not just this one -- "counted
    # across all cases for that customer, not per case" (POLICY-ENGINE
    # SS3). PolicyContext's own comment above still applies: a rule reads
    # this value, it never queries `contact_events` itself.
    contact_history: tuple[ContactEvent, ...]
    mandate: Mandate | None
    kill_switch: KillSwitchState
    # R11: a snapshot token count per channel, gathered the same way
    # `KillSwitchState` is -- a live token bucket lives in Redis, read
    # fresh before `evaluate` is ever called, never inside the rule
    # itself. A channel absent here is unconstrained, not exhausted.
    rate_limit_tokens: Mapping[Channel, int]
    # R8's global daily cap: total spend across *all* cases today, not
    # just this one -- the per-case ceiling's own blast-radius backstop
    # (POLICY-ENGINE SS3).
    daily_spend: Money
