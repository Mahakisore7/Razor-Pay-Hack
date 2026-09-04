"""Property tests for `recoup.attribution.matcher.match_payment` (T2.8,
TR-31): across arbitrary candidate pools, the matcher never reports the
same payment as attributed to two cases, its declared winner always
precedes every other reported candidate in TR-29's own age order, and it
is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from recoup.attribution.matcher import ATTRIBUTION_WINDOW, CaseCandidate, PaymentInfo, match_payment
from recoup.domain.identifiers import CaseId, uuid7
from recoup.domain.money import Money

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

_customer_ids = st.sampled_from(["cust_a", "cust_b", "cust_c"])
_paise = st.integers(min_value=0, max_value=2_000_000)
_offset_hours = st.integers(min_value=-100, max_value=100)


def _dt(hours: int) -> datetime:
    return _EPOCH + timedelta(hours=hours)


@st.composite
def _candidates(draw: st.DrawFn) -> list[CaseCandidate]:
    count = draw(st.integers(min_value=0, max_value=6))
    result: list[CaseCandidate] = []
    for _ in range(count):
        result.append(
            CaseCandidate(
                case_id=CaseId(uuid7()),
                razorpay_customer_id=draw(_customer_ids),
                at_risk=Money(draw(_paise)),
                opened_at=_dt(draw(_offset_hours)),
                window_anchor=_dt(draw(_offset_hours)),
                window_step_id=draw(st.one_of(st.none(), st.text(min_size=1, max_size=8))),
            )
        )
    return result


def _payment(customer_id: str, amount_paise: int, captured_hour: int) -> PaymentInfo:
    return PaymentInfo(
        id="pay_1",
        razorpay_customer_id=customer_id,
        amount=Money(amount_paise),
        captured_at=_dt(captured_hour),
    )


@given(
    candidates=_candidates(),
    customer_id=_customer_ids,
    amount_paise=_paise,
    captured_hour=_offset_hours,
)
def test_a_winner_is_never_also_reported_as_ambiguous(
    candidates: list[CaseCandidate], customer_id: str, amount_paise: int, captured_hour: int
) -> None:
    result = match_payment(_payment(customer_id, amount_paise, captured_hour), candidates)

    if result.winner is not None:
        assert result.winner not in result.ambiguous_with
    assert len(set(result.ambiguous_with)) == len(result.ambiguous_with)


@given(
    candidates=_candidates(),
    customer_id=_customer_ids,
    amount_paise=_paise,
    captured_hour=_offset_hours,
)
def test_reported_candidates_are_a_subset_of_those_that_could_plausibly_match(
    candidates: list[CaseCandidate], customer_id: str, amount_paise: int, captured_hour: int
) -> None:
    """A coarser, independently-written filter (same customer, in window,
    a positive amount) than the matcher's own -- every candidate the
    matcher reports (won or lost to contention) must pass it too, since
    the matcher's own amount classification only narrows this further.
    """
    payment = _payment(customer_id, amount_paise, captured_hour)
    result = match_payment(payment, candidates)

    plausible_ids = {
        c.case_id
        for c in candidates
        if c.razorpay_customer_id == payment.razorpay_customer_id
        and c.window_anchor <= payment.captured_at <= c.window_anchor + ATTRIBUTION_WINDOW
        and payment.amount.paise > 0
    }
    reported = {c for c in (result.winner, *result.ambiguous_with) if c is not None}
    assert reported.issubset(plausible_ids)


@given(
    candidates=_candidates(),
    customer_id=_customer_ids,
    amount_paise=_paise,
    captured_hour=_offset_hours,
)
def test_the_winner_precedes_every_reported_loser_in_age_order(
    candidates: list[CaseCandidate], customer_id: str, amount_paise: int, captured_hour: int
) -> None:
    payment = _payment(customer_id, amount_paise, captured_hour)
    result = match_payment(payment, candidates)

    if result.winner is None or not result.ambiguous_with:
        return
    by_id = {c.case_id: c for c in candidates}
    winner_key = (by_id[result.winner].opened_at, str(result.winner))
    for loser_id in result.ambiguous_with:
        loser_key = (by_id[loser_id].opened_at, str(loser_id))
        assert winner_key <= loser_key


@given(
    candidates=_candidates(),
    customer_id=_customer_ids,
    amount_paise=_paise,
    captured_hour=_offset_hours,
)
def test_match_payment_is_deterministic(
    candidates: list[CaseCandidate], customer_id: str, amount_paise: int, captured_hour: int
) -> None:
    payment = _payment(customer_id, amount_paise, captured_hour)

    assert match_payment(payment, candidates) == match_payment(payment, candidates)
