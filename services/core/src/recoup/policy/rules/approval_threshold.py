"""R10 -- approval threshold (POLICY-ENGINE SS3): cases above `at_risk`
threshold require a human before anything executes. Unlike every other
rule here, this is not a fresh per-action computation -- `Case.state`
(DOMAIN-MODEL SS4.1's own transition table restricts `AWAITING_APPROVAL`
to arriving only from `PLANNED`) is what actually remembers whether a
human has cleared this case:

- `requires_approval` (this module's own reusable predicate) is what
  `planning.repository.persist_plan` calls to decide whether a newly
  planned case goes to `AWAITING_APPROVAL` instead of `EXECUTING` in the
  first place. `planning` sits above `policy` in the layering contract,
  so it may import this.
- The future approval queue (T4.6) is what takes a case back out, by
  transitioning it to `EXECUTING` once a human approves (or `SUPPRESSED`
  on reject).
- This rule only ever reads that already-decided fact off `ctx.case.
  state`; it does not recompute the threshold itself, so an approved
  case's every subsequent action passes without re-litigating a decision
  a human already made -- and a case that never needed approval (at_risk
  at or under threshold) never sees this rule fire at all, since it is
  never routed through `AWAITING_APPROVAL` to begin with.
"""

from __future__ import annotations

from recoup.domain.action import Action
from recoup.domain.case import CaseState
from recoup.domain.money import Money
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["APPROVAL_THRESHOLD", "evaluate", "requires_approval"]

APPROVAL_THRESHOLD = Money(2_500_000)  # ₹25,000, POLICY-ENGINE SS7


def requires_approval(at_risk: Money) -> bool:
    return at_risk > APPROVAL_THRESHOLD


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    if ctx.case.state is not CaseState.AWAITING_APPROVAL:
        return None
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DEFER,
        rule_id="awaiting_approval",
        inputs={"at_risk_paise": ctx.case.at_risk.paise},
        # No natural resume time exists -- unlike quiet hours or a
        # frequency cap aging out, this only clears when a human acts
        # (T4.6's approve/reject endpoints), which this rule has no way
        # to predict.
        defer_until=None,
        decided_at=ctx.now,
    )
