"""RazorpaySimulator -- seeded, deterministic, no network (RAZORPAY-
INTEGRATION SS6, ADR-0004). Satisfies `PaymentGateway` structurally; a
conformance suite against the live client is Phase 7 work, once that
client exists to compare against.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from recoup.domain.decline import DeclineCategory
from recoup.domain.money import Money
from recoup.gateway.interface import (
    AuthLink,
    AuthLinkRequest,
    DebitResult,
    Invoice,
    LinkRequest,
    MandateDebitRequest,
    Order,
    Page,
    Payment,
    PaymentLink,
    PaymentLinkStatus,
    PaymentQuery,
    PaymentResult,
    PaymentStatus,
    RetryRequest,
    Subscription,
    SubscriptionStatus,
)
from recoup.gateway.simulator.config import SimulatorConfig, load_default_simulator_config
from recoup.gateway.simulator.ground_truth import GroundTruthLog, GroundTruthRecord
from recoup.gateway.simulator.world import World

__all__ = ["RazorpaySimulator"]


class RazorpaySimulator:
    def __init__(self, seed: int, config: SimulatorConfig | None = None) -> None:
        self._seed = seed
        self._config = config if config is not None else load_default_simulator_config()
        self._world = World(config=self._config, seed=seed)
        self._ground_truth = GroundTruthLog()
        self._payments: dict[str, Payment] = {}
        self._orders: dict[str, Order] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._invoices: dict[str, Invoice] = {}
        self._payment_links: dict[str, PaymentLink] = {}
        self._auth_links: dict[str, AuthLink] = {}
        self._representations_used: dict[str, int] = {}
        self._next_id = 0

    @property
    def ground_truth(self) -> GroundTruthLog:
        return self._ground_truth

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}_{self._seed}_{self._next_id}"

    def _execute_attempt(
        self,
        *,
        order_id: str,
        customer_id: str,
        amount: Money,
        method: str,
        issuer: str,
        at: datetime,
        attempt_no: int,
    ) -> Payment:
        """The one place an attempt is rolled, recorded as a Payment, and
        logged to ground truth -- used by `seed_payment`, `retry_payment`,
        and `present_mandate` alike so the three don't drift out of sync."""
        outcome = self._world.attempt_outcome(
            customer_id=customer_id, instrument=method, issuer=issuer, at=at, attempt_no=attempt_no
        )
        payment = Payment(
            id=self._new_id("pay"),
            order_id=order_id,
            customer_id=customer_id,
            amount=amount,
            status=PaymentStatus.CAPTURED if outcome.success else PaymentStatus.FAILED,
            method=method,
            issuer=issuer,
            error_reason=(
                None if outcome.decline_category is None else outcome.decline_category.value
            ),
            created_at=at,
        )
        self._payments[payment.id] = payment
        self._ground_truth.record(
            GroundTruthRecord(
                payment_id=payment.id,
                customer_id=customer_id,
                true_cause=outcome.true_cause,
                decline_category=outcome.decline_category,
                would_have_recovered_unaided=self._world.would_recover_unaided(customer_id),
                occurred_at=at,
            )
        )
        return payment

    def seed_payment(
        self,
        *,
        customer_id: str,
        amount: Money,
        method: str,
        issuer: str,
        at: datetime,
        order_id: str | None = None,
    ) -> Payment:
        """Test/fixture harness only -- not part of `PaymentGateway`. Primes
        an initial payment as if it had come through Razorpay's checkout
        flow, which Recoup never controls; only retries and recoveries are
        ours (RAZORPAY-INTEGRATION SS1)."""
        return self._execute_attempt(
            order_id=order_id if order_id is not None else self._new_id("order"),
            customer_id=customer_id,
            amount=amount,
            method=method,
            issuer=issuer,
            at=at,
            attempt_no=1,
        )

    def seed_failed_payment(
        self,
        *,
        customer_id: str,
        amount: Money,
        method: str,
        issuer: str,
        at: datetime,
        decline_category: DeclineCategory,
        order_id: str | None = None,
    ) -> Payment:
        """T3.5's benchmark harness only -- not part of `PaymentGateway`,
        and deliberately not `seed_payment` (whose outcome `_execute_attempt`
        always rolls via `World.attempt_outcome`). A benchmark cohort (T3.1)
        pre-assigns each case's decline category from its own realistic
        distribution -- an *at-risk* population by construction. Rolling the
        initial attempt through `World` instead could just as easily land on
        `success`, at each instrument's ~85-92% base rate, which would
        silently evaporate most of a requested cohort before it ever became
        a case. This forces the *initial* failure to match the cohort's own
        assignment; every *retry* against it still rolls normally through
        `World.attempt_outcome` inside `retry_payment`, so whether recovery
        actually happens stays exactly as stochastic and realistic as ever.

        Still recorded to `ground_truth`, `true_cause="cohort_seed"` --
        distinguishable from a naturally-rolled failure, but present in the
        same log `bench.evaluation` reads, with the same
        `would_have_recovered_unaided` counterfactual computed the normal
        way.
        """
        payment = Payment(
            id=self._new_id("pay"),
            order_id=order_id if order_id is not None else self._new_id("order"),
            customer_id=customer_id,
            amount=amount,
            status=PaymentStatus.FAILED,
            method=method,
            issuer=issuer,
            error_reason=decline_category.value,
            created_at=at,
        )
        self._payments[payment.id] = payment
        self._ground_truth.record(
            GroundTruthRecord(
                payment_id=payment.id,
                customer_id=customer_id,
                true_cause="cohort_seed",
                decline_category=decline_category,
                would_have_recovered_unaided=self._world.would_recover_unaided(customer_id),
                occurred_at=at,
            )
        )
        return payment

    # --- PaymentGateway: reads ------------------------------------------

    async def fetch_payment(self, payment_id: str) -> Payment:
        return self._payments[payment_id]

    async def fetch_order(self, order_id: str) -> Order:
        return self._orders[order_id]

    async def fetch_subscription(self, sub_id: str) -> Subscription:
        return self._subscriptions[sub_id]

    async def fetch_invoice(self, invoice_id: str) -> Invoice:
        return self._invoices[invoice_id]

    async def list_payments(self, q: PaymentQuery) -> Page[Payment]:
        items = [
            p
            for p in self._payments.values()
            if (q.from_time is None or p.created_at >= q.from_time)
            and (q.to_time is None or p.created_at <= q.to_time)
        ]
        items.sort(key=lambda p: p.created_at)
        page = items[q.skip : q.skip + q.count]
        has_more = q.skip + q.count < len(items)
        return Page(items=tuple(page), has_more=has_more)

    # --- PaymentGateway: recovery actions --------------------------------

    async def retry_payment(self, req: RetryRequest) -> PaymentResult:
        original = self._payments[req.payment_id]
        if original.status != PaymentStatus.FAILED:
            raise ValueError(f"cannot retry payment {req.payment_id} in status {original.status}")
        # _execute_attempt is the only place a Payment is ever created, and
        # it always sets error_reason exactly when status is FAILED -- so
        # this is a real invariant, not a defensive maybe.
        assert original.error_reason is not None, "a FAILED payment always carries an error_reason"
        decline = DeclineCategory(original.error_reason)
        if not decline.retryable:
            raise ValueError(f"payment {req.payment_id} decline {decline} is not retryable")
        new_payment = self._execute_attempt(
            order_id=original.order_id,
            customer_id=original.customer_id,
            amount=original.amount,
            method=original.method,
            issuer=original.issuer,
            at=req.at,
            attempt_no=req.attempt,
        )
        success = new_payment.status == PaymentStatus.CAPTURED
        return PaymentResult(success=success, payment=new_payment)

    async def present_mandate(self, req: MandateDebitRequest) -> DebitResult:
        used = self._representations_used.get(req.mandate_id, 0)
        cap = self._config.mandate_budgets.default_representation_cap
        if used >= cap:
            raise ValueError(f"mandate {req.mandate_id} representation cap {cap} already reached")
        self._representations_used[req.mandate_id] = used + 1

        # present_mandate carries no prior Payment to read a customer/issuer
        # from (unlike retry_payment); derive both deterministically from
        # the mandate id so the attempt is still reproducible and
        # attributable to a stable "customer" across representations.
        payment = self._execute_attempt(
            order_id=self._new_id("order"),
            customer_id=f"mandate_customer:{req.mandate_id}",
            amount=req.amount,
            method="upi",
            issuer=f"mandate_issuer:{req.mandate_id}",
            at=req.due_at,
            attempt_no=used + 1,
        )
        return DebitResult(success=payment.status == PaymentStatus.CAPTURED, payment=payment)

    async def create_payment_link(self, req: LinkRequest) -> PaymentLink:
        link = PaymentLink(
            id=self._new_id("plink"),
            short_url=f"https://rzp.io/l/{self._new_id('slug')}",
            status=PaymentLinkStatus.CREATED,
        )
        self._payment_links[link.id] = link
        return link

    async def cancel_payment_link(self, link_id: str) -> None:
        link = self._payment_links[link_id]
        self._payment_links[link_id] = replace(link, status=PaymentLinkStatus.CANCELLED)

    # --- PaymentGateway: subscription lifecycle --------------------------

    async def resume_subscription(self, sub_id: str) -> Subscription:
        sub = self._subscriptions[sub_id]
        resumed = replace(sub, status=SubscriptionStatus.ACTIVE)
        self._subscriptions[sub_id] = resumed
        return resumed

    async def create_auth_link(self, req: AuthLinkRequest) -> AuthLink:
        link = AuthLink(
            id=self._new_id("alink"),
            short_url=f"https://rzp.io/a/{self._new_id('slug')}",
            status=PaymentLinkStatus.CREATED,
        )
        self._auth_links[link.id] = link
        return link
