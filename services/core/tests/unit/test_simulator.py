"""RazorpaySimulator satisfies PaymentGateway with no network and no mock --
it is a real (if synthetic) world, seeded and offline (ADR-0004)."""

from datetime import UTC, datetime, timedelta

import pytest

from recoup.domain.decline import DeclineCategory
from recoup.domain.money import Money
from recoup.gateway.interface import (
    AuthLinkRequest,
    Invoice,
    InvoiceStatus,
    LinkRequest,
    MandateDebitRequest,
    Order,
    OrderStatus,
    Payment,
    PaymentQuery,
    PaymentStatus,
    RetryRequest,
    Subscription,
    SubscriptionStatus,
)
from recoup.gateway.simulator.simulator import RazorpaySimulator

_AT = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)  # not pre-payday, mid-morning


def _find_seed_payment(sim: RazorpaySimulator, *, retryable: bool, tries: int = 500) -> Payment:
    for i in range(tries):
        payment = sim.seed_payment(
            customer_id=f"cust_{i}", amount=Money(250_000), method="card", issuer="HDFC", at=_AT
        )
        if payment.status != PaymentStatus.FAILED:
            continue
        assert payment.error_reason is not None
        decline = DeclineCategory(payment.error_reason)
        if decline.retryable is retryable:
            return payment
    kind = "retryable" if retryable else "non-retryable"
    raise AssertionError(f"no {kind} failure found in {tries} tries")


def test_seed_payment_records_ground_truth() -> None:
    sim = RazorpaySimulator(seed=1)
    payment = sim.seed_payment(
        customer_id="cust_1", amount=Money(100_00), method="upi", issuer="HDFC", at=_AT
    )
    records = sim.ground_truth.all()
    assert len(records) == 1
    assert records[0].payment_id == payment.id


async def test_fetch_payment_returns_a_seeded_payment() -> None:
    sim = RazorpaySimulator(seed=1)
    payment = sim.seed_payment(
        customer_id="cust_1", amount=Money(100_00), method="upi", issuer="HDFC", at=_AT
    )
    fetched = await sim.fetch_payment(payment.id)
    assert fetched == payment


async def test_fetch_order_returns_a_test_seeded_order() -> None:
    sim = RazorpaySimulator(seed=1)
    order = Order(id="order_1", amount=Money(100_00), status=OrderStatus.PAID, created_at=_AT)
    sim._orders["order_1"] = order  # test-only seeding; no create method in Protocol
    assert await sim.fetch_order("order_1") == order


async def test_fetch_subscription_returns_a_test_seeded_subscription() -> None:
    sim = RazorpaySimulator(seed=1)
    sub = Subscription(
        id="sub_1", status=SubscriptionStatus.ACTIVE, current_start=_AT, current_end=_AT
    )
    sim._subscriptions["sub_1"] = sub  # test-only seeding
    assert await sim.fetch_subscription("sub_1") == sub


async def test_fetch_invoice_returns_a_test_seeded_invoice() -> None:
    sim = RazorpaySimulator(seed=1)
    invoice = Invoice(
        id="inv_1", amount_due=Money(100_00), status=InvoiceStatus.ISSUED, due_date=_AT
    )
    sim._invoices["inv_1"] = invoice  # test-only seeding; no create method in Protocol
    assert await sim.fetch_invoice("inv_1") == invoice


async def test_list_payments_filters_by_time_window_and_paginates() -> None:
    sim = RazorpaySimulator(seed=2)
    for i in range(5):
        sim.seed_payment(
            customer_id=f"cust_{i}",
            amount=Money(100_00),
            method="upi",
            issuer="HDFC",
            at=_AT + timedelta(hours=i),
        )
    page = await sim.list_payments(PaymentQuery(count=2))
    assert len(page.items) == 2
    assert page.has_more is True

    windowed = await sim.list_payments(PaymentQuery(from_time=_AT + timedelta(hours=3), count=10))
    assert len(windowed.items) == 2  # hours 3 and 4 only


async def test_retry_payment_on_a_retryable_failure_records_a_new_attempt() -> None:
    sim = RazorpaySimulator(seed=3)
    original = _find_seed_payment(sim, retryable=True)
    result = await sim.retry_payment(
        RetryRequest(payment_id=original.id, attempt=2, at=_AT + timedelta(hours=1))
    )
    assert result.payment.id != original.id
    assert result.payment.customer_id == original.customer_id
    assert len(sim.ground_truth.all()) == 2  # the seed attempt and the retry


async def test_retry_payment_rejects_a_non_retryable_decline() -> None:
    sim = RazorpaySimulator(seed=4)
    original = _find_seed_payment(sim, retryable=False)
    with pytest.raises(ValueError, match="not retryable"):
        await sim.retry_payment(
            RetryRequest(payment_id=original.id, attempt=2, at=_AT + timedelta(hours=1))
        )


async def test_retry_payment_rejects_a_payment_that_is_not_failed() -> None:
    sim = RazorpaySimulator(seed=5)
    payment = None
    for i in range(200):
        candidate = sim.seed_payment(
            customer_id=f"c{i}", amount=Money(100_00), method="upi", issuer="HDFC", at=_AT
        )
        if candidate.status == PaymentStatus.CAPTURED:
            payment = candidate
            break
    assert payment is not None, "no captured seed payment found in 200 tries"
    with pytest.raises(ValueError, match="cannot retry"):
        await sim.retry_payment(RetryRequest(payment_id=payment.id, attempt=2, at=_AT))


async def test_present_mandate_debits_and_tracks_representation_budget() -> None:
    sim = RazorpaySimulator(seed=6)
    for _ in range(3):
        await sim.present_mandate(
            MandateDebitRequest(mandate_id="mandate_1", amount=Money(500_00), due_at=_AT)
        )
    with pytest.raises(ValueError, match="representation cap"):
        await sim.present_mandate(
            MandateDebitRequest(mandate_id="mandate_1", amount=Money(500_00), due_at=_AT)
        )


async def test_present_mandate_budgets_are_independent_per_mandate() -> None:
    sim = RazorpaySimulator(seed=6)
    for _ in range(3):
        await sim.present_mandate(
            MandateDebitRequest(mandate_id="mandate_a", amount=Money(500_00), due_at=_AT)
        )
    # A different mandate's budget is untouched by mandate_a's exhaustion.
    result = await sim.present_mandate(
        MandateDebitRequest(mandate_id="mandate_b", amount=Money(500_00), due_at=_AT)
    )
    assert result.payment.amount == Money(500_00)


async def test_create_and_cancel_payment_link() -> None:
    sim = RazorpaySimulator(seed=7)
    link = await sim.create_payment_link(
        LinkRequest(amount=Money(100_00), customer_contact_hash="hash1", expire_by=_AT)
    )
    await sim.cancel_payment_link(link.id)
    # No PaymentGateway read method for links exists in the interface;
    # absence of an exception on cancel is the observable behaviour here.


async def test_resume_subscription() -> None:
    sim = RazorpaySimulator(seed=8)
    sim._subscriptions["sub_1"] = Subscription(  # test-only seeding; no create method in Protocol
        id="sub_1", status=SubscriptionStatus.HALTED, current_start=_AT, current_end=_AT
    )
    resumed = await sim.resume_subscription("sub_1")
    assert resumed.status == SubscriptionStatus.ACTIVE


async def test_create_auth_link() -> None:
    sim = RazorpaySimulator(seed=9)
    link = await sim.create_auth_link(
        AuthLinkRequest(customer_contact_hash="hash1", amount=Money(100_00), expire_by=_AT)
    )
    assert link.short_url.startswith("https://rzp.io/a/")
