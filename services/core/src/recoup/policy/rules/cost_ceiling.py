"""R8 -- cost ceiling (POLICY-ENGINE SS3): `case.cost_spent + action.cost >
case.cost_ceiling` denies. `case.cost_ceiling` is read as-is rather than
recomputed from `playbook.cost_ceiling_pct * case.at_risk` here -- planning
(T2.4) is what computes and pins that value onto the case in the first
place, and `policy` cannot import `planning` (layering); this rule trusts
the value the case already carries, the same way it trusts `cost_spent`.

T4.1 adds R8's other half, the global daily spend cap POLICY-ENGINE SS3
also assigns to this rule -- a blast-radius backstop across *all* cases,
not this one case's own budget. POLICY-ENGINE SS7's illustrative
`config/policy.yaml` names ₹5,00,000/day; no live worker or scheduler
exists yet to aggregate a real `daily_spend` from (same gap `context.py`'s
`daily_spend` field comment already notes), so this half stays
structurally correct but untriggered outside a caller that deliberately
sets `daily_spend` near the cap, the same way R1/R5/R11 already do for
their own not-yet-live data sources.
"""

from __future__ import annotations

from recoup.domain.action import Action
from recoup.domain.money import Money
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["evaluate"]

GLOBAL_DAILY_CAP = Money(50_000_000)  # ₹5,00,000/day, POLICY-ENGINE SS7


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    projected_case = ctx.case.cost_spent + action.cost
    if projected_case > ctx.case.cost_ceiling:
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

    projected_daily = ctx.daily_spend + action.cost
    if projected_daily > GLOBAL_DAILY_CAP:
        return PolicyDecision(
            action_id=action.id,
            attempt=action.attempt,
            verdict=Verdict.DENY,
            rule_id="cost_ceiling",
            inputs={
                "daily_spend_paise": ctx.daily_spend.paise,
                "action_cost_paise": action.cost.paise,
                "global_daily_cap_paise": GLOBAL_DAILY_CAP.paise,
            },
            defer_until=None,
            decided_at=ctx.now,
        )

    return None
