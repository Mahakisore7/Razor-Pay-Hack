"""Unit tests for `recoup.bench.report` (T3.7): pure assembly and
rendering functions over hand-built `CaseOutcome` fixtures -- no
database required. `write_report` (the repository/glue half) is
exercised against a real run in `tests/integration/test_bench_report.py`.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import UTC, datetime

import pytest

from recoup.bench.report import (
    RunMetadata,
    _build_exception_list,
    _build_playbook_breakdown,
    _build_validity_statement,
    _diagnosis_quality,
    _exception_reason,
    _git_sha,
    build_report,
    render_json,
    render_markdown,
)
from recoup.bench.statistics import CaseOutcome, compute_statistics
from recoup.domain.case import Arm
from recoup.domain.identifiers import CaseId, uuid7
from recoup.domain.money import Money
from recoup.domain.outcome import OutcomeKind

_T0 = datetime(2026, 4, 10, tzinfo=UTC)


def _case(
    arm: Arm,
    *,
    at_risk_paise: int = 100_000,
    recovered_paise: int = 0,
    cost_paise: int = 0,
    outcome_kind: OutcomeKind | None = None,
    reason_code: str | None = None,
    case_state: str = "awaiting_outcome",
    playbook_id: str | None = None,
) -> CaseOutcome:
    return CaseOutcome(
        case_id=CaseId(uuid7()),
        arm=arm,
        at_risk=Money(at_risk_paise),
        recovered=Money(recovered_paise),
        cost=Money(cost_paise),
        outcome_kind=outcome_kind,
        reason_code=reason_code,
        contact_events=(),
        case_state=case_state,
        playbook_id=playbook_id,
    )


def _metadata(*, seed: int = 1) -> RunMetadata:
    return RunMetadata(
        run_id=uuid.uuid4(),
        seed=seed,
        cohort_size=3,
        started_at=_T0,
        completed_at=_T0,
        arm_weights={"control": 0.1, "baseline": 0.1, "treatment": 0.8},
        git_sha="deadbeef",
        cohort_config_hash="abc123",
        simulator_config_hash="def456",
        playbook_versions={"baseline-naive": 1, "l1-generic-retry": 1},
        cost_table_version="test-cost-table",
        gateway_mode="simulated",
    )


def _realistic_cases() -> list[CaseOutcome]:
    return [
        _case(
            Arm.TREATMENT,
            recovered_paise=100_000,
            cost_paise=200,
            outcome_kind=OutcomeKind.RECOVERED,
            case_state="recovered",
            playbook_id="l1-generic-retry",
        ),
        _case(
            Arm.TREATMENT,
            outcome_kind=OutcomeKind.LOST,
            reason_code="max_attempts_exhausted",
            case_state="lost",
            playbook_id="l1-generic-retry",
        ),
        _case(
            Arm.BASELINE,
            recovered_paise=20_000,
            cost_paise=2,
            outcome_kind=OutcomeKind.PARTIALLY_RECOVERED,
            case_state="partially_recovered",
            playbook_id="baseline-naive",
        ),
        _case(
            Arm.CONTROL,
            recovered_paise=10_000,
            outcome_kind=OutcomeKind.RECOVERED,
            case_state="recovered",
        ),
        _case(Arm.CONTROL, case_state="awaiting_outcome"),  # never resolved -- outcome_kind is None
    ]


# --- git SHA (best-effort, never fatal) ----------------------------------------


def test_git_sha_returns_unknown_rather_than_raising_when_git_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert _git_sha() == "unknown"


# --- diagnosis quality (a constant, by construction) --------------------------


def test_diagnosis_quality_is_trivially_perfect_and_says_so() -> None:
    d = _diagnosis_quality()
    assert d.top1_accuracy == 1.0
    assert d.top3_accuracy == 1.0
    assert d.calibration_error == 0.0
    assert d.abstention_rate == 0.0
    assert "stub_diagnose" in d.note
    assert "not applicable" in d.note or "P5" in d.note


# --- exception list -------------------------------------------------------------


def test_exception_reason_for_a_case_with_no_terminal_outcome_names_its_state() -> None:
    case = _case(Arm.TREATMENT, outcome_kind=None, case_state="executing")
    reason = _exception_reason(case)
    assert "executing" in reason
    assert "no terminal outcome" in reason


def test_exception_reason_uses_the_outcomes_own_reason_code_when_present() -> None:
    case = _case(Arm.TREATMENT, outcome_kind=OutcomeKind.LOST, reason_code="max_attempts_exhausted")
    assert _exception_reason(case) == "max_attempts_exhausted"


def test_exception_reason_explains_a_partial_recovery_with_no_reason_code() -> None:
    case = _case(Arm.BASELINE, outcome_kind=OutcomeKind.PARTIALLY_RECOVERED, reason_code=None)
    reason = _exception_reason(case)
    assert "partially_recovered" in reason


def test_exception_list_excludes_fully_recovered_cases() -> None:
    cases = _realistic_cases()
    exceptions = _build_exception_list(cases)
    assert all(ex.outcome_kind != OutcomeKind.RECOVERED.value for ex in exceptions)
    # 5 cases in the fixture, 2 are RECOVERED -- 3 belong in the exception list.
    assert len(exceptions) == 3


def test_exception_list_is_never_truncated() -> None:
    many = [
        _case(Arm.TREATMENT, outcome_kind=OutcomeKind.LOST, reason_code="x") for _ in range(500)
    ]
    assert len(_build_exception_list(many)) == 500


def test_exception_list_is_deterministically_ordered_by_case_id() -> None:
    cases = _realistic_cases()
    first = _build_exception_list(cases)
    second = _build_exception_list(list(reversed(cases)))
    assert [e.case_id for e in first] == [e.case_id for e in second]
    assert [str(e.case_id) for e in first] == sorted(str(e.case_id) for e in first)


# --- per-playbook breakdown ------------------------------------------------------


def test_playbook_breakdown_groups_by_playbook_and_computes_amount_weighted_rate() -> None:
    cases = [
        _case(Arm.TREATMENT, at_risk_paise=100_000, recovered_paise=50_000, playbook_id="pb-a"),
        _case(Arm.TREATMENT, at_risk_paise=100_000, recovered_paise=0, playbook_id="pb-a"),
        _case(Arm.BASELINE, at_risk_paise=100_000, recovered_paise=100_000, playbook_id="pb-b"),
    ]
    breakdown = {b.playbook_id: b for b in _build_playbook_breakdown(cases)}
    assert breakdown["pb-a"].case_count == 2
    assert breakdown["pb-a"].recovery_rate == pytest.approx(0.25)
    assert breakdown["pb-b"].case_count == 1
    assert breakdown["pb-b"].recovery_rate == pytest.approx(1.0)


def test_playbook_breakdown_labels_control_and_unmatched_cases_together() -> None:
    cases = [_case(Arm.CONTROL, playbook_id=None), _case(Arm.TREATMENT, playbook_id=None)]
    breakdown = _build_playbook_breakdown(cases)
    assert len(breakdown) == 1
    assert breakdown[0].case_count == 2
    assert "none" in breakdown[0].playbook_id.lower()


def test_playbook_breakdown_is_sorted_by_playbook_id() -> None:
    cases = [
        _case(Arm.TREATMENT, playbook_id="zzz"),
        _case(Arm.TREATMENT, playbook_id="aaa"),
    ]
    breakdown = _build_playbook_breakdown(cases)
    assert [b.playbook_id for b in breakdown] == ["aaa", "zzz"]


# --- validity statement -----------------------------------------------------------


def test_validity_statement_warns_when_baseline_does_not_beat_control() -> None:
    cases = [
        _case(Arm.TREATMENT, at_risk_paise=100_000, recovered_paise=10_000),
        _case(Arm.BASELINE, at_risk_paise=100_000, recovered_paise=0),
        _case(Arm.CONTROL, at_risk_paise=100_000, recovered_paise=10_000),
    ]
    stats = compute_statistics(cases, rng_seed=1, bootstrap_resamples=50)
    validity = _build_validity_statement(stats)
    assert validity.baseline_beats_control is False
    assert any("A3.7" in w for w in validity.warnings)


def test_validity_statement_is_clean_when_baseline_beats_control_and_ci_excludes_zero() -> None:
    cases = (
        [_case(Arm.TREATMENT, at_risk_paise=10_000, recovered_paise=9_000) for _ in range(50)]
        + [_case(Arm.BASELINE, at_risk_paise=10_000, recovered_paise=3_000) for _ in range(50)]
        + [_case(Arm.CONTROL, at_risk_paise=10_000, recovered_paise=0) for _ in range(50)]
    )
    stats = compute_statistics(cases, rng_seed=1, bootstrap_resamples=200)
    validity = _build_validity_statement(stats)
    assert validity.baseline_beats_control is True
    assert validity.warnings == ()


# --- build_report / render_markdown / render_json: full assembly -----------------


def test_build_report_assembles_every_section() -> None:
    report = build_report(
        metadata=_metadata(),
        cases=_realistic_cases(),
        policy_denials={"no_consent": 3, "cost_ceiling": 1},
        rng_seed=1,
        bootstrap_resamples=50,
    )
    assert report.metadata.seed == 1
    assert len(report.exceptions) == 3
    assert report.policy_denials == {"no_consent": 3, "cost_ceiling": 1}
    assert {b.playbook_id for b in report.playbook_breakdown} >= {
        "l1-generic-retry",
        "baseline-naive",
    }


def test_render_markdown_sections_appear_fixed_order_validity_before_headline() -> None:
    report = build_report(
        metadata=_metadata(),
        cases=_realistic_cases(),
        policy_denials={},
        rng_seed=1,
        bootstrap_resamples=50,
    )
    text = render_markdown(report)
    headers = [
        "## 1. Run metadata",
        "## 2. Validity statement",
        "## 3. Headline",
        "## 4. Economics",
        "## 5. Guardrails",
        "## 6. Diagnosis quality",
        "## 7. Exception list",
        "## 8. Policy denials",
        "## 9. Per-playbook breakdown",
    ]
    positions = [text.index(h) for h in headers]
    assert positions == sorted(positions)


def test_render_markdown_exception_list_is_never_truncated() -> None:
    cases = [
        _case(Arm.TREATMENT, outcome_kind=OutcomeKind.LOST, reason_code="x") for _ in range(50)
    ] + [_case(Arm.BASELINE, outcome_kind=OutcomeKind.LOST, reason_code="x"), _case(Arm.CONTROL)]
    report = build_report(
        metadata=_metadata(), cases=cases, policy_denials={}, rng_seed=1, bootstrap_resamples=50
    )
    text = render_markdown(report)
    # One table row per exception (50 treatment + 1 baseline + 1 control,
    # since none of these ever reach RECOVERED), plus header/separator rows.
    assert text.count("| `") == 52


def test_render_markdown_reports_no_denials_plainly() -> None:
    report = build_report(
        metadata=_metadata(),
        cases=_realistic_cases(),
        policy_denials={},
        rng_seed=1,
        bootstrap_resamples=50,
    )
    text = render_markdown(report)
    assert "No denials recorded this run." in text


def test_render_markdown_lists_denials_grouped_by_rule() -> None:
    report = build_report(
        metadata=_metadata(),
        cases=_realistic_cases(),
        policy_denials={"no_consent": 3, "cost_ceiling": 1},
        rng_seed=1,
        bootstrap_resamples=50,
    )
    text = render_markdown(report)
    assert "| no_consent | 3 |" in text
    assert "| cost_ceiling | 1 |" in text


def test_render_markdown_omits_the_exception_table_when_every_case_recovered() -> None:
    cases = [
        _case(Arm.TREATMENT, recovered_paise=100_000, outcome_kind=OutcomeKind.RECOVERED),
        _case(Arm.BASELINE, recovered_paise=100_000, outcome_kind=OutcomeKind.RECOVERED),
        _case(Arm.CONTROL, recovered_paise=100_000, outcome_kind=OutcomeKind.RECOVERED),
    ]
    report = build_report(
        metadata=_metadata(), cases=cases, policy_denials={}, rng_seed=1, bootstrap_resamples=50
    )
    assert report.exceptions == ()
    text = render_markdown(report)
    assert "0 case(s) did not resolve to a full recovery" in text
    assert "| Case | Arm | State |" not in text


def test_render_markdown_has_no_warnings_when_the_run_is_clean() -> None:
    cases = (
        [_case(Arm.TREATMENT, at_risk_paise=10_000, recovered_paise=9_000) for _ in range(50)]
        + [_case(Arm.BASELINE, at_risk_paise=10_000, recovered_paise=3_000) for _ in range(50)]
        + [_case(Arm.CONTROL, at_risk_paise=10_000, recovered_paise=0) for _ in range(50)]
    )
    report = build_report(
        metadata=_metadata(), cases=cases, policy_denials={}, rng_seed=1, bootstrap_resamples=200
    )
    assert report.validity.warnings == ()
    text = render_markdown(report)
    assert "**Warning:**" not in text


def test_render_json_round_trips_through_json_dumps() -> None:
    report = build_report(
        metadata=_metadata(),
        cases=_realistic_cases(),
        policy_denials={"no_consent": 1},
        rng_seed=1,
        bootstrap_resamples=50,
    )
    payload = render_json(report)
    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)
    assert reloaded["metadata"]["seed"] == 1
    assert reloaded["policy_denials"] == {"no_consent": 1}
    assert len(reloaded["exceptions"]) == 3
    assert reloaded["diagnosis"]["top1_accuracy"] == 1.0
    assert set(reloaded["statistics"]["per_arm"]) == {"control", "baseline", "treatment"}
