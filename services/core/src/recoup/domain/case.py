"""Case -- the unit of work (DOMAIN-MODEL SS4).

Transitions are an explicit table. Anything not in the table raises
`IllegalTransition` -- the case does not silently move.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from recoup.domain.diagnosis import Diagnosis
from recoup.domain.errors import RecoupError
from recoup.domain.identifiers import CaseId, CustomerRef, SignalId
from recoup.domain.money import Money
from recoup.domain.outcome import Outcome
from recoup.domain.plan import Plan

__all__ = [
    "DEFAULT_ARM_WEIGHTS",
    "TERMINAL_STATES",
    "Arm",
    "Case",
    "CaseState",
    "CostCeilingExceededError",
    "IllegalTransition",
    "assign_arm",
]


class Arm(StrEnum):
    CONTROL = "control"
    BASELINE = "baseline"
    TREATMENT = "treatment"


# PHASE-03 T3.2: 10% control, 10% baseline, 80% treatment -- few cases
# sacrificed to the counterfactual arms, most into the arm the product
# actually bets on. A caller with its own config (T3.5's benchmark
# runner, a future live-traffic settings field) overrides this rather
# than being stuck with it.
DEFAULT_ARM_WEIGHTS: Mapping[Arm, float] = MappingProxyType(
    {Arm.CONTROL: 0.10, Arm.BASELINE: 0.10, Arm.TREATMENT: 0.80}
)


def assign_arm(
    seed: int, case_id: CaseId, weights: Mapping[Arm, float] = DEFAULT_ARM_WEIGHTS
) -> Arm:
    """Deterministically assign an arm from `hash(seed | case_id)`, split
    according to `weights` (PHASE-03 T3.2 checklist: "configurable").

    Python's builtin `hash()` is randomised per-process for str/bytes
    (`PYTHONHASHSEED`), which would make arm assignment non-reproducible --
    fatal for A1.7, the determinism gate every benchmark number depends on.
    `sha256` is stable across processes and interpreters instead.

    A weighted cumulative-threshold pick, not `hash % len(arms)`: the
    latter can only ever produce a uniform split, and T3.2 explicitly
    calls for a 10/10/80 split, not 33/33/33. Same technique
    `gateway.simulator.world` and `bench.cohort` already use for their own
    weighted sampling, for the same reason.
    """
    digest = hashlib.sha256(f"{seed}|{case_id}".encode()).digest()
    roll = int.from_bytes(digest[:8], "big") / 2**64
    total = sum(weights.values())
    threshold = roll * total
    cumulative = 0.0
    ordered = sorted(weights, key=str)
    for arm in ordered:
        cumulative += weights[arm]
        if threshold < cumulative:
            return arm
    # Unreachable in practice: `roll` is strictly < 1.0 (a 64-bit numerator
    # over a 2**64 denominator), so `threshold` is strictly less than the
    # final cumulative total and the loop above always returns first.
    return ordered[-1]  # pragma: no cover


class CaseState(StrEnum):
    DETECTED = "detected"
    DIAGNOSING = "diagnosing"
    ABSTAINED = "abstained"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    HOLDOUT = "holdout"
    EXECUTING = "executing"
    ESCALATED = "escalated"
    AWAITING_OUTCOME = "awaiting_outcome"
    RECOVERED = "recovered"
    PARTIALLY_RECOVERED = "partially_recovered"
    LOST = "lost"
    EXPIRED = "expired"
    SUPPRESSED = "suppressed"


_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.DETECTED: frozenset({CaseState.DIAGNOSING, CaseState.SUPPRESSED}),
    CaseState.DIAGNOSING: frozenset({CaseState.PLANNED, CaseState.ABSTAINED, CaseState.SUPPRESSED}),
    CaseState.ABSTAINED: frozenset({CaseState.PLANNED, CaseState.SUPPRESSED}),
    CaseState.PLANNED: frozenset(
        {
            CaseState.EXECUTING,
            CaseState.AWAITING_APPROVAL,
            CaseState.HOLDOUT,
            CaseState.SUPPRESSED,
        }
    ),
    CaseState.AWAITING_APPROVAL: frozenset({CaseState.EXECUTING, CaseState.SUPPRESSED}),
    CaseState.HOLDOUT: frozenset({CaseState.AWAITING_OUTCOME}),
    CaseState.EXECUTING: frozenset(
        {
            CaseState.EXECUTING,
            CaseState.AWAITING_OUTCOME,
            CaseState.ESCALATED,
            CaseState.SUPPRESSED,
        }
    ),
    CaseState.ESCALATED: frozenset({CaseState.EXECUTING, CaseState.SUPPRESSED}),
    CaseState.AWAITING_OUTCOME: frozenset(
        {
            CaseState.RECOVERED,
            CaseState.PARTIALLY_RECOVERED,
            CaseState.LOST,
            CaseState.EXPIRED,
        }
    ),
    # Terminal states (RECOVERED, PARTIALLY_RECOVERED, LOST, EXPIRED,
    # SUPPRESSED) have no key here, and therefore no outgoing transitions --
    # that omission is what makes I1 ("exactly one terminal state, exactly
    # once") hold structurally rather than by convention.
}

TERMINAL_STATES: frozenset[CaseState] = frozenset(
    state for state in CaseState if state not in _TRANSITIONS
)


class IllegalTransition(RecoupError):  # noqa: N818 -- name is spec-mandated, DOMAIN-MODEL SS4.1
    def __init__(self, case_id: CaseId, from_state: CaseState, to_state: CaseState) -> None:
        self.case_id = case_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"case {case_id}: illegal transition {from_state} -> {to_state}")


class CostCeilingExceededError(RecoupError):
    def __init__(self, case_id: CaseId, attempted: Money, ceiling: Money) -> None:
        self.case_id = case_id
        self.attempted = attempted
        self.ceiling = ceiling
        super().__init__(
            f"case {case_id}: cost {attempted.paise}p would exceed ceiling {ceiling.paise}p"
        )


@dataclass(slots=True, kw_only=True)
class Case:
    """Mutable, unlike the domain's value objects: a `Case` is an entity that
    lives through a state machine, not a fact fixed at construction."""

    id: CaseId
    signal_id: SignalId
    customer: CustomerRef
    at_risk: Money
    state: CaseState
    arm: Arm
    opened_at: datetime
    cost_spent: Money
    cost_ceiling: Money
    # The originating Signal's own field of the same name (T3.5) -- carried
    # here so a `payment_retry` action's payload can be populated without
    # every planning-side caller re-fetching the Signal.
    source_payment_id: str | None = None
    diagnosis: Diagnosis | None = None
    plan: Plan | None = None
    terminal_outcome: Outcome | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition_to(self, new_state: CaseState) -> None:
        # I7: a control-arm case never reaches EXECUTING. Checked ahead of
        # the table lookup because PLANNED -> EXECUTING is otherwise a
        # structurally legal transition for any arm -- this is what makes
        # I7 a real guard and not just a restatement of the table.
        if new_state == CaseState.EXECUTING and self.arm == Arm.CONTROL:
            raise IllegalTransition(self.id, self.state, new_state)
        legal = _TRANSITIONS.get(self.state, frozenset())
        if new_state not in legal:
            raise IllegalTransition(self.id, self.state, new_state)
        self.state = new_state

    def record_cost(self, amount: Money) -> None:
        """I2: `cost_spent <= cost_ceiling` at all times -- enforced here, the
        one place `cost_spent` is allowed to change, rather than trusted of
        every caller that touches it."""
        projected = self.cost_spent + amount
        if projected > self.cost_ceiling:
            raise CostCeilingExceededError(self.id, projected, self.cost_ceiling)
        self.cost_spent = projected
