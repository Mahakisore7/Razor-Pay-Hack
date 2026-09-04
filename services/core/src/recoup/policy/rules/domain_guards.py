"""R3 -- domain guards (POLICY-ENGINE SS3): refusals the domain model
already knows are pointless. The planner's safety net -- a planning bug
should not become a customer-facing error.

Four checks, in order:

1. The case is already terminal -- nothing should act on it again.
2. The case is a `control`-arm case -- I7 (`Case.transition_to`) already
   forbids a control case from reaching `EXECUTING`, so this cannot fire
   through the normal state machine; it is defence in depth for the same
   reason `cost_within_ceiling` backs `Case.record_cost` at the database
   layer. This is the mechanism behind P9 (POLICY-ENGINE SS6.2): a control
   case must accumulate zero executed actions under any input sequence.
3. A `payment_retry` proposed against a decline category the domain model
   already knows is not retryable (`DeclineCategory.retryable`). Only
   checked when the case's diagnosed root cause maps onto a known decline
   category -- `RootCause` is playbook-defined vocabulary, not a closed
   enum (domain/diagnosis.py), so an unrecognised one is not an error here,
   just not enough information to refuse on.
4. A `payment_retry` against a mandate that cannot authorise this debit --
   delegated entirely to `Mandate.authorize_debit`, which already encodes
   both the max-amount and validity-window checks (DOMAIN-MODEL SS11).

Not implemented here: "channel not registered" and "payload failing its
schema" (POLICY-ENGINE SS3, R3). Both need the channel registry
(execution/channels/, T2.7), which does not exist yet -- policy cannot
depend on it even once it does (layering places `execution` above
`policy`), so that pair of checks belongs in the executor's own admission
path, not here.
"""

from __future__ import annotations

from typing import Any

from recoup.domain.action import Action, Channel
from recoup.domain.case import Arm
from recoup.domain.decline import DeclineCategory
from recoup.domain.mandate import MandateAmountExceededError, MandateNotValidError
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["evaluate"]

_DECLINE_CATEGORY_VALUES = frozenset(category.value for category in DeclineCategory)


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    violation = _first_violation(action, ctx)
    if violation is None:
        return None
    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DENY,
        rule_id="domain_guard",
        inputs=violation,
        defer_until=None,
        decided_at=ctx.now,
    )


def _first_violation(action: Action, ctx: PolicyContext) -> dict[str, Any] | None:
    if ctx.case.is_terminal:
        return {"reason": "case_is_terminal", "state": ctx.case.state.value}

    if ctx.case.arm == Arm.CONTROL:
        return {"reason": "control_arm_case"}

    if action.channel != Channel.PAYMENT_RETRY:
        return None

    root_cause = ctx.case.diagnosis.root_cause if ctx.case.diagnosis is not None else None
    if root_cause is not None and root_cause in _DECLINE_CATEGORY_VALUES:
        category = DeclineCategory(root_cause)
        if not category.retryable:
            return {"reason": "decline_not_retryable", "decline_category": category.value}

    if ctx.mandate is not None:
        try:
            ctx.mandate.authorize_debit(ctx.case.at_risk, ctx.now.date())
        except MandateAmountExceededError as exc:
            return {
                "reason": "mandate_amount_exceeded",
                "amount_paise": exc.amount.paise,
                "max_amount_paise": exc.max_amount.paise,
            }
        except MandateNotValidError as exc:
            return {
                "reason": "mandate_not_valid",
                "at": exc.at.isoformat(),
                "valid_from": exc.valid_from.isoformat(),
                "valid_until": exc.valid_until.isoformat(),
            }

    return None
