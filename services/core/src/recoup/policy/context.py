"""PolicyContext (POLICY-ENGINE SS4) -- everything a rule needs, gathered
once and passed in as values, never a repository a rule could query itself.
A rule that touches the database is a rule that cannot be unit-tested or
exactly replayed from a `PolicyDecision`'s recorded `inputs`.

Scoped to exactly what T2.5/T4.1/T4.2's rules (kill switch, stopping
rules, domain guards, consent, DND, quiet hours, frequency cap, cost
ceiling, mandate budget, approval threshold, rate limits) consume.
Notably absent: a `playbook: Playbook` field. No rule here needs more
than a couple of scalars off it (R1's per-playbook kill switch id, R2's
`max_attempts`/`max_case_age_days`), and `recoup.planning` sits above
`recoup.policy` in the layering contract -- policy cannot import it, so
those scalars are carried directly rather than the object they came from.
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
from recoup.domain.promise import PromiseToPay

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
    # R2 (POLICY-ENGINE SS3, PHASE-04 T4.2): the playbook's own static
    # caps, carried as scalars rather than a `Playbook` reference (see
    # this module's own docstring). Both are copied from the same
    # playbook `case.playbook_id` already names, at the moment a
    # `PolicyContext` is built -- a rule reads them, it never resolves
    # `playbook_id` into a `Playbook` itself.
    max_attempts: int
    max_case_age_days: int
    # R2: no dispute-tracking data source exists yet -- see
    # `policy.rules.stopping`'s own docstring for why this stays a
    # permissive default (`False`) in the one caller that builds a
    # `PolicyContext` today, the same "no real backing source yet" shape
    # as `mandate`/`dnd_status`/`rate_limit_tokens` above.
    dispute_exists: bool
    # R2: an account-closure signal from the rail -- same "no real
    # backing source yet" shape as `dispute_exists` above.
    account_closed: bool
    # R2: `None` means no promise is on file, which is the common case
    # and also this field's own permissive default until a capture path
    # (a human agent's console action, an IVR flow) exists to populate it
    # for real.
    promise_to_pay: PromiseToPay | None
