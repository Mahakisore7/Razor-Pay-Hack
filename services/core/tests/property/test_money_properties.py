"""Property tests for Money (ENGINEERING-STANDARDS SS4.1).

The property share is unusually high on purpose: "no input sequence can
produce a violation" is a stronger claim than "these examples pass," and
`allocate`'s largest-remainder logic is exactly the kind of code where an
off-by-one only shows up on inputs nobody thought to write by hand.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from recoup.domain.money import Money


@given(
    paise=st.integers(min_value=0, max_value=10_000_000_00),
    ratios=st.lists(st.integers(min_value=1, max_value=1000), min_size=1, max_size=12),
)
def test_allocate_always_sums_to_original(paise: int, ratios: list[int]) -> None:
    shares = Money(paise).allocate(ratios)
    assert sum((s.paise for s in shares), 0) == paise
    assert len(shares) == len(ratios)
    assert all(s.paise >= 0 for s in shares)


@given(value=st.floats(allow_nan=True, allow_infinity=True))
def test_money_never_accepts_a_float(value: float) -> None:
    with pytest.raises(TypeError):
        Money(value)  # type: ignore[arg-type]
