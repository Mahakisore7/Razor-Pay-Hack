"""R1 -- kill switch (POLICY-ENGINE SS3). The most absolute rule, and so
the first one evaluated: if it is tripped, nothing else about this action
matters and every other rule's lookup is wasted work.
"""

from __future__ import annotations

from recoup.domain.action import Action
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["evaluate"]


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    playbook_tripped = ctx.playbook_id in ctx.kill_switch.tripped_playbooks
    if not (ctx.kill_switch.global_tripped or playbook_tripped):
        return None
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DENY,
        rule_id="kill_switch_active",
        inputs={
            "global_tripped": ctx.kill_switch.global_tripped,
            "playbook_id": ctx.playbook_id,
            "playbook_tripped": playbook_tripped,
        },
        defer_until=None,
        decided_at=ctx.now,
    )
