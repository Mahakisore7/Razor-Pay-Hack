"""Playbook selection and plan instantiation for one case (PHASE-02 T2.4).

Fixed-schedule timing only: TR-19's contextual bandit is P5
(`planning/timing/` stays untouched until then). Every `PlannedStep.due_at`
here comes straight from the playbook's own `fixed`/`relative` offsets,
resolved against the injected clock -- nothing to train, nothing
non-deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from recoup.domain.case import Case
from recoup.domain.diagnosis import Diagnosis
from recoup.domain.errors import RecoupError
from recoup.domain.money import Money
from recoup.domain.plan import Plan, PlannedStep
from recoup.domain.signal import LeakClass
from recoup.planning.playbooks.schema import Playbook, PlaybookStep
from recoup.platform.clock import Clock

__all__ = [
    "DroppedStep",
    "PlanningError",
    "PlanningResult",
    "UnaffordablePlaybookError",
    "build_plan",
    "select_playbook",
]


class PlanningError(RecoupError):
    """A playbook could not be turned into a coherent plan -- a malformed
    step graph (a `relative` step whose anchor was dropped), not a caller
    mistake, so it is raised rather than silently patched up."""


class UnaffordablePlaybookError(RecoupError):
    """Even the playbook's `required` steps cost more than the computed
    cost ceiling. TR-18 has the planner drop optional steps to fit, but
    DOMAIN-MODEL SS6.1 forbids dropping a `required` one -- only a policy
    DENY may halt those -- so an unaffordable required set is a
    configuration problem to surface, not a plan to shrink further.
    """

    def __init__(
        self, case_id: object, playbook_id: str, required_cost: Money, ceiling: Money
    ) -> None:
        self.playbook_id = playbook_id
        self.required_cost = required_cost
        self.ceiling = ceiling
        super().__init__(
            f"case {case_id}: playbook {playbook_id!r} required steps cost "
            f"{required_cost.paise}p, exceeding the {ceiling.paise}p cost ceiling"
        )


@dataclass(frozen=True, slots=True)
class DroppedStep:
    """A step the planner declined to include, kept so the caller can audit
    it (TR-18: "MUST audit each drop"). Writing the audit event itself is
    T2.9's scope -- this is the fact that event is built from.
    """

    step_id: str
    expected_cost: Money
    reason: str


@dataclass(frozen=True, slots=True)
class PlanningResult:
    plan: Plan
    dropped_steps: tuple[DroppedStep, ...]


def select_playbook(
    playbooks: Mapping[str, Playbook], diagnosis: Diagnosis, leak_class: LeakClass
) -> Playbook | None:
    """The playbook whose `applies_to` matches this diagnosis's root cause
    and this signal's leak class, or `None` if no playbook claims it --
    an abstained diagnosis (`root_cause is None`) always misses, same as an
    unrecognised root cause. Only one playbook exists this phase, so `None`
    is the common case for anything other than `insufficient_funds`; a
    generic fallback playbook (ARCHITECTURE SS6.2) is out of this phase's
    scope.
    """
    root_cause = diagnosis.root_cause
    if root_cause is None:
        return None
    for playbook in playbooks.values():
        if playbook.applies_to.root_cause == root_cause and (
            leak_class in playbook.applies_to.leak_classes
        ):
            return playbook
    return None


def _applicable_steps(playbook: Playbook, at_risk: Money) -> list[PlaybookStep]:
    return [
        step
        for step in playbook.steps
        if step.skip_if is None
        or step.skip_if.at_risk_below_paise is None
        or at_risk.paise >= step.skip_if.at_risk_below_paise
    ]


def _cost_ceiling(playbook: Playbook, at_risk: Money) -> Money:
    pct = Decimal(str(playbook.cost_ceiling_pct))
    ceiling_paise = (Decimal(at_risk.paise) * pct / Decimal(100)).to_integral_value(
        rounding=ROUND_FLOOR
    )
    return Money(ceiling_paise, at_risk.currency)


def _fit_to_ceiling(
    steps: list[PlaybookStep], ceiling: Money
) -> tuple[list[PlaybookStep], list[DroppedStep]]:
    """TR-18: drop the lowest-value steps until the plan fits. "Value" is
    playbook declaration order -- a step's author places the steps they'd
    keep first (DOMAIN-MODEL SS6.1's own example ends on its least-critical
    step, `escalate`) -- so the last non-`required` step is dropped first,
    repeating until the total fits or nothing droppable remains.
    """
    included = list(steps)
    dropped: list[DroppedStep] = []
    total = sum(step.expected_cost_paise for step in included)
    while total > ceiling.paise:
        index = next((i for i in reversed(range(len(included))) if not included[i].required), None)
        if index is None:
            break
        step = included.pop(index)
        dropped.append(
            DroppedStep(
                step_id=step.id,
                expected_cost=Money(step.expected_cost_paise, ceiling.currency),
                reason="cost_ceiling_exceeded",
            )
        )
        total -= step.expected_cost_paise
    return included, dropped


def build_plan(case: Case, playbook: Playbook, clock: Clock) -> PlanningResult:
    """TR-17: pins `playbook.version` on the `Plan`, so a later edit to this
    playbook never retroactively changes a case already planned against it.

    Steps whose `skip_if.at_risk_below_paise` excludes this case's
    `at_risk` are left out entirely -- not dropped, since TR-18's "dropped"
    steps are specifically the ones cut for cost, and an audited drop
    implies a step that would otherwise have run.

    `due_at` is computed in playbook order, so a `relative` step can only
    reference an earlier one -- enforced at load time (schema.py). If the
    cost-ceiling fit above happens to have dropped that earlier step, the
    reference is dangling; this is surfaced as `PlanningError` rather than
    a `KeyError`, though it cannot happen for `insufficient-funds` today
    (its only `relative` step, `payment_link`, is dropped before its
    `retry` anchor ever would be -- see `_fit_to_ceiling`'s ordering).
    """
    ceiling = _cost_ceiling(playbook, case.at_risk)
    applicable = _applicable_steps(playbook, case.at_risk)
    included, dropped = _fit_to_ceiling(applicable, ceiling)

    total_paise = sum(step.expected_cost_paise for step in included)
    if total_paise > ceiling.paise:
        required_paise = sum(step.expected_cost_paise for step in included if step.required)
        raise UnaffordablePlaybookError(
            case.id,
            playbook.id,
            Money(required_paise, case.at_risk.currency),
            ceiling,
        )

    due_at_by_step: dict[str, datetime] = {}
    planned_steps: list[PlannedStep] = []
    for step in included:
        if step.timing.policy == "relative":
            anchor = step.timing.after_step
            if anchor is None or anchor not in due_at_by_step:
                raise PlanningError(
                    f"case {case.id}: step {step.id!r} timing depends on "
                    f"{anchor!r}, which was dropped or does not precede it"
                )
            due_at = due_at_by_step[anchor] + timedelta(hours=step.timing.offset_hours)
        else:
            due_at = clock.now() + timedelta(hours=step.timing.offset_hours)
        due_at_by_step[step.id] = due_at
        planned_steps.append(
            PlannedStep(
                step_id=step.id,
                due_at=due_at,
                expected_cost=Money(step.expected_cost_paise, case.at_risk.currency),
            )
        )

    plan = Plan(
        case_id=case.id,
        playbook_id=playbook.id,
        playbook_version=playbook.version,
        steps=tuple(planned_steps),
        total_expected_cost=Money(total_paise, case.at_risk.currency),
        created_at=clock.now(),
    )
    return PlanningResult(plan=plan, dropped_steps=tuple(dropped))
