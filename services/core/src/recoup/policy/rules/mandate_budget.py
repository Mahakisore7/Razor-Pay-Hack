"""R9 -- mandate budget (POLICY-ENGINE SS3): a re-presentation budget is
finite per cycle and spent whether or not the attempt succeeds
(`domain.mandate.Mandate`, DOMAIN-MODEL SS11, already models this fully).
Only steps that actually consume it are gated -- `action.
consumes_mandate_budget`, read straight off the playbook step that
declared it (`PlaybookStep.consumes_mandate_budget`), the same way R3
(domain_guards.py) already reads `action.channel` to decide whether its
own mandate check (`Mandate.authorize_debit`) applies.

Not implemented here: POLICY-ENGINE SS3's "reserved atomically before the
gateway call" half. That needs a real case-to-mandate association, which
does not exist anywhere in this schema yet (no `cases.mandate_id`, no
repository loader for `MandateRow`) -- `ctx.mandate` has been `None` in
every caller since T2.5 (domain_guards.py's own mandate checks are
already dormant for the identical reason), and building that association
is a schema change, not a rule file. This rule is therefore correct and
fully tested against any `Mandate` it is handed, but -- like R1's kill
switch and R5's DND status before their own dedicated wiring -- provably
inert until a caller actually populates `ctx.mandate` for a real case.
"""

from __future__ import annotations

from recoup.domain.action import Action
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["evaluate"]


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    if not action.consumes_mandate_budget or ctx.mandate is None:
        return None
    if ctx.mandate.remaining_representations > 0:
        return None
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DENY,
        rule_id="mandate_exhausted",
        inputs={
            "mandate_id": ctx.mandate.id,
            "representation_cap": ctx.mandate.representation_cap,
            "representations_used_this_cycle": ctx.mandate.representations_used_this_cycle,
        },
        defer_until=None,
        decided_at=ctx.now,
    )
