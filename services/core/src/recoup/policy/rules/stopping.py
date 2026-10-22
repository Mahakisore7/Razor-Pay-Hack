"""R2 -- stopping rules (POLICY-ENGINE SS3, PHASE-04 T4.2): terminate the
case, not merely deny the one action in front of it. Every other rule in
this package answers "may *this* action go out"; this one answers "should
anything else ever go out for this case again."

That distinction is why this rule runs second, right after the kill
switch and before every other check (POLICY-ENGINE SS2's own diagram):
if the case is already over, asking whether the customer consented to
*this specific* channel is moot.

`evaluate` itself stays pure and only ever returns the same DENY/DEFER
shape every other rule does (POLICY-ENGINE SS1's third guarantee) --
`STOPPING_OUTCOMES` is what a caller (`execution.stopping.
apply_stopping_rule`, in whichever layer actually has a database) reads
off the returned `rule_id` to decide *which* `CaseState` to close the
case into, and to cancel or defer its remaining scheduled actions. This
rule never touches the database, the case's `state` field aside (read
only, never written) -- deciding is this module's job, doing something
about it is `execution.stopping`'s, the same split T2.10's
`execution.suppression.suppress_case` already drew for `customer_opt_out`
alone before this rule generalised it to R2's full outcome table.

Four of the eight conditions (`dispute_filed`, `mandate_revoked`,
`customer_deceased_or_closed`, `promise_to_pay_active`) stay
permissively `False`/`None` in the one caller that builds a
`PolicyContext` today (`bench.runner`, T3.5/T4.1's own precedent for
`dnd_status`/`mandate`/`rate_limit_tokens`): no dispute ledger, mandate
linkage, account-closure feed, or promise-to-pay capture path exists yet
to populate them for real. `customer_opt_out`, `already_paid`,
`max_attempts_reached`, and `max_case_age` are real and wired -- they
read `ctx.consent_events`, `ctx.case.state`, `action.attempt`, and
`ctx.case.opened_at`, all of which already carry live data.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from recoup.domain.action import Action
from recoup.domain.case import CaseState
from recoup.domain.consent import ConsentSource
from recoup.domain.mandate import MandateStatus
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.policy.context import PolicyContext

__all__ = ["STOPPING_OUTCOMES", "evaluate"]

# POLICY-ENGINE SS3 R2's own outcome table. `None` for the two rule ids
# whose mechanical side is not "close the case into a new state":
# `already_paid` (attribution has already resolved it -- this rule only
# fires to give a stale scheduled action the right audit reason instead
# of the generic `domain_guard`, and to sweep up any siblings attribution
# itself does not cancel) and `promise_to_pay_active` (the case is
# deferred, not terminated -- POLICY-ENGINE SS3's own "DEFER whole case
# to that date", not a `CaseState` transition at all).
STOPPING_OUTCOMES: dict[str, CaseState | None] = {
    "customer_opt_out": CaseState.SUPPRESSED,
    "dispute_filed": CaseState.SUPPRESSED,
    "already_paid": None,
    "max_attempts_reached": CaseState.LOST,
    "max_case_age": CaseState.EXPIRED,
    "mandate_revoked": CaseState.ESCALATED,
    "promise_to_pay_active": None,
    "customer_deceased_or_closed": CaseState.SUPPRESSED,
}

_RECOVERED_STATES = frozenset({CaseState.RECOVERED, CaseState.PARTIALLY_RECOVERED})


def evaluate(action: Action, ctx: PolicyContext) -> PolicyDecision | None:
    rule_id = _first_stopping_condition(action, ctx)
    if rule_id is None:
        return None

    if rule_id == "promise_to_pay_active":
        promise = ctx.promise_to_pay
        assert promise is not None  # _first_stopping_condition already checked this
        defer_until = datetime.combine(promise.committed_date, time.min, tzinfo=UTC)
        return PolicyDecision(
            action_id=action.id,
            attempt=action.attempt,
            verdict=Verdict.DEFER,
            rule_id=rule_id,
            inputs={"committed_date": promise.committed_date.isoformat()},
            defer_until=defer_until,
            decided_at=ctx.now,
        )

    return PolicyDecision(
        action_id=action.id,
        attempt=action.attempt,
        verdict=Verdict.DENY,
        rule_id=rule_id,
        inputs=_inputs_for(rule_id, action, ctx),
        defer_until=None,
        decided_at=ctx.now,
    )


def _first_stopping_condition(action: Action, ctx: PolicyContext) -> str | None:
    """POLICY-ENGINE SS3's own table order -- see this module's docstring
    for why the order among these eight matters the same way it does
    across the whole rule set (SS2.2)."""
    if _opted_out(ctx):
        return "customer_opt_out"
    if ctx.dispute_exists:
        return "dispute_filed"
    if ctx.case.state in _RECOVERED_STATES:
        return "already_paid"
    if action.attempt > ctx.max_attempts:
        return "max_attempts_reached"
    if ctx.now - ctx.case.opened_at > timedelta(days=ctx.max_case_age_days):
        return "max_case_age"
    if ctx.mandate is not None and ctx.mandate.status in (
        MandateStatus.REVOKED,
        MandateStatus.EXPIRED,
    ):
        return "mandate_revoked"
    if ctx.promise_to_pay is not None and ctx.promise_to_pay.is_active(ctx.now):
        return "promise_to_pay_active"
    if ctx.account_closed:
        return "customer_deceased_or_closed"
    return None


def _opted_out(ctx: PolicyContext) -> bool:
    """ "Consent revoked on any channel with `source == SMS_STOP`"
    (POLICY-ENGINE SS3) -- deliberately *any* channel, unlike R4's own
    per-channel `consent_at`: an SMS STOP is a customer telling the
    merchant to stop contacting them altogether, not just on SMS."""
    return any(
        not event.granted and event.source is ConsentSource.SMS_STOP for event in ctx.consent_events
    )


def _inputs_for(rule_id: str, action: Action, ctx: PolicyContext) -> dict[str, object]:
    if rule_id == "max_attempts_reached":
        return {"attempt": action.attempt, "max_attempts": ctx.max_attempts}
    if rule_id == "max_case_age":
        return {
            "opened_at": ctx.case.opened_at.isoformat(),
            "now": ctx.now.isoformat(),
            "max_case_age_days": ctx.max_case_age_days,
        }
    if rule_id == "already_paid":
        return {"case_state": ctx.case.state.value}
    if rule_id == "mandate_revoked":
        assert ctx.mandate is not None
        return {"mandate_id": ctx.mandate.id, "mandate_status": ctx.mandate.status.value}
    return {}
