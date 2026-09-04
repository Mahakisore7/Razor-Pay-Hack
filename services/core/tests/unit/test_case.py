"""Case's state machine is an explicit table (DOMAIN-MODEL SS4.1): anything
not in it raises `IllegalTransition` rather than silently moving the case.
"""

import pytest

from recoup.domain.case import (
    TERMINAL_STATES,
    Arm,
    CaseState,
    CostCeilingExceededError,
    IllegalTransition,
    assign_arm,
)
from recoup.domain.identifiers import CaseId, uuid7
from recoup.domain.money import Money
from tests.factories import make_case


def test_legal_transition_updates_state() -> None:
    case = make_case(state=CaseState.DETECTED)
    case.transition_to(CaseState.DIAGNOSING)
    assert case.state == CaseState.DIAGNOSING


def test_illegal_transition_raises() -> None:
    case = make_case(state=CaseState.DETECTED)
    with pytest.raises(IllegalTransition):
        case.transition_to(CaseState.RECOVERED)
    assert case.state == CaseState.DETECTED  # rejected transition leaves state unchanged


def test_illegal_transition_carries_context() -> None:
    case = make_case(state=CaseState.DETECTED)
    with pytest.raises(IllegalTransition) as exc_info:
        case.transition_to(CaseState.EXECUTING)
    assert exc_info.value.case_id == case.id
    assert exc_info.value.from_state == CaseState.DETECTED
    assert exc_info.value.to_state == CaseState.EXECUTING


def test_terminal_state_has_no_outgoing_transition() -> None:
    case = make_case(state=CaseState.RECOVERED)
    with pytest.raises(IllegalTransition):
        case.transition_to(CaseState.DIAGNOSING)


def test_executing_may_self_loop_for_a_retry() -> None:
    case = make_case(state=CaseState.EXECUTING, arm=Arm.TREATMENT)
    case.transition_to(CaseState.EXECUTING)
    assert case.state == CaseState.EXECUTING


def test_terminal_states_match_states_absent_from_the_table() -> None:
    assert {
        CaseState.RECOVERED,
        CaseState.PARTIALLY_RECOVERED,
        CaseState.LOST,
        CaseState.EXPIRED,
        CaseState.SUPPRESSED,
    } == TERMINAL_STATES


def test_is_terminal_reflects_state() -> None:
    assert make_case(state=CaseState.LOST).is_terminal is True
    assert make_case(state=CaseState.PLANNED).is_terminal is False


# --- I7: a control-arm case never reaches EXECUTING ------------------------


def test_control_case_cannot_transition_directly_to_executing() -> None:
    case = make_case(state=CaseState.PLANNED, arm=Arm.CONTROL)
    with pytest.raises(IllegalTransition):
        case.transition_to(CaseState.EXECUTING)


def test_control_case_can_still_reach_holdout() -> None:
    case = make_case(state=CaseState.PLANNED, arm=Arm.CONTROL)
    case.transition_to(CaseState.HOLDOUT)
    assert case.state == CaseState.HOLDOUT


def test_treatment_case_can_reach_executing() -> None:
    case = make_case(state=CaseState.PLANNED, arm=Arm.TREATMENT)
    case.transition_to(CaseState.EXECUTING)
    assert case.state == CaseState.EXECUTING


# --- I2: cost_spent <= cost_ceiling at all times ----------------------------


def test_record_cost_accumulates_within_ceiling() -> None:
    case = make_case(cost_ceiling=Money(100), cost_spent=Money(0))
    case.record_cost(Money(60))
    case.record_cost(Money(40))
    assert case.cost_spent == Money(100)


def test_record_cost_rejects_breach_of_ceiling() -> None:
    case = make_case(cost_ceiling=Money(100), cost_spent=Money(90))
    with pytest.raises(CostCeilingExceededError):
        case.record_cost(Money(11))
    assert case.cost_spent == Money(90)  # rejected spend leaves cost_spent unchanged


# --- Arm assignment ----------------------------------------------------------


def test_assign_arm_is_deterministic_for_the_same_seed_and_case() -> None:
    case_id = CaseId(uuid7())
    assert assign_arm(42, case_id) == assign_arm(42, case_id)


def test_assign_arm_can_differ_across_cases() -> None:
    seed = 42
    arms = {assign_arm(seed, CaseId(uuid7())) for _ in range(200)}
    assert arms == set(Arm)  # 200 draws should hit all three arms at least once
