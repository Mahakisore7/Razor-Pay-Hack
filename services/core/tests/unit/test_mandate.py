"""Mandate re-presentation budget accounting and debit authorisation
(DOMAIN-MODEL SS11): debiting above max_amount or outside validity is
refused in the domain layer, before it can reach the gateway."""

from datetime import date

import pytest

from recoup.domain.mandate import (
    Frequency,
    Mandate,
    MandateAmountExceededError,
    MandateBudgetExhaustedError,
    MandateNotValidError,
    MandateRail,
    MandateStatus,
)
from recoup.domain.money import Money
from tests.factories import make_customer_ref


def _make_mandate(
    *,
    max_amount: Money = Money(500_00),
    valid_from: date = date(2026, 1, 1),
    valid_until: date = date(2026, 12, 31),
    representations_used_this_cycle: int = 0,
    representation_cap: int = 3,
) -> Mandate:
    return Mandate(
        id="mandate_test",
        customer=make_customer_ref(),
        rail=MandateRail.UPI_AUTOPAY,
        max_amount=max_amount,
        frequency=Frequency.MONTHLY,
        valid_from=valid_from,
        valid_until=valid_until,
        status=MandateStatus.ACTIVE,
        representations_used_this_cycle=representations_used_this_cycle,
        representation_cap=representation_cap,
    )


def test_authorize_debit_within_amount_and_validity_does_not_raise() -> None:
    mandate = _make_mandate(max_amount=Money(500_00))
    mandate.authorize_debit(Money(499_00), date(2026, 6, 1))


def test_authorize_debit_rejects_amount_above_max() -> None:
    mandate = _make_mandate(max_amount=Money(500_00))
    with pytest.raises(MandateAmountExceededError):
        mandate.authorize_debit(Money(500_01), date(2026, 6, 1))


def test_authorize_debit_rejects_before_valid_from() -> None:
    mandate = _make_mandate(valid_from=date(2026, 6, 1), valid_until=date(2026, 12, 31))
    with pytest.raises(MandateNotValidError):
        mandate.authorize_debit(Money(100), date(2026, 1, 1))


def test_authorize_debit_rejects_after_valid_until() -> None:
    mandate = _make_mandate(valid_from=date(2026, 1, 1), valid_until=date(2026, 6, 30))
    with pytest.raises(MandateNotValidError):
        mandate.authorize_debit(Money(100), date(2026, 12, 1))


def test_authorize_debit_at_the_validity_boundaries_does_not_raise() -> None:
    mandate = _make_mandate(valid_from=date(2026, 1, 1), valid_until=date(2026, 12, 31))
    mandate.authorize_debit(Money(100), date(2026, 1, 1))
    mandate.authorize_debit(Money(100), date(2026, 12, 31))


def test_with_representation_used_increments_and_returns_a_new_mandate() -> None:
    original = _make_mandate(representations_used_this_cycle=0, representation_cap=3)
    updated = original.with_representation_used()
    assert updated.representations_used_this_cycle == 1
    assert original.representations_used_this_cycle == 0  # frozen: original is untouched
    assert updated.remaining_representations == 2


def test_with_representation_used_raises_once_cap_is_reached() -> None:
    exhausted = _make_mandate(representations_used_this_cycle=3, representation_cap=3)
    with pytest.raises(MandateBudgetExhaustedError):
        exhausted.with_representation_used()


def test_remaining_representations_reflects_usage() -> None:
    mandate = _make_mandate(representations_used_this_cycle=1, representation_cap=3)
    assert mandate.remaining_representations == 2
