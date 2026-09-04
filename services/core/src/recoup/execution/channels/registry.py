"""The channel registry (T2.7, POLICY-ENGINE SS3 R3's "channel not
registered" check moves here, not into `policy` -- `execution` sits above
`policy` in the layering contract, so `policy` cannot depend on this
module, but nothing stops `execution` from being the thing that finally
enforces it).
"""

from __future__ import annotations

from recoup.domain.action import Channel
from recoup.domain.errors import RecoupError
from recoup.execution.channels import email, human_review, link, payment_retry, sms, voice, whatsapp
from recoup.execution.channels.base import ChannelHandler

__all__ = ["UnregisteredChannelError", "get_channel_handler"]

_REGISTRY: dict[Channel, ChannelHandler] = {
    Channel.SMS: sms.handle,
    Channel.WHATSAPP: whatsapp.handle,
    Channel.EMAIL: email.handle,
    Channel.VOICE: voice.handle,
    Channel.PAYMENT_RETRY: payment_retry.handle,
    Channel.LINK: link.handle,
    Channel.HUMAN_REVIEW: human_review.handle,
}


class UnregisteredChannelError(RecoupError):
    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        super().__init__(f"no channel handler registered for {channel!r}")


def get_channel_handler(channel: Channel) -> ChannelHandler:
    try:
        return _REGISTRY[channel]
    except KeyError:
        raise UnregisteredChannelError(channel) from None
