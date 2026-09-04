"""Unit tests for `recoup.attribution.matcher` (T2.8). Pure, so every
branch is reachable from values alone -- TR-31 asks for 100% branch
coverage here specifically, and this file is written to hit each one by
name rather than incidentally.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from recoup.attribution.matcher import (
    ATTRIBUTION_WINDOW,
    CaseCandidate,
    PaymentInfo,
    match_payment,
)
from recoup.domain.identifiers import CaseId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.outcome import OutcomeKind

_NOW = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
_CUSTOMER = "cust_1"


def _payment(
    *, amount_paise: int, captured_at: datetime = _NOW, customer: str = _CUSTOMER
) -> PaymentInfo:
    return PaymentInfo(
        id="pay_1",
        razorpay_customer_id=customer,
        amount=Money(amount_paise),
        captured_at=captured_at,
    )


def _candidate(
    *,
    at_risk_paise: int = 500_000,
    opened_at: datetime = _NOW,
    window_anchor: datetime = _NOW,
    customer: str = _CUSTOMER,
    step_id: str | None = "step-1",
    case_id: CaseId | None = None,
) -> CaseCandidate:
    return CaseCandidate(
        case_id=case_id if case_id is not None else CaseId(uuid7()),
        razorpay_customer_id=customer,
        at_risk=Money(at_risk_paise),
        opened_at=opened_at,
        window_anchor=window_anchor,
        window_step_id=step_id,
    )


# --- no match --------------------------------------------------------------------


def test_no_candidates_is_no_match() -> None:
    result = match_payment(_payment(amount_paise=500_000), [])

    assert result.winner is None
    assert result.kind is None
    assert result.recovered is None
    assert result.step_id is None
    assert result.ambiguous_with == ()


def test_a_different_customer_never_matches() -> None:
    candidate = _candidate(customer="cust_2")

    result = match_payment(_payment(amount_paise=500_000, customer=_CUSTOMER), [candidate])

    assert result.winner is None


def test_a_payment_before_the_window_anchor_never_matches() -> None:
    candidate = _candidate(window_anchor=_NOW)
    payment = _payment(amount_paise=500_000, captured_at=_NOW - timedelta(seconds=1))

    result = match_payment(payment, [candidate])

    assert result.winner is None


def test_a_payment_after_the_window_never_matches() -> None:
    candidate = _candidate(window_anchor=_NOW)
    payment = _payment(
        amount_paise=500_000, captured_at=_NOW + ATTRIBUTION_WINDOW + timedelta(seconds=1)
    )

    result = match_payment(payment, [candidate])

    assert result.winner is None


def test_an_overpayment_beyond_tolerance_does_not_match() -> None:
    candidate = _candidate(at_risk_paise=500_000)  # tolerance = 2_500 paise
    payment = _payment(amount_paise=502_501)

    result = match_payment(payment, [candidate])

    assert result.winner is None


def test_a_non_positive_payment_never_matches() -> None:
    candidate = _candidate()
    payment = _payment(amount_paise=0)

    result = match_payment(payment, [candidate])

    assert result.winner is None


# --- window boundaries (inclusive on both ends) -----------------------------------


def test_a_payment_exactly_at_the_window_anchor_matches() -> None:
    candidate = _candidate(window_anchor=_NOW)
    payment = _payment(amount_paise=500_000, captured_at=_NOW)

    result = match_payment(payment, [candidate])

    assert result.winner == candidate.case_id


def test_a_payment_exactly_at_the_window_close_matches() -> None:
    candidate = _candidate(window_anchor=_NOW)
    payment = _payment(amount_paise=500_000, captured_at=_NOW + ATTRIBUTION_WINDOW)

    result = match_payment(payment, [candidate])

    assert result.winner == candidate.case_id


# --- amount classification --------------------------------------------------------


def test_an_exact_amount_match_is_recovered() -> None:
    candidate = _candidate(at_risk_paise=500_000)
    payment = _payment(amount_paise=500_000)

    result = match_payment(payment, [candidate])

    assert result.kind is OutcomeKind.RECOVERED
    assert result.recovered == Money(500_000)


def test_an_underpayment_within_tolerance_is_recovered() -> None:
    candidate = _candidate(at_risk_paise=500_000)  # tolerance = 2_500 paise
    payment = _payment(amount_paise=497_500)  # diff == -tolerance, boundary

    result = match_payment(payment, [candidate])

    assert result.kind is OutcomeKind.RECOVERED


def test_an_overpayment_within_tolerance_is_recovered() -> None:
    candidate = _candidate(at_risk_paise=500_000)
    payment = _payment(amount_paise=502_500)  # diff == +tolerance, boundary

    result = match_payment(payment, [candidate])

    assert result.kind is OutcomeKind.RECOVERED


def test_an_underpayment_beyond_tolerance_is_partially_recovered() -> None:
    candidate = _candidate(at_risk_paise=500_000)
    payment = _payment(amount_paise=300_000)

    result = match_payment(payment, [candidate])

    assert result.kind is OutcomeKind.PARTIALLY_RECOVERED
    assert result.recovered == Money(300_000)  # the actual amount, not rounded up


def test_the_minimum_rupee_one_tolerance_floor_applies_to_small_amounts() -> None:
    # 0.5% of 10_000 paise is 50 paise, below the Rs 1 (100 paise) floor.
    candidate = _candidate(at_risk_paise=10_000)
    payment = _payment(amount_paise=10_090)  # diff 90 paise: inside the floor, outside 0.5%

    result = match_payment(payment, [candidate])

    assert result.kind is OutcomeKind.RECOVERED


# --- contention (TR-29) ------------------------------------------------------------


def test_a_single_match_has_no_ambiguity() -> None:
    candidate = _candidate()

    result = match_payment(_payment(amount_paise=500_000), [candidate])

    assert result.winner == candidate.case_id
    assert result.ambiguous_with == ()


def test_contention_resolves_to_the_older_case() -> None:
    older = _candidate(opened_at=_NOW - timedelta(days=1))
    younger = _candidate(opened_at=_NOW)

    result = match_payment(_payment(amount_paise=500_000), [younger, older])

    assert result.winner == older.case_id
    assert result.ambiguous_with == (younger.case_id,)


def test_contention_ties_on_opened_at_break_by_case_id() -> None:
    low = _candidate(opened_at=_NOW, case_id=CaseId(uuid7()))
    high = _candidate(opened_at=_NOW, case_id=CaseId(uuid7()))
    expected_winner = min(low.case_id, high.case_id, key=str)
    expected_loser = high.case_id if expected_winner == low.case_id else low.case_id

    result = match_payment(_payment(amount_paise=500_000), [low, high])

    assert result.winner == expected_winner
    assert result.ambiguous_with == (expected_loser,)


def test_a_non_matching_candidate_is_excluded_from_a_contended_result() -> None:
    matching = _candidate()
    wrong_customer = _candidate(customer="cust_other")

    result = match_payment(_payment(amount_paise=500_000), [matching, wrong_customer])

    assert result.winner == matching.case_id
    assert result.ambiguous_with == ()


def test_the_winner_carries_its_own_window_step_id() -> None:
    candidate = _candidate(step_id="step-retry-2")

    result = match_payment(_payment(amount_paise=500_000), [candidate])

    assert result.step_id == "step-retry-2"


def test_a_holdout_style_candidate_with_no_step_id_wins_with_none() -> None:
    candidate = _candidate(step_id=None)

    result = match_payment(_payment(amount_paise=500_000), [candidate])

    assert result.step_id is None


def test_money_currency_is_preserved_through_a_match() -> None:
    candidate = _candidate(at_risk_paise=500_000)
    payment = PaymentInfo(
        id="pay_1",
        razorpay_customer_id=_CUSTOMER,
        amount=Money(500_000, Currency.INR),
        captured_at=_NOW,
    )

    result = match_payment(payment, [candidate])

    assert result.recovered is not None
    assert result.recovered.currency == Currency.INR
