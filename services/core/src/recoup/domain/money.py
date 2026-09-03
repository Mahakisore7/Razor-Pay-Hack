"""Money -- the value object that must not be wrong (DOMAIN-MODEL SS2.1).

Integer paise throughout; ENGINEERING-STANDARDS SS1 rule 1. `0.1 + 0.2 ==
0.30000000000000004` is enough reason on its own to ban floats from this path
-- the deeper reason is that once a float enters, every downstream sum
inherits the error and the benchmark's headline number becomes indefensible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

__all__ = ["Currency", "Money"]


class Currency(StrEnum):
    """ISO 4217 codes this system can hold.

    INR is Razorpay's settlement currency and the only one Recoup operates
    on; USD exists solely so cross-currency arithmetic has a second currency
    to raise against in tests.
    """

    INR = "INR"
    USD = "USD"


@dataclass(frozen=True, slots=True, init=False)
class Money:
    """An exact amount of money as integer minor units (paise).

    No float constructor, ever: a float silently loses precision before it
    reaches this class, and every downstream sum would inherit the error.
    """

    paise: int
    currency: Currency

    def __init__(self, paise: int | Decimal, currency: Currency = Currency.INR) -> None:
        if isinstance(paise, bool):
            raise TypeError("Money.paise cannot be a bool")
        if isinstance(paise, Decimal):
            if paise != paise.to_integral_value():
                raise ValueError(f"Money requires a whole number of paise, got {paise}")
            resolved = int(paise)
        elif isinstance(paise, int):
            resolved = paise
        else:
            raise TypeError(
                f"Money does not accept {type(paise).__name__}; "
                "pass int paise or a whole-paise Decimal"
            )
        object.__setattr__(self, "paise", resolved)
        object.__setattr__(self, "currency", currency)

    def __add__(self, other: Money) -> Money:
        self._check_same_currency(other)
        return Money(self.paise + other.paise, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check_same_currency(other)
        return Money(self.paise - other.paise, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.paise, self.currency)

    def __mul__(self, factor: int) -> Money:
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise TypeError(f"Money can only be multiplied by an int, got {type(factor).__name__}")
        return Money(self.paise * factor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._check_same_currency(other)
        return self.paise < other.paise

    def __le__(self, other: Money) -> bool:
        self._check_same_currency(other)
        return self.paise <= other.paise

    def __gt__(self, other: Money) -> bool:
        self._check_same_currency(other)
        return self.paise > other.paise

    def __ge__(self, other: Money) -> bool:
        self._check_same_currency(other)
        return self.paise >= other.paise

    def _check_same_currency(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError(f"expected Money, got {type(other).__name__}")
        if self.currency != other.currency:
            raise ValueError(
                f"cannot operate across currencies: {self.currency} vs {other.currency}"
            )

    def allocate(self, ratios: Sequence[int]) -> list[Money]:
        """Split into ``len(ratios)`` parts that sum exactly back to ``self``.

        Largest-remainder method: each share gets
        ``floor(paise * ratio / total_ratio)``, then the leftover paise
        (always fewer than ``len(ratios)``) go one each to the shares with
        the largest fractional remainder, ties broken by index order -- so
        the split is deterministic and reproducible for the same inputs.
        """
        if not ratios:
            raise ValueError("allocate requires at least one ratio")
        if any(r < 0 for r in ratios):
            raise ValueError("ratios must be non-negative")
        total_ratio = sum(ratios)
        if total_ratio == 0:
            raise ValueError("ratios must sum to more than zero")

        shares = [self.paise * r for r in ratios]
        base = [s // total_ratio for s in shares]
        remainders = [s % total_ratio for s in shares]
        leftover = self.paise - sum(base)

        order = sorted(range(len(ratios)), key=lambda i: (-remainders[i], i))
        for i in order[:leftover]:
            base[i] += 1

        return [Money(p, self.currency) for p in base]

    def to_dict(self) -> dict[str, int | str]:
        """Serialise as paise, never as a decimal string -- no precision lost at a boundary."""
        return {"paise": self.paise, "currency": self.currency.value}
