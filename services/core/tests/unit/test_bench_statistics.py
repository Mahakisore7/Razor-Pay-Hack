"""Unit tests for `recoup.bench.statistics` (T3.6): pure functions over
hand-built `CaseOutcome` fixtures, checked against METRICS-AND-KPIS.md's
own formulas -- no database required.
"""

from datetime import UTC, datetime, timedelta

import pytest

from recoup.bench.statistics import (
    CaseOutcome,
    arm_statistics,
    compare_arms,
    compute_statistics,
    economics,
    guardrails,
)
from recoup.domain.case import Arm
from recoup.domain.identifiers import CaseId, uuid7
from recoup.domain.money import Money
from recoup.domain.outcome import OutcomeKind

_T0 = datetime(2026, 4, 10, tzinfo=UTC)


def _case(
    arm: Arm,
    *,
    at_risk_paise: int,
    recovered_paise: int = 0,
    cost_paise: int = 0,
    outcome_kind: OutcomeKind | None = None,
    reason_code: str | None = None,
    contact_events: tuple[datetime, ...] = (),
) -> CaseOutcome:
    return CaseOutcome(
        case_id=CaseId(uuid7()),
        arm=arm,
        at_risk=Money(at_risk_paise),
        recovered=Money(recovered_paise),
        cost=Money(cost_paise),
        outcome_kind=outcome_kind,
        reason_code=reason_code,
        contact_events=contact_events,
    )


# --- arm_statistics / amount-weighted recovery rate -------------------------


def test_arm_statistics_is_amount_weighted_not_count_weighted() -> None:
    """One large recovered case and nine small unrecovered ones must not
    read the same as the reverse -- METRICS-AND-KPIS SS1's whole point."""
    cases = [
        _case(Arm.TREATMENT, at_risk_paise=900_000, recovered_paise=900_000),
        *[_case(Arm.TREATMENT, at_risk_paise=1_000, recovered_paise=0) for _ in range(9)],
    ]
    stats = arm_statistics(cases, Arm.TREATMENT)
    assert stats.case_count == 10
    assert stats.at_risk_total == Money(909_000)
    assert stats.recovered_total == Money(900_000)
    assert stats.recovery_rate == pytest.approx(900_000 / 909_000)


def test_arm_statistics_ignores_other_arms() -> None:
    cases = [
        _case(Arm.TREATMENT, at_risk_paise=1000, recovered_paise=1000),
        _case(Arm.CONTROL, at_risk_paise=1000, recovered_paise=0),
    ]
    stats = arm_statistics(cases, Arm.CONTROL)
    assert stats.case_count == 1
    assert stats.recovery_rate == 0.0


def test_arm_statistics_recovery_rate_is_zero_when_arm_has_zero_at_risk() -> None:
    stats = arm_statistics([], Arm.CONTROL)
    assert stats.case_count == 0
    assert stats.recovery_rate == 0.0


# --- compare_arms: incremental rate/value, CI, bootstrap ---------------------


def test_compare_arms_incremental_rate_matches_the_metrics_doc_formula() -> None:
    treatment = [_case(Arm.TREATMENT, at_risk_paise=100_000, recovered_paise=40_000)]
    control = [_case(Arm.CONTROL, at_risk_paise=100_000, recovered_paise=10_000)]
    result = compare_arms(
        treatment + control,
        treatment=Arm.TREATMENT,
        comparison=Arm.CONTROL,
        label="t_vs_c",
        rng_seed=1,
        bootstrap_resamples=200,
    )
    assert result.incremental_rate == pytest.approx(0.4 - 0.1)
    # incremental_value = incremental_rate * at_risk(all arms)
    assert result.incremental_value == Money(round(0.3 * 200_000))


def test_compare_arms_ci_crosses_zero_when_arms_are_indistinguishable() -> None:
    treatment = [_case(Arm.TREATMENT, at_risk_paise=10_000, recovered_paise=3_000)]
    control = [_case(Arm.CONTROL, at_risk_paise=10_000, recovered_paise=3_000)]
    result = compare_arms(
        treatment + control,
        treatment=Arm.TREATMENT,
        comparison=Arm.CONTROL,
        label="t_vs_c",
        rng_seed=1,
        bootstrap_resamples=200,
    )
    assert result.incremental_rate == 0.0
    assert result.ci_crosses_zero is True
    assert result.ci_low <= 0.0 <= result.ci_high


def test_compare_arms_ci_excludes_zero_for_a_large_clear_effect() -> None:
    treatment = [
        _case(Arm.TREATMENT, at_risk_paise=10_000, recovered_paise=9_000) for _ in range(200)
    ]
    control = [_case(Arm.CONTROL, at_risk_paise=10_000, recovered_paise=0) for _ in range(200)]
    result = compare_arms(
        treatment + control,
        treatment=Arm.TREATMENT,
        comparison=Arm.CONTROL,
        label="t_vs_c",
        rng_seed=1,
        bootstrap_resamples=500,
    )
    assert result.ci_crosses_zero is False
    assert result.ci_low > 0.0


def test_compare_arms_bootstrap_is_deterministic_for_the_same_seed() -> None:
    cases = [
        _case(Arm.TREATMENT, at_risk_paise=10_000, recovered_paise=4_000),
        _case(Arm.TREATMENT, at_risk_paise=20_000, recovered_paise=0),
        _case(Arm.CONTROL, at_risk_paise=10_000, recovered_paise=1_000),
        _case(Arm.CONTROL, at_risk_paise=20_000, recovered_paise=2_000),
    ]
    first = compare_arms(
        cases,
        treatment=Arm.TREATMENT,
        comparison=Arm.CONTROL,
        label="t_vs_c",
        rng_seed=42,
        bootstrap_resamples=500,
    )
    second = compare_arms(
        cases,
        treatment=Arm.TREATMENT,
        comparison=Arm.CONTROL,
        label="t_vs_c",
        rng_seed=42,
        bootstrap_resamples=500,
    )
    assert first.bootstrap_ci_low == second.bootstrap_ci_low
    assert first.bootstrap_ci_high == second.bootstrap_ci_high


def test_compare_arms_raises_when_an_arm_has_no_cases() -> None:
    cases = [_case(Arm.TREATMENT, at_risk_paise=1000, recovered_paise=0)]
    with pytest.raises(ValueError, match="at least one case"):
        compare_arms(
            cases, treatment=Arm.TREATMENT, comparison=Arm.CONTROL, label="t_vs_c", rng_seed=1
        )


# --- economics ---------------------------------------------------------------


def test_economics_computes_cost_per_rupee_net_value_and_roi() -> None:
    cases = [
        _case(Arm.TREATMENT, at_risk_paise=100_000, recovered_paise=50_000, cost_paise=1_000),
        _case(Arm.CONTROL, at_risk_paise=100_000, recovered_paise=10_000, cost_paise=0),
    ]
    result = economics(cases, incremental_value=Money(40_000))
    assert result.total_cost == Money(1_000)
    assert result.cost_per_rupee_recovered == pytest.approx(1_000 / 40_000)
    assert result.net_incremental_value == Money(39_000)
    assert result.roi == pytest.approx(39_000 / 1_000)
    assert result.mandate_budget_efficiency is None  # not measurable this phase


def test_economics_cost_per_rupee_is_none_when_incremental_value_is_not_positive() -> None:
    cases = [_case(Arm.TREATMENT, at_risk_paise=1000, recovered_paise=0, cost_paise=500)]
    result = economics(cases, incremental_value=Money(0))
    assert result.cost_per_rupee_recovered is None


def test_economics_roi_is_none_when_nothing_was_spent() -> None:
    cases = [_case(Arm.TREATMENT, at_risk_paise=1000, recovered_paise=1000, cost_paise=0)]
    result = economics(cases, incremental_value=Money(1000))
    assert result.roi is None


# --- guardrails ---------------------------------------------------------------


def test_guardrails_contact_fatigue_counts_only_contacted_customers() -> None:
    contacted = _case(
        Arm.BASELINE,
        at_risk_paise=1000,
        contact_events=(_T0, _T0 + timedelta(hours=1)),
    )
    not_contacted = _case(Arm.CONTROL, at_risk_paise=1000)  # no contact_events at all
    result = guardrails([contacted, not_contacted])
    assert result.contact_fatigue_index == 2.0  # both events fall in one 7-day window


def test_guardrails_contact_fatigue_index_is_zero_when_nobody_was_contacted() -> None:
    result = guardrails([_case(Arm.CONTROL, at_risk_paise=1000)])
    assert result.contact_fatigue_index == 0.0


def test_guardrails_contact_fatigue_uses_the_busiest_7_day_window() -> None:
    """Contacts 10 days apart must not average into one window --
    fatigue is about how many contacts land close together, not the
    total spread over the case's whole lifetime."""
    case = _case(
        Arm.BASELINE,
        at_risk_paise=1000,
        contact_events=(_T0, _T0 + timedelta(days=1), _T0 + timedelta(days=10)),
    )
    result = guardrails([case])
    assert result.contact_fatigue_index == 2.0  # the first two, not all three


def test_guardrails_opt_out_rate_is_a_fraction_of_contacted_customers() -> None:
    cases = [
        _case(Arm.BASELINE, at_risk_paise=1000, contact_events=(_T0,), reason_code=None),
        _case(
            Arm.BASELINE,
            at_risk_paise=1000,
            contact_events=(_T0,),
            reason_code="customer_opt_out",
        ),
        _case(Arm.CONTROL, at_risk_paise=1000),  # never contacted -- excluded from denominator
    ]
    result = guardrails(cases)
    assert result.opt_out_rate == pytest.approx(0.5)


def test_guardrails_quiet_hour_violations_is_not_measurable_this_phase() -> None:
    result = guardrails([_case(Arm.TREATMENT, at_risk_paise=1000, contact_events=(_T0,))])
    assert result.quiet_hour_violations is None


# --- compute_statistics: the whole assembly -----------------------------------


def test_compute_statistics_assembles_all_three_comparisons_and_one_per_arm() -> None:
    cases = [
        _case(Arm.TREATMENT, at_risk_paise=100_000, recovered_paise=40_000, cost_paise=200),
        _case(Arm.BASELINE, at_risk_paise=100_000, recovered_paise=20_000, cost_paise=100),
        _case(Arm.CONTROL, at_risk_paise=100_000, recovered_paise=10_000),
    ]
    stats = compute_statistics(cases, rng_seed=7, bootstrap_resamples=100)

    assert set(stats.per_arm) == set(Arm)
    assert stats.treatment_vs_control.incremental_rate == pytest.approx(0.4 - 0.1)
    assert stats.treatment_vs_baseline.incremental_rate == pytest.approx(0.4 - 0.2)
    assert stats.baseline_vs_control.incremental_rate == pytest.approx(0.2 - 0.1)
    # economics is keyed off the headline (treatment vs control), not re-derived
    assert stats.economics.total_cost == Money(300)
