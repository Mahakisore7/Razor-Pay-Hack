"""R11 -- rate limits (POLICY-ENGINE SS3): per-channel throughput caps, so
a large batch cannot exhaust an SMS quota or trip Razorpay's own rate
limiting. `ctx.rate_limit_tokens` is a snapshot -- gathered by whoever
builds a `PolicyContext` (a Redis token bucket, in a live deployment),
the same way `KillSwitchState` is read fresh before `evaluate` is ever
called (POLICY-ENGINE SS3, R1). This rule only reads the number it was
handed; it never touches Redis itself, and it does not decrement
anything -- consuming a token on ALLOW is that same caller's job, not
this pure function's, exactly as `evaluate`'s purity (POLICY-ENGINE SS1)
requires.

A channel absent from `rate_limit_tokens` is treated as unconstrained,
not exhausted: missing throughput data is an infrastructure gap, not a
reason to withhold an otherwise-compliant, otherwise-wanted contact.
"""

from __future__ import annotations

from datetime import timedelta

from recoup.domain.action import Action
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["evaluate"]

# No real bucket exists yet to report a precise refill instant (see this
# module's own docstring) -- a short, fixed retry is the honest amount of
# precision available from a bare token count alone.
_RETRY_AFTER = timedelta(minutes=1)


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    tokens = ctx.rate_limit_tokens.get(action.channel)
    if tokens is None or tokens > 0:
        return None
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DEFER,
        rule_id="rate_limited",
        inputs={"channel": action.channel.value, "tokens_remaining": tokens},
        defer_until=ctx.now + _RETRY_AFTER,
        decided_at=ctx.now,
    )
