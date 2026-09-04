"""`evaluate(action, ctx)` -- the compliance gate (POLICY-ENGINE SS1, T2.5).

A pure function of `(action, ctx)`: no clock read, no RNG, no repository
lookup inside it or any rule it calls, so a denial is exactly replayable
from its `PolicyDecision.inputs` alone (POLICY-ENGINE SS1's third
guarantee). `import-linter`'s `policy-is-model-free` contract additionally
forbids `anthropic` (and `recoup.diagnosis`/`recoup.planning`, which could
carry one in their own transitive closure) anywhere in this package.

Rules run in the order POLICY-ENGINE SS2.2 mandates: cheapest and most
absolute first, so the recorded `rule_id` names the most fundamental
reason an action was refused, not an incidental one. Four rules this phase
-- T2.5's "skeleton" (kill switch, domain guards, consent, cost ceiling).
The rest of POLICY-ENGINE SS3's rule set (stopping rules, DND, quiet
hours, frequency cap, mandate budget, approval threshold, rate limits)
lands with the phase that actually needs it; `evaluate`'s dispatch loop
does not change shape when they do -- only `_RULES` grows.
"""

from __future__ import annotations

from recoup.domain.action import Action
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext
from recoup.policy.rules import consent, cost_ceiling, domain_guards, kill_switch
from recoup.policy.rules.base import Rule

__all__ = ["evaluate"]

_RULES: tuple[Rule, ...] = (
    kill_switch.evaluate,
    domain_guards.evaluate,
    consent.evaluate,
    cost_ceiling.evaluate,
)


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision:
    for rule in _RULES:
        decision = rule(action, ctx)
        if decision is not None:
            return decision
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.ALLOW,
        rule_id=None,
        inputs={},
        defer_until=None,
        decided_at=ctx.now,
    )
