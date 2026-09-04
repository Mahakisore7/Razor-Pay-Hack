"""Unit tests for `recoup.planning.planner` (T2.4) -- pure functions of a
`Case`, a `Playbook`, and a clock, so every case here is in-memory.
"""

from datetime import UTC, datetime, timedelta

import pytest

from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.diagnosis import Diagnosis, DiagnosisMethod, Hypothesis, RootCause
from recoup.domain.identifiers import CaseId, CustomerRef, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.signal import LeakClass
from recoup.planning.planner import (
    PlanningError,
    UnaffordablePlaybookError,
    build_plan,
    select_playbook,
)
from recoup.planning.playbooks.loader import load_playbooks
from recoup.planning.playbooks.schema import Playbook
from recoup.platform.clock import FrozenClock

_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
_CUSTOMER = CustomerRef(id="cust-1", razorpay_customer_id="cust_1", contact_hash="h1")


def _case(at_risk_paise: int) -> Case:
    return Case(
        id=CaseId(uuid7()),
        signal_id=SignalId(uuid7()),
        customer=_CUSTOMER,
        at_risk=Money(at_risk_paise, Currency.INR),
        state=CaseState.DIAGNOSING,
        arm=Arm.TREATMENT,
        opened_at=_CLOCK.now(),
        cost_spent=Money(0, Currency.INR),
        cost_ceiling=Money(0, Currency.INR),
    )


def _diagnosis(root_cause: str | None) -> Diagnosis:
    hypotheses = (
        ()
        if root_cause is None
        else (Hypothesis(RootCause(root_cause), confidence=1.0, evidence=(), narration=None),)
    )
    return Diagnosis(
        case_id=CaseId(uuid7()),
        hypotheses=hypotheses,
        method=DiagnosisMethod.ABSTAINED if root_cause is None else DiagnosisMethod.STATISTICAL,
        computed_at=_CLOCK.now(),
        llm_model=None,
        fallback_reason=None,
    )


def _playbook(**overrides: object) -> Playbook:
    base: dict[str, object] = {
        "id": "test-playbook",
        "version": 1,
        "applies_to": {"root_cause": "insufficient_funds", "leak_classes": ["L1"]},
        "cost_ceiling_pct": 4.0,
        "max_attempts": 3,
        "max_case_age_days": 21,
        "steps": [
            {
                "id": "retry",
                "channel": "payment_retry",
                "timing": {"policy": "fixed", "offset_hours": 6},
                "expected_cost_paise": 0,
            },
            {
                "id": "payment_link",
                "channel": "link",
                "timing": {"policy": "relative", "after_step": "retry", "offset_hours": 24},
                "expected_cost_paise": 0,
            },
        ],
    }
    base.update(overrides)
    return Playbook.model_validate(base)


# --- select_playbook -----------------------------------------------------------


def test_select_playbook_matches_on_root_cause_and_leak_class() -> None:
    playbooks = {"test-playbook": _playbook()}
    diagnosis = _diagnosis("insufficient_funds")
    result = select_playbook(playbooks, diagnosis, LeakClass.L1_FAILED_ONE_TIME_PAYMENT)
    assert result is playbooks["test-playbook"]


def test_select_playbook_returns_none_for_an_unmatched_root_cause() -> None:
    playbooks = {"test-playbook": _playbook()}
    diagnosis = _diagnosis("mandate_revoked")
    assert select_playbook(playbooks, diagnosis, LeakClass.L1_FAILED_ONE_TIME_PAYMENT) is None


def test_select_playbook_returns_none_for_an_unmatched_leak_class() -> None:
    playbooks = {"test-playbook": _playbook()}
    diagnosis = _diagnosis("insufficient_funds")
    assert select_playbook(playbooks, diagnosis, LeakClass.L4_ABANDONED_CHECKOUT) is None


def test_select_playbook_returns_none_for_an_abstained_diagnosis() -> None:
    playbooks = {"test-playbook": _playbook()}
    diagnosis = _diagnosis(None)
    assert select_playbook(playbooks, diagnosis, LeakClass.L1_FAILED_ONE_TIME_PAYMENT) is None


# --- build_plan: happy path, against the real shipped playbook -----------------


def test_build_plan_against_the_shipped_insufficient_funds_playbook() -> None:
    playbook = load_playbooks()["insufficient-funds"]
    case = _case(at_risk_paise=500_000)

    result = build_plan(case, playbook, _CLOCK)

    assert result.dropped_steps == ()
    plan = result.plan
    assert plan.case_id == case.id
    assert plan.playbook_id == "insufficient-funds"
    assert plan.playbook_version == 1
    assert plan.created_at == _CLOCK.now()
    assert [step.step_id for step in plan.steps] == ["retry", "payment_link"]

    retry, payment_link = plan.steps
    assert retry.due_at == _CLOCK.now() + timedelta(hours=6)
    assert payment_link.due_at == retry.due_at + timedelta(hours=24)
    assert plan.total_expected_cost == Money(0, Currency.INR)


# --- build_plan: skip_if --------------------------------------------------------


def test_build_plan_omits_a_step_whose_skip_if_excludes_this_case() -> None:
    playbook = _playbook(
        steps=[
            {
                "id": "retry",
                "channel": "payment_retry",
                "timing": {"policy": "fixed", "offset_hours": 0},
                "expected_cost_paise": 0,
                "skip_if": {"at_risk_below_paise": 50_000},
            }
        ]
    )
    case = _case(at_risk_paise=1_000)

    result = build_plan(case, playbook, _CLOCK)

    assert result.plan.steps == ()
    assert result.dropped_steps == ()  # skipped, not dropped -- never counted against the ceiling


# --- build_plan: cost-ceiling fit (TR-18) ---------------------------------------


def test_build_plan_drops_the_last_non_required_step_to_fit_the_ceiling() -> None:
    playbook = _playbook(
        cost_ceiling_pct=4.0,
        steps=[
            {
                "id": "step_a",
                "channel": "payment_retry",
                "timing": {"policy": "fixed", "offset_hours": 0},
                "expected_cost_paise": 30,
            },
            {
                "id": "step_b",
                "channel": "link",
                "timing": {"policy": "fixed", "offset_hours": 1},
                "expected_cost_paise": 30,
            },
        ],
    )
    case = _case(at_risk_paise=1_000)  # ceiling = floor(1000 * 4 / 100) = 40 paise

    result = build_plan(case, playbook, _CLOCK)

    assert [step.step_id for step in result.plan.steps] == ["step_a"]
    assert result.plan.total_expected_cost == Money(30, Currency.INR)
    assert len(result.dropped_steps) == 1
    assert result.dropped_steps[0].step_id == "step_b"
    assert result.dropped_steps[0].expected_cost == Money(30, Currency.INR)
    assert result.dropped_steps[0].reason == "cost_ceiling_exceeded"


def test_build_plan_never_drops_a_required_step() -> None:
    playbook = _playbook(
        cost_ceiling_pct=10.0,
        steps=[
            {
                "id": "step_a",
                "channel": "payment_retry",
                "timing": {"policy": "fixed", "offset_hours": 0},
                "expected_cost_paise": 5,
                "required": True,
            },
            {
                "id": "step_b",
                "channel": "link",
                "timing": {"policy": "fixed", "offset_hours": 1},
                "expected_cost_paise": 200,
            },
        ],
    )
    case = _case(at_risk_paise=1_000)  # ceiling = 100 paise

    result = build_plan(case, playbook, _CLOCK)

    assert [step.step_id for step in result.plan.steps] == ["step_a"]
    assert result.dropped_steps[0].step_id == "step_b"


def test_build_plan_raises_when_required_steps_alone_exceed_the_ceiling() -> None:
    playbook = _playbook(
        cost_ceiling_pct=4.0,
        steps=[
            {
                "id": "step_a",
                "channel": "payment_retry",
                "timing": {"policy": "fixed", "offset_hours": 0},
                "expected_cost_paise": 50,
                "required": True,
            }
        ],
    )
    case = _case(at_risk_paise=100)  # ceiling = floor(100 * 4 / 100) = 4 paise

    with pytest.raises(UnaffordablePlaybookError) as exc_info:
        build_plan(case, playbook, _CLOCK)

    assert exc_info.value.playbook_id == "test-playbook"
    assert exc_info.value.required_cost == Money(50, Currency.INR)
    assert exc_info.value.ceiling == Money(4, Currency.INR)


def test_build_plan_raises_when_a_relative_steps_anchor_is_dropped_for_cost() -> None:
    """An edge case the shipped playbook cannot hit (see planner.py's
    docstring): a `required` step relative to an `optional` one that cost
    fitting removes. Surfaced as `PlanningError`, not a `KeyError`."""
    playbook = _playbook(
        cost_ceiling_pct=4.0,
        steps=[
            {
                "id": "step1",
                "channel": "payment_retry",
                "timing": {"policy": "fixed", "offset_hours": 0},
                "expected_cost_paise": 80,
            },
            {
                "id": "step2",
                "channel": "link",
                "timing": {"policy": "relative", "after_step": "step1", "offset_hours": 4},
                "expected_cost_paise": 50,
                "required": True,
            },
        ],
    )
    case = _case(at_risk_paise=2_500)  # ceiling = floor(2500 * 4 / 100) = 100 paise

    with pytest.raises(PlanningError, match="step2"):
        build_plan(case, playbook, _CLOCK)
