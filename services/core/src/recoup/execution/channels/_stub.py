"""Shared body for channels with no real integration yet (T2.7): always
succeeds, calls nothing. Cost is accounted generically by the executor
from the planned `action.cost`, not by any per-channel logic here -- a
stubbed channel is still cost-accounted without needing its own price.

`sms.py`, `whatsapp.py`, `email.py`, `voice.py`, and `human_review.py`
each just alias this, one file per channel matching ARCHITECTURE's module
layout, so a future messaging-provider integration replaces exactly one
file's `handle` and nothing about the registry or the executor changes
when it does.
"""

from __future__ import annotations

from recoup.domain.action import Action
from recoup.domain.case import Case
from recoup.execution.channels.base import ChannelResult
from recoup.gateway.interface import PaymentGateway
from recoup.platform.clock import Clock

__all__ = ["stub_handle"]


async def stub_handle(
    gateway: PaymentGateway, action: Action, case: Case, clock: Clock
) -> ChannelResult:
    return ChannelResult(success=True, reference=None)
