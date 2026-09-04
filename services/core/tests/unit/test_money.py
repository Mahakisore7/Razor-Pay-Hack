"""Money is the value object that must not be wrong (DOMAIN-MODEL SS2.1).

Every test here exists because a float in a money path is a correctness bug
waiting for a demo, and `allocate` is the one operation clever enough to lose
a paise if the remainder logic is off by one.
"""

from decimal import Decimal

import pytest

from recoup.domain.money import Currency, Money


def test_money_from_int_paise() -> None:
    money = Money(2499)
    assert money.paise == 2499
    assert money.currency == Currency.INR


def test_money_from_decimal_whole_paise() -> None:
    money = Money(Decimal("2499"))
    assert money.paise == 2499


def test_money_rejects_fractional_decimal_paise() -> None:
    with pytest.raises(ValueError, match="whole number of paise"):
        Money(Decimal("2499.5"))


def test_money_rejects_float() -> None:
    with pytest.raises(TypeError):
        Money(2499.99)  # type: ignore[arg-type]


def test_money_rejects_bool() -> None:
    # bool is an int subtype, so this is only a runtime check -- mypy sees it
    # as a valid `int` argument, which is exactly why the runtime guard exists.
    with pytest.raises(TypeError):
        Money(True)


def test_money_rejects_string() -> None:
    with pytest.raises(TypeError):
        Money("2499")  # type: ignore[arg-type]


def test_money_addition() -> None:
    assert Money(100) + Money(50) == Money(150)


def test_money_subtraction() -> None:
    assert Money(100) - Money(50) == Money(50)


def test_money_negation() -> None:
    assert -Money(100) == Money(-100)


def test_money_multiplication_by_int() -> None:
    assert Money(100) * 3 == Money(300)


def test_money_multiplication_rejects_float() -> None:
    with pytest.raises(TypeError):
        Money(100) * 1.5  # type: ignore[operator]


def test_money_addition_across_currencies_raises() -> None:
    with pytest.raises(ValueError, match="currenc"):
        Money(100, Currency.INR) + Money(100, Currency.USD)


def test_money_comparison_across_currencies_raises() -> None:
    with pytest.raises(ValueError, match="currenc"):
        _ = Money(100, Currency.INR) < Money(100, Currency.USD)


def test_money_addition_rejects_non_money_operand() -> None:
    with pytest.raises(TypeError, match="expected Money"):
        Money(100) + 50  # type: ignore[operator]


def test_money_ordering() -> None:
    assert Money(50) < Money(100)
    assert Money(100) <= Money(100)
    assert Money(150) > Money(100)
    assert Money(100) >= Money(100)


def test_money_allocate_three_equal_shares() -> None:
    assert Money(100).allocate([1, 1, 1]) == [Money(34), Money(33), Money(33)]


def test_money_allocate_sums_to_original() -> None:
    shares = Money(1_000_001).allocate([1, 1, 1])
    assert sum((s.paise for s in shares), 0) == 1_000_001


def test_money_allocate_weighted() -> None:
    shares = Money(100).allocate([1, 2, 3])
    assert shares == [Money(17), Money(33), Money(50)]
    assert sum((s.paise for s in shares), 0) == 100


def test_money_allocate_rejects_empty_ratios() -> None:
    with pytest.raises(ValueError):
        Money(100).allocate([])


def test_money_allocate_rejects_all_zero_ratios() -> None:
    with pytest.raises(ValueError):
        Money(100).allocate([0, 0])


def test_money_allocate_rejects_negative_ratio() -> None:
    with pytest.raises(ValueError):
        Money(100).allocate([1, -1])


def test_money_serialises_to_dict() -> None:
    assert Money(249999).to_dict() == {"paise": 249999, "currency": "INR"}
