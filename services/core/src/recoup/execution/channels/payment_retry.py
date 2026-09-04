"""payment_retry -- via the gateway (T2.7). Requires
`action.payload.variables["payment_id"]`, the id of the originally-failed
payment this attempt re-presents; nothing upstream of the executor
derives that automatically yet, so its absence is a planning/enqueue bug,
not a channel failure.
"""

from __future__ import annotations

from recoup.domain.action import Action
from recoup.domain.case import Case
from recoup.execution.channels.base import ChannelPayloadError, ChannelResult
from recoup.gateway.interface import PaymentGateway, RetryRequest
from recoup.platform.clock import Clock

__all__ = ["handle"]


async def handle(
    gateway: PaymentGateway, action: Action, case: Case, clock: Clock
) -> ChannelResult:
    payment_id = action.payload.variables.get("payment_id")
    if not payment_id:
        raise ChannelPayloadError(action.id, "payment_retry", "payment_id")
    result = await gateway.retry_payment(
        RetryRequest(payment_id=payment_id, attempt=action.attempt, at=clock.now())
    )
    return ChannelResult(success=result.success, reference=result.payment.id)
