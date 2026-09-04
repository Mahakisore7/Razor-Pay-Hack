"""`ChannelHandler` (T2.7): one per `Channel`, invoked by the executor
after the idempotency check passes and only when not in dry-run.

`payment_retry` and `link` call the real gateway (still the simulator
this phase, ADR-0004); every other channel is stubbed -- always succeeds,
calls nothing -- until a real messaging-provider integration lands.

Cost is never a handler's concern: the executor adds the already-planned
`action.cost` to `case.cost_spent` uniformly, regardless of which channel
ran, so a stubbed channel is still "cost-accounted" without needing to
know its own price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from recoup.domain.action import Action
from recoup.domain.case import Case
from recoup.domain.errors import RecoupError
from recoup.domain.identifiers import ActionId
from recoup.gateway.interface import PaymentGateway
from recoup.platform.clock import Clock

__all__ = ["ChannelHandler", "ChannelPayloadError", "ChannelResult"]


@dataclass(frozen=True, slots=True)
class ChannelResult:
    success: bool
    reference: str | None = None  # a provider-side id (payment, link), when there is one


class ChannelHandler(Protocol):
    async def __call__(
        self, gateway: PaymentGateway, action: Action, case: Case, clock: Clock
    ) -> ChannelResult: ...


class ChannelPayloadError(RecoupError):
    """A channel that needs a specific `action.payload.variables` entry
    (`payment_retry` needs `payment_id`) did not get one -- a planning or
    enqueue bug, surfaced here rather than let the gateway raise something
    less legible."""

    def __init__(self, action_id: ActionId, channel: str, missing_field: str) -> None:
        self.action_id = action_id
        self.channel = channel
        self.missing_field = missing_field
        super().__init__(
            f"action {action_id}: channel {channel!r} requires payload variable "
            f"{missing_field!r}, which was not set"
        )
