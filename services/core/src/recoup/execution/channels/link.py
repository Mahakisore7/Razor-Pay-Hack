"""link -- a Razorpay payment link, via the gateway (T2.7). Gateway-routed
like `payment_retry`, not a messaging send: `create_payment_link` alone
generates the link, it does not deliver it, so there is no per-message
provider cost to account here beyond the planned `action.cost`.
"""

from __future__ import annotations

from datetime import timedelta

from recoup.domain.action import Action
from recoup.domain.case import Case
from recoup.execution.channels.base import ChannelResult
from recoup.gateway.interface import LinkRequest, PaymentGateway, PaymentLinkStatus
from recoup.platform.clock import Clock

__all__ = ["handle"]

_EXPIRY = timedelta(days=3)


async def handle(
    gateway: PaymentGateway, action: Action, case: Case, clock: Clock
) -> ChannelResult:
    link = await gateway.create_payment_link(
        LinkRequest(
            amount=case.at_risk,
            customer_contact_hash=case.customer.contact_hash,
            expire_by=clock.now() + _EXPIRY,
        )
    )
    return ChannelResult(success=link.status == PaymentLinkStatus.CREATED, reference=link.id)
