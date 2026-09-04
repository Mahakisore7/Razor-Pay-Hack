"""consent_at folds a ledger rather than reading a boolean column
(DOMAIN-MODEL SS12): compliance asks whether the customer was opted in
*when contacted*, not whether they are now, and absence of a record must
mean refusal -- never permission."""

from datetime import UTC, datetime, timedelta

from recoup.domain.action import Channel
from recoup.domain.consent import ConsentEvent, ConsentSource, consent_at
from tests.factories import make_customer_ref

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = _T0 + timedelta(days=1)
_T2 = _T0 + timedelta(days=2)


def _event(*, granted: bool, occurred_at: datetime, channel: Channel = Channel.SMS) -> ConsentEvent:
    return ConsentEvent(
        customer=make_customer_ref(),
        channel=channel,
        granted=granted,
        source=ConsentSource.CHECKOUT,
        occurred_at=occurred_at,
    )


def test_no_events_at_all_is_refusal() -> None:
    assert consent_at([], Channel.SMS, _T1) is False


def test_no_events_for_the_queried_channel_is_refusal() -> None:
    events = [_event(granted=True, occurred_at=_T0, channel=Channel.WHATSAPP)]
    assert consent_at(events, Channel.SMS, _T1) is False


def test_no_events_at_or_before_when_is_refusal() -> None:
    events = [_event(granted=True, occurred_at=_T2)]
    assert consent_at(events, Channel.SMS, _T0) is False


def test_a_single_grant_before_when_is_honoured() -> None:
    events = [_event(granted=True, occurred_at=_T0)]
    assert consent_at(events, Channel.SMS, _T1) is True


def test_the_most_recent_event_at_or_before_when_wins() -> None:
    events = [
        _event(granted=True, occurred_at=_T0),
        _event(granted=False, occurred_at=_T1),  # SMS_STOP after checkout opt-in
    ]
    assert consent_at(events, Channel.SMS, _T2) is False


def test_a_later_revocation_does_not_retroactively_change_a_past_answer() -> None:
    events = [
        _event(granted=True, occurred_at=_T0),
        _event(granted=False, occurred_at=_T2),
    ]
    # Consent as of _T1 must reflect what was true at _T1, not the eventual
    # revocation at _T2 -- this is the entire point of the `when` parameter.
    assert consent_at(events, Channel.SMS, _T1) is True


def test_events_out_of_input_order_still_resolve_to_the_latest_by_occurred_at() -> None:
    # Deliberately not appended in chronological order, e.g. as an unordered
    # DB fetch might return them.
    events = [
        _event(granted=False, occurred_at=_T1),
        _event(granted=True, occurred_at=_T0),
    ]
    assert consent_at(events, Channel.SMS, _T2) is False


def test_channel_specific_consent_is_independent() -> None:
    events = [
        _event(granted=True, occurred_at=_T0, channel=Channel.SMS),
        _event(granted=False, occurred_at=_T0, channel=Channel.WHATSAPP),
    ]
    assert consent_at(events, Channel.SMS, _T1) is True
    assert consent_at(events, Channel.WHATSAPP, _T1) is False
