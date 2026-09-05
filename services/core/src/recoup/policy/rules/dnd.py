"""R5 -- DND / DNC registry (POLICY-ENGINE SS3). Applies to promotional
actions only: transactional notifications (a pre-debit notice required by
RBI, a payment receipt) are exempt from DND by regulation. This rule reads
`action.category` alone -- a promotional message mis-classified as
transactional is caught by the compliance validator (T4.3), not here.

Runs after R4 (consent): the two are deliberately independent checks, not
layers of the same fold. Consent is what *this merchant* collected from
the customer, per channel; DND is a national registry the customer never
told this merchant about, and it does not vary by channel. Folding DND
into `consent_events`/`consent_at` would make a DND-registered customer's
*transactional* messages fail R4's blanket consent check before R5 ever
got a chance to exempt them -- exactly the regulatory carve-out this rule
exists to apply. That is why DND status arrives as its own
`PolicyContext.dnd_status` field instead.
"""

from __future__ import annotations

from recoup.domain.action import Action, ActionCategory
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["evaluate"]


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    if action.category != ActionCategory.PROMOTIONAL:
        return None
    if not ctx.dnd_status.registered:
        return None
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DENY,
        rule_id="dnd_registered",
        inputs={"category": action.category.value},
        defer_until=None,
        decided_at=ctx.now,
    )
