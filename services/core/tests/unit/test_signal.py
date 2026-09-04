"""Signal invariants (DOMAIN-MODEL SS3): a zero-value signal or one that
traces to no raw event is a detector bug, not a valid value."""

import pytest

from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import SignalId, uuid7
from recoup.domain.money import Money
from recoup.domain.signal import LeakClass, Signal, SignalContext
from tests.factories import EPOCH, make_customer_ref


def _make_signal(
    *,
    at_risk: Money = Money(2_499_00),
    source_event_ids: tuple[str, ...] = ("evt_1",),
) -> Signal:
    return Signal(
        id=SignalId(uuid7()),
        leak_class=LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
        customer=make_customer_ref(),
        at_risk=at_risk,
        detected_at=EPOCH,
        source_event_ids=source_event_ids,
        decline=DeclineCategory.INSUFFICIENT_FUNDS,
        context=SignalContext(issuer="HDFC", method="upi"),
    )


def test_valid_signal_constructs() -> None:
    signal = _make_signal()
    assert signal.leak_class == LeakClass.L1_FAILED_ONE_TIME_PAYMENT


def test_signal_rejects_zero_at_risk() -> None:
    with pytest.raises(ValueError, match="positive"):
        _make_signal(at_risk=Money(0))


def test_signal_rejects_negative_at_risk() -> None:
    with pytest.raises(ValueError, match="positive"):
        _make_signal(at_risk=Money(-1))


def test_signal_rejects_empty_source_events() -> None:
    with pytest.raises(ValueError, match="source_event_ids"):
        _make_signal(source_event_ids=())


def test_signal_context_defaults_are_all_none() -> None:
    context = SignalContext()
    assert context.issuer is None
    assert context.bin is None
    assert context.psp is None
    assert context.instrument is None
    assert context.method is None
