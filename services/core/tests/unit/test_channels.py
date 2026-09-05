"""Unit tests for `recoup.execution.channels` (T2.7) -- `payment_retry`
and `link` against the real (offline, seeded) simulator, the stubbed
messaging channels against nothing at all, and the registry's dispatch
and its one failure mode.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from recoup.domain.action import Action, ActionCategory, ActionPayload, Channel
from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.decline import DeclineCategory
from recoup.domain.identifiers import ActionId, CaseId, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.execution.channels import email, human_review, link, payment_retry, sms, voice, whatsapp
from recoup.execution.channels.base import ChannelPayloadError, ChannelResult
from recoup.execution.channels.registry import _REGISTRY as REGISTRY
from recoup.execution.channels.registry import UnregisteredChannelError, get_channel_handler
from recoup.gateway.interface import Payment, PaymentGateway, PaymentQuery, PaymentStatus
from recoup.gateway.simulator.simulator import RazorpaySimulator
from recoup.platform.clock import FrozenClock
from tests.factories import make_customer_ref

_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
_SEED_AT = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)


def _find_failed_payment(sim: RazorpaySimulator, *, retryable: bool, tries: int = 500) -> Payment:
    for i in range(tries):
        payment = sim.seed_payment(
            customer_id=f"cust_{i}",
            amount=Money(250_000),
            method="card",
            issuer="HDFC",
            at=_SEED_AT,
        )
        if payment.status != PaymentStatus.FAILED:
            continue
        assert payment.error_reason is not None
        decline = DeclineCategory(payment.error_reason)
        if decline.retryable is retryable:
            return payment
    kind = "retryable" if retryable else "non-retryable"
    raise AssertionError(f"no {kind} failure found in {tries} tries")


def _action(
    *, channel: Channel, variables: dict[str, str] | None = None, attempt: int = 1
) -> Action:
    return Action(
        id=ActionId(uuid7()),
        case_id=CaseId(uuid7()),
        step_id="step-1",
        attempt=attempt,
        channel=channel,
        category=ActionCategory.TRANSACTIONAL,
        payload=ActionPayload(variables=variables or {}),
        cost=Money(0, Currency.INR),
        due_at=_CLOCK.now(),
    )


def _case() -> Case:
    return Case(
        id=CaseId(uuid7()),
        signal_id=SignalId(uuid7()),
        customer=make_customer_ref(),
        at_risk=Money(500_000, Currency.INR),
        state=CaseState.EXECUTING,
        arm=Arm.TREATMENT,
        opened_at=_CLOCK.now(),
        cost_spent=Money(0, Currency.INR),
        cost_ceiling=Money(100_000, Currency.INR),
    )


# --- payment_retry ----------------------------------------------------------------


async def test_payment_retry_calls_the_gateway_and_reports_the_new_payment() -> None:
    sim = RazorpaySimulator(seed=1)
    original = _find_failed_payment(sim, retryable=True)
    action = _action(channel=Channel.PAYMENT_RETRY, variables={"payment_id": original.id})

    result = await payment_retry.handle(sim, action, _case(), _CLOCK)

    assert result.reference is not None
    assert result.reference != original.id  # a new attempt, not the original


async def test_payment_retry_raises_without_a_payment_id() -> None:
    sim = RazorpaySimulator(seed=1)
    action = _action(channel=Channel.PAYMENT_RETRY, variables={})

    with pytest.raises(ChannelPayloadError) as exc_info:
        await payment_retry.handle(sim, action, _case(), _CLOCK)

    assert exc_info.value.missing_field == "payment_id"


# --- link ---------------------------------------------------------------------------


async def test_link_creates_a_payment_link() -> None:
    sim = RazorpaySimulator(seed=2)
    action = _action(channel=Channel.LINK)

    result = await link.handle(sim, action, _case(), _CLOCK)

    assert result.success is True
    assert result.reference is not None


# --- stubbed messaging channels -----------------------------------------------------


@pytest.mark.parametrize(
    ("handle", "channel"),
    [
        (sms.handle, Channel.SMS),
        (whatsapp.handle, Channel.WHATSAPP),
        (email.handle, Channel.EMAIL),
        (voice.handle, Channel.VOICE),
        (human_review.handle, Channel.HUMAN_REVIEW),
    ],
)
async def test_stubbed_channels_always_succeed_and_call_nothing(
    handle: Callable[[PaymentGateway, Action, Case, FrozenClock], Awaitable[ChannelResult]],
    channel: Channel,
) -> None:
    sim = RazorpaySimulator(seed=3)
    before = await sim.list_payments(PaymentQuery(count=1000))
    action = _action(channel=channel)

    result = await handle(sim, action, _case(), _CLOCK)

    after = await sim.list_payments(PaymentQuery(count=1000))
    assert result.success is True
    assert result.reference is None
    assert len(after.items) == len(before.items)  # nothing was actually sent


# --- registry -------------------------------------------------------------------------


@pytest.mark.parametrize("channel", list(Channel))
def test_get_channel_handler_returns_a_handler_for_every_channel(channel: Channel) -> None:
    assert get_channel_handler(channel) is not None


def test_get_channel_handler_raises_for_an_unregistered_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(REGISTRY, Channel.SMS)
    with pytest.raises(UnregisteredChannelError) as exc_info:
        get_channel_handler(Channel.SMS)
    assert exc_info.value.channel == Channel.SMS
