"""PaymentGateway -- the one interface execution touches Razorpay through
(RAZORPAY-INTEGRATION SS2, ADR-0004).

Two implementations satisfy it: the seeded `RazorpaySimulator` (default,
this phase) and a live test-mode client (Phase 7). `Protocol`, not an ABC,
so the simulator's type hierarchy stays independent of the live client's
(ENGINEERING-STANDARDS SS2.2) -- structural typing is what lets two
unrelated classes both "be" a PaymentGateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from recoup.domain.money import Money

__all__ = [
    "AuthLink",
    "AuthLinkRequest",
    "DebitResult",
    "Invoice",
    "InvoiceStatus",
    "LinkRequest",
    "MandateDebitRequest",
    "Order",
    "OrderStatus",
    "Page",
    "Payment",
    "PaymentGateway",
    "PaymentLink",
    "PaymentLinkStatus",
    "PaymentQuery",
    "PaymentResult",
    "PaymentStatus",
    "RetryRequest",
    "Subscription",
    "SubscriptionStatus",
]


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


@dataclass(frozen=True, slots=True)
class Payment:
    id: str
    order_id: str
    customer_id: str
    amount: Money
    status: PaymentStatus
    method: str  # "upi" | "card" | "netbanking" | "wallet"
    issuer: str
    error_reason: str | None  # raw reason string; categorise via gateway.decline_taxonomy
    created_at: datetime


class OrderStatus(StrEnum):
    CREATED = "created"
    ATTEMPTED = "attempted"
    PAID = "paid"


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    amount: Money
    status: OrderStatus
    created_at: datetime


class SubscriptionStatus(StrEnum):
    CREATED = "created"
    AUTHENTICATED = "authenticated"
    ACTIVE = "active"
    HALTED = "halted"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class Subscription:
    id: str
    status: SubscriptionStatus
    current_start: datetime
    current_end: datetime


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Invoice:
    id: str
    amount_due: Money
    status: InvoiceStatus
    due_date: datetime


@dataclass(frozen=True, slots=True)
class PaymentQuery:
    from_time: datetime | None = None
    to_time: datetime | None = None
    count: int = 10
    skip: int = 0


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class RetryRequest:
    payment_id: str
    attempt: int
    at: datetime  # when the retry executes -- caller-supplied, never wall-clock (SS6.2 determinism)


@dataclass(frozen=True, slots=True)
class PaymentResult:
    success: bool
    payment: Payment


@dataclass(frozen=True, slots=True)
class MandateDebitRequest:
    mandate_id: str
    amount: Money
    due_at: datetime


@dataclass(frozen=True, slots=True)
class DebitResult:
    success: bool
    payment: Payment


@dataclass(frozen=True, slots=True)
class LinkRequest:
    amount: Money
    customer_contact_hash: str
    expire_by: datetime
    description: str | None = None


class PaymentLinkStatus(StrEnum):
    CREATED = "created"
    PAID = "paid"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class PaymentLink:
    id: str
    short_url: str
    status: PaymentLinkStatus


@dataclass(frozen=True, slots=True)
class AuthLinkRequest:
    customer_contact_hash: str
    amount: Money
    expire_by: datetime


@dataclass(frozen=True, slots=True)
class AuthLink:
    id: str
    short_url: str
    status: PaymentLinkStatus  # same lifecycle shape as a payment link


class PaymentGateway(Protocol):
    # Reads
    async def fetch_payment(self, payment_id: str) -> Payment: ...
    async def fetch_order(self, order_id: str) -> Order: ...
    async def fetch_subscription(self, sub_id: str) -> Subscription: ...
    async def fetch_invoice(self, invoice_id: str) -> Invoice: ...
    async def list_payments(self, q: PaymentQuery) -> Page[Payment]: ...

    # Recovery actions
    async def retry_payment(self, req: RetryRequest) -> PaymentResult: ...
    async def present_mandate(self, req: MandateDebitRequest) -> DebitResult: ...
    async def create_payment_link(self, req: LinkRequest) -> PaymentLink: ...
    async def cancel_payment_link(self, link_id: str) -> None: ...

    # Subscription lifecycle
    async def resume_subscription(self, sub_id: str) -> Subscription: ...
    async def create_auth_link(self, req: AuthLinkRequest) -> AuthLink: ...
