"""R7 -- contact frequency cap (POLICY-ENGINE SS3). Counted across *all*
of a customer's cases, not per case -- a customer with three failed
subscriptions must not receive three times the contact, the single most
common way recovery tooling turns into harassment. `ctx.contact_history`
is already scoped to the customer (PolicyContext's own field comment);
`ContactEvent` (domain/contact.py) deliberately carries no `case_id` for
the same reason -- this rule folds it into the three caps below and
never needs to know which case any prior contact belonged to.

`payment_retry` and `link` are exempt (`NON_CONTACT_CHANNELS`): neither
one reaches the customer, so neither counts as contact here either --
the same reasoning R4 (consent.py) already applies.
"""

from __future__ import annotations

from datetime import timedelta

from recoup.domain.action import NON_CONTACT_CHANNELS, Action, Channel
from recoup.domain.contact import ContactEvent
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["evaluate"]

_PER_CHANNEL_WINDOW = timedelta(hours=24)
_PER_CHANNEL_CAP = 1
_ALL_CHANNELS_WINDOW = timedelta(days=7)
_ALL_CHANNELS_CAP = 3
_VOICE_WINDOW = timedelta(days=7)
_VOICE_CAP = 1


def _within(
    events: tuple[ContactEvent, ...], ctx: PolicyContext, window: timedelta
) -> list[ContactEvent]:
    cutoff = ctx.now - window
    return [event for event in events if event.occurred_at > cutoff]


def _deny(
    action: Action,
    ctx: PolicyContext,
    *,
    scope: str,
    breaching: list[ContactEvent],
    window: timedelta,
) -> PolicyDecision:
    oldest = min(event.occurred_at for event in breaching)
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DEFER,
        rule_id="frequency_cap",
        inputs={"scope": scope, "channel": action.channel.value, "count": len(breaching)},
        defer_until=oldest + window,
        decided_at=ctx.now,
    )


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    if action.channel in NON_CONTACT_CHANNELS:
        return None

    per_channel = [
        e
        for e in _within(ctx.contact_history, ctx, _PER_CHANNEL_WINDOW)
        if e.channel == action.channel
    ]
    if len(per_channel) >= _PER_CHANNEL_CAP:
        return _deny(
            action, ctx, scope="per_channel_24h", breaching=per_channel, window=_PER_CHANNEL_WINDOW
        )

    all_channels = _within(ctx.contact_history, ctx, _ALL_CHANNELS_WINDOW)
    if len(all_channels) >= _ALL_CHANNELS_CAP:
        return _deny(
            action,
            ctx,
            scope="all_channels_7d",
            breaching=all_channels,
            window=_ALL_CHANNELS_WINDOW,
        )

    if action.channel is Channel.VOICE:
        voice = [
            e
            for e in _within(ctx.contact_history, ctx, _VOICE_WINDOW)
            if e.channel is Channel.VOICE
        ]
        if len(voice) >= _VOICE_CAP:
            return _deny(action, ctx, scope="voice_7d", breaching=voice, window=_VOICE_WINDOW)

    return None
