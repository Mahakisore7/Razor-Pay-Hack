"""R8 -- cost ceiling (POLICY-ENGINE SS3): `case.cost_spent + action.cost >
case.cost_ceiling` denies. `case.cost_ceiling` is read as-is rather than
recomputed from `playbook.cost_ceiling_pct * case.at_risk` here -- planning
(T2.4) is what computes and pins that value onto the case in the first
place, and `policy` cannot import `planning` (layering); this rule trusts
the value the case already carries, the same way it trusts `cost_spent`.

The global daily spend cap POLICY-ENGINE SS3 also assigns to R8 is not
implemented here -- it needs `daily_spend`, which is not part of T2.5's
scope (see context.py's docstring).
"""

from __future__ import annotations

from recoup.domain.action import Action
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["evaluate"]


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    projected = ctx.case.cost_spent + action.cost
    if projected <= ctx.case.cost_ceiling:
        return None
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DENY,
        rule_id="cost_ceiling",
        inputs={
            "cost_spent_paise": ctx.case.cost_spent.paise,
            "action_cost_paise": action.cost.paise,
            "cost_ceiling_paise": ctx.case.cost_ceiling.paise,
        },
        defer_until=None,
        decided_at=ctx.now,
    )
