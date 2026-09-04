"""Pure domain model: entities, value objects, state machines.

No I/O, no framework imports, no clock access, no randomness. Everything here
is a function of its inputs, which is what makes the pipeline replayable."""

from recoup.domain.action import Action, ActionPayload, Channel
from recoup.domain.case import (
    TERMINAL_STATES,
    Arm,
    Case,
    CaseState,
    CostCeilingExceededError,
    IllegalTransition,
    assign_arm,
)
from recoup.domain.consent import ConsentEvent, ConsentSource, consent_at
from recoup.domain.decline import DeclineCategory, RetryHorizon
from recoup.domain.diagnosis import Diagnosis, DiagnosisMethod, Evidence, Hypothesis, RootCause
from recoup.domain.errors import RecoupError
from recoup.domain.identifiers import (
    ActionId,
    AuditEventId,
    CaseId,
    CustomerRef,
    SignalId,
    hash_contact,
    uuid7,
)
from recoup.domain.mandate import (
    Frequency,
    Mandate,
    MandateAmountExceededError,
    MandateBudgetExhaustedError,
    MandateNotValidError,
    MandateRail,
    MandateStatus,
)
from recoup.domain.money import Currency, Money
from recoup.domain.outcome import Outcome, OutcomeKind
from recoup.domain.plan import Plan, PlannedStep
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.domain.signal import LeakClass, Signal, SignalContext

__all__ = [
    "TERMINAL_STATES",
    "Action",
    "ActionId",
    "ActionPayload",
    "Arm",
    "AuditEventId",
    "Case",
    "CaseId",
    "CaseState",
    "Channel",
    "ConsentEvent",
    "ConsentSource",
    "CostCeilingExceededError",
    "Currency",
    "CustomerRef",
    "DeclineCategory",
    "Diagnosis",
    "DiagnosisMethod",
    "Evidence",
    "Frequency",
    "Hypothesis",
    "IllegalTransition",
    "LeakClass",
    "Mandate",
    "MandateAmountExceededError",
    "MandateBudgetExhaustedError",
    "MandateNotValidError",
    "MandateRail",
    "MandateStatus",
    "Money",
    "Outcome",
    "OutcomeKind",
    "Plan",
    "PlannedStep",
    "PolicyDecision",
    "RecoupError",
    "RetryHorizon",
    "RootCause",
    "Signal",
    "SignalContext",
    "SignalId",
    "Verdict",
    "assign_arm",
    "consent_at",
    "hash_contact",
    "uuid7",
]
