"""Plan invariants (DOMAIN-MODEL SS6.2): steps must be due in non-decreasing
order, since the executor works through them in sequence."""

from datetime import timedelta

import pytest

from recoup.domain.identifiers import CaseId, uuid7
from recoup.domain.money import Money
from recoup.domain.plan import Plan, PlannedStep
from tests.factories import EPOCH


def _step(step_id: str, offset_hours: int) -> PlannedStep:
    return PlannedStep(
        step_id=step_id, due_at=EPOCH + timedelta(hours=offset_hours), expected_cost=Money(500)
    )


def test_plan_with_non_decreasing_due_dates_constructs() -> None:
    plan = Plan(
        case_id=CaseId(uuid7()),
        playbook_id="insufficient-funds",
        playbook_version=3,
        steps=(_step("pre_debit_notice", 0), _step("timed_retry", 4), _step("timed_retry", 4)),
        total_expected_cost=Money(1_500),
        created_at=EPOCH,
    )
    assert len(plan.steps) == 3


def test_plan_rejects_out_of_order_due_dates() -> None:
    with pytest.raises(ValueError, match="non-decreasing"):
        Plan(
            case_id=CaseId(uuid7()),
            playbook_id="insufficient-funds",
            playbook_version=3,
            steps=(_step("later_step", 4), _step("earlier_step", 0)),
            total_expected_cost=Money(1_000),
            created_at=EPOCH,
        )


def test_plan_with_a_single_step_or_no_steps_is_trivially_ordered() -> None:
    plan = Plan(
        case_id=CaseId(uuid7()),
        playbook_id="insufficient-funds",
        playbook_version=3,
        steps=(),
        total_expected_cost=Money(0),
        created_at=EPOCH,
    )
    assert plan.steps == ()
