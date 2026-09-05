"""PolicyContext (POLICY-ENGINE SS4) -- everything a rule needs, gathered
once and passed in as values, never a repository a rule could query itself.
A rule that touches the database is a rule that cannot be unit-tested or
exactly replayed from a `PolicyDecision`'s recorded `inputs`.

Scoped to exactly what T2.5/T4.1's rules (kill switch, domain guards,
consent, DND, cost ceiling) consume. POLICY-ENGINE SS4's full contract
also carries `contact_history`, `promise_to_pay`, `daily_spend`,
`customer_timezone`, and `rate_limit_tokens` -- those arrive with the
rules that actually read them (R2, R6, R7, R9-R11), in whichever phase
builds them, not before. Notably absent for the same reason: a
`playbook: Playbook` field. None of the rules here need more than the
playbook's id (R1's per-playbook kill switch), and `recoup.planning` sits
above `recoup.policy` in the layering contract -- policy cannot import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from recoup.domain.case import Case
from recoup.domain.consent import ConsentEvent
from recoup.domain.mandate import Mandate

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
    mandate: Mandate | None
    kill_switch: KillSwitchState
