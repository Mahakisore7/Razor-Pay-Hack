"""R6 -- quiet hours (POLICY-ENGINE SS3). Evaluated in the *customer's*
own timezone, not the server's -- P3 (POLICY-ENGINE SS6.2) requires this
to hold for any timezone, not just IST. A DEFER, not a DENY (SS2.1): the
message itself is legitimate, only its timing is wrong, so it is
rescheduled to the next window open rather than dropped.

Exempt: `payment_retry` and `link` (see `NON_CONTACT_CHANNELS` --
gateway-routed, not a message a customer perceives), and `email` --
asynchronous by nature, read at the recipient's own convenience rather
than interrupting them the way a push notification or a ringing phone
does. That is the same "does not wake anyone up" test POLICY-ENGINE SS3
already applies to `payment_retry`; email passes it for the same reason.

Voice gets a stricter window (10:00-19:00) than the SMS/WhatsApp default
(09:00-21:00) -- a ringing phone is the most interruptive channel here.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from recoup.domain.action import Action, Channel
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["evaluate"]

_EXEMPT_CHANNELS = frozenset({Channel.PAYMENT_RETRY, Channel.LINK, Channel.EMAIL})

# (allowed_start, allowed_end) -- both same-day, so no midnight-wrap handling
# is needed: the *allowed* window is what is configured, the forbidden one
# (what POLICY-ENGINE SS3 states, e.g. "21:00-09:00") is simply its complement.
_DEFAULT_ALLOWED_WINDOW = (time(9, 0), time(21, 0))
_VOICE_ALLOWED_WINDOW = (time(10, 0), time(19, 0))


def _allowed_window(channel: Channel) -> tuple[time, time]:
    return _VOICE_ALLOWED_WINDOW if channel is Channel.VOICE else _DEFAULT_ALLOWED_WINDOW


def _next_window_open(local_now: datetime, allowed_start: time) -> datetime:
    """The next instant, in `local_now`'s own timezone, that the allowed
    window opens -- today's if it has not opened yet today, tomorrow's if
    today's has already passed. Only ever called when `local_now` is
    currently outside the window (see `evaluate`), so "already passed"
    and "not yet open" are the only two cases to distinguish.
    """
    candidate = datetime.combine(local_now.date(), allowed_start, tzinfo=local_now.tzinfo)
    if candidate <= local_now:
        candidate = datetime.combine(
            local_now.date() + timedelta(days=1), allowed_start, tzinfo=local_now.tzinfo
        )
    return candidate


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    if action.channel in _EXEMPT_CHANNELS:
        return None

    allowed_start, allowed_end = _allowed_window(action.channel)
    local_now = ctx.now.astimezone(ctx.customer_timezone)
    if allowed_start <= local_now.time() < allowed_end:
        return None

    defer_until = _next_window_open(local_now, allowed_start).astimezone(ctx.now.tzinfo)
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DEFER,
        rule_id="quiet_hours",
        inputs={
            "channel": action.channel.value,
            "customer_local_time": local_now.time().isoformat(),
            "customer_timezone": str(ctx.customer_timezone),
        },
        defer_until=defer_until,
        decided_at=ctx.now,
    )
