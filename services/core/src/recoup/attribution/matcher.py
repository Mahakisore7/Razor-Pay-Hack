"""Deterministic payment-to-case matching (METRICS-AND-KPIS SS6, TR-28..
TR-30). Pure: no I/O, no clock access -- every timestamp involved is a
value the caller already computed, which is what keeps this exhaustively
branch-testable and reproducible under Hypothesis.

A payment is attributed to a case only when all hold: same customer,
amount within tolerance of `at_risk` (or a genuine partial payment, still
inside tolerance's "not an overpayment" side), the payment falls inside
the 72-hour attribution window, and -- enforced by the caller, which only
ever offers open cases as candidates -- the case is not already terminal.
Contention (more than one candidate matches) resolves to the *older*
case, `opened_at` order (TR-29); the loser(s) are reported, never
silently dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from recoup.domain.identifiers import CaseId
from recoup.domain.money import Money
from recoup.domain.outcome import OutcomeKind

__all__ = [
    "ATTRIBUTION_WINDOW",
    "CaseCandidate",
    "MatchResult",
    "PaymentInfo",
    "match_payment",
]

ATTRIBUTION_WINDOW = timedelta(hours=72)

_MIN_TOLERANCE_PAISE = 100  # Rs 1
_TOLERANCE_NUMERATOR = 5  # 0.5% == 5 / 1000, kept an exact integer fraction --
_TOLERANCE_DENOMINATOR = 1000  # `Money` has no fractional multiply, deliberately (SS2.1)


@dataclass(frozen=True, slots=True)
class PaymentInfo:
    """The subset of a captured payment attribution needs."""

    id: str
    razorpay_customer_id: str
    amount: Money
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class CaseCandidate:
    """The subset of an open case attribution needs -- scoped down to
    values rather than the full `Case` entity, the same shape
    `PolicyContext` (T2.5) uses to keep a decision function decoupled
    from everything it doesn't touch.
    """

    case_id: CaseId
    razorpay_customer_id: str
    at_risk: Money
    opened_at: datetime  # case creation -- what "older" means for TR-29 contention
    window_anchor: datetime  # opened_at for holdout (TR-30); else the most recent executed action
    window_step_id: str | None  # the step credited if this case wins; None for holdout


@dataclass(frozen=True, slots=True)
class MatchResult:
    winner: CaseId | None
    kind: OutcomeKind | None  # RECOVERED or PARTIALLY_RECOVERED; None iff winner is None
    recovered: Money | None  # the actual payment amount, never rounded up; None iff winner is None
    step_id: str | None
    ambiguous_with: tuple[CaseId, ...]  # other candidates that also matched and lost (TR-29)


def _tolerance(at_risk: Money) -> Money:
    """`max(Rs 1, 0.5% of at_risk)`, in exact integer paise."""
    pct = (at_risk.paise * _TOLERANCE_NUMERATOR) // _TOLERANCE_DENOMINATOR
    return Money(max(_MIN_TOLERANCE_PAISE, pct), at_risk.currency)


def _classify(payment_amount: Money, at_risk: Money) -> OutcomeKind | None:
    if payment_amount.paise <= 0:
        return None
    tolerance_paise = _tolerance(at_risk).paise
    diff = payment_amount.paise - at_risk.paise
    if abs(diff) <= tolerance_paise:
        return OutcomeKind.RECOVERED
    if diff < -tolerance_paise:
        return OutcomeKind.PARTIALLY_RECOVERED
    return None  # overpays this case beyond tolerance -- not a match here


def _in_window(captured_at: datetime, candidate: CaseCandidate) -> bool:
    return candidate.window_anchor <= captured_at <= candidate.window_anchor + ATTRIBUTION_WINDOW


def match_payment(payment: PaymentInfo, candidates: Sequence[CaseCandidate]) -> MatchResult:
    """TR-28: deterministic and total. The same `(payment, candidates)`
    pair always produces the same result, and every candidate is either a
    match, ambiguous, or excluded -- nothing is left undecided.
    """
    matches: list[tuple[CaseCandidate, OutcomeKind]] = []
    for candidate in candidates:
        if candidate.razorpay_customer_id != payment.razorpay_customer_id:
            continue
        if not _in_window(payment.captured_at, candidate):
            continue
        kind = _classify(payment.amount, candidate.at_risk)
        if kind is None:
            continue
        matches.append((candidate, kind))

    if not matches:
        return MatchResult(winner=None, kind=None, recovered=None, step_id=None, ambiguous_with=())

    # TR-29: the older case wins -- `opened_at`, ties broken by `case_id`
    # (a UUIDv7, itself time-ordered) so the result stays deterministic
    # even if two candidates share a timestamp exactly.
    matches.sort(key=lambda pair: (pair[0].opened_at, str(pair[0].case_id)))
    winner, kind = matches[0]
    ambiguous = tuple(candidate.case_id for candidate, _ in matches[1:])

    return MatchResult(
        winner=winner.case_id,
        kind=kind,
        recovered=payment.amount,
        step_id=winner.window_step_id,
        ambiguous_with=ambiguous,
    )
