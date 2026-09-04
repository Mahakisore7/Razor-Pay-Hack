"""R4 -- consent (POLICY-ENGINE SS3). Evaluated at `action.due_at`, the
time the action would actually be sent, not `ctx.now` -- consent is asked
retrospectively ("were they opted in when you contacted them"), and
`consent_at` (domain/consent.py) already encodes exactly that semantics.
Absence of a record is refusal, never permission.

`payment_retry` and `link` are exempt: both are gateway-routed actions
(T2.7 -- "payment_retry and payment_link via the gateway"), not a message
to the customer on a channel they can opt in or out of. Consent, as
POLICY-ENGINE SS3/SS12 frame it, governs contact -- an automated
re-presentation of a mandate debit is not contact.
"""

from __future__ import annotations

from recoup.domain.action import Action, Channel
from recoup.domain.consent import consent_at
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["evaluate"]

_EXEMPT_CHANNELS = frozenset({Channel.PAYMENT_RETRY, Channel.LINK})


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    if action.channel in _EXEMPT_CHANNELS:
        return None
    if consent_at(ctx.consent_events, action.channel, action.due_at):
        return None
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DENY,
        rule_id="no_consent",
        inputs={"channel": action.channel.value, "at": action.due_at.isoformat()},
        defer_until=None,
        decided_at=ctx.now,
    )
