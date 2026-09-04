"""DeclineCategory's three properties drive everything downstream
(DOMAIN-MODEL SS2.3) -- a member missing one of them would KeyError the
first time policy touched it, so completeness is asserted directly.
"""

from recoup.domain.decline import DeclineCategory, RetryHorizon


def test_every_member_has_all_three_properties_defined() -> None:
    for category in DeclineCategory:
        assert isinstance(category.retryable, bool)
        assert isinstance(category.customer_action_required, bool)
        assert isinstance(category.retry_horizon, RetryHorizon)


def test_invalid_instrument_is_not_retryable_but_needs_customer_action() -> None:
    category = DeclineCategory.INVALID_INSTRUMENT
    assert category.retryable is False
    assert category.customer_action_required is True


def test_insufficient_funds_is_retryable_without_customer_action() -> None:
    category = DeclineCategory.INSUFFICIENT_FUNDS
    assert category.retryable is True
    assert category.customer_action_required is False
    assert category.retry_horizon == RetryHorizon.DAYS


def test_risk_blocked_never_retries() -> None:
    category = DeclineCategory.RISK_BLOCKED
    assert category.retryable is False
    assert category.retry_horizon == RetryHorizon.NEVER


def test_unknown_is_conservative_by_default() -> None:
    category = DeclineCategory.UNKNOWN
    assert category.retryable is False
