"""The benchmark report writer (T3.7; PHASE-03-measurement.md,
METRICS-AND-KPIS.md SS8): assembles a `BenchmarkReport` from a completed
run's own data and renders it as Markdown and JSON to
`bench/reports/<seed>-<timestamp>/`.

Same split as `bench.statistics`: pure assembly/rendering functions
(`build_report`, `render_markdown`, `render_json`) that take already-
loaded data and never touch a session, plus one thin repository/glue
function (`write_report`) that loads everything from a real run and
calls them. `build_report` itself calls `bench.statistics.
compute_statistics` rather than duplicating it -- this module's whole
job is the sections T3.6 does not cover (run metadata, validity, the
exception list, policy denials, per-playbook breakdown) plus rendering
all of it, in the fixed order METRICS-AND-KPIS SS8 mandates.

Diagnosis quality (SS8 section 6, SS5) is a constant, not a query: this
phase's diagnosis is `diagnosis.engine.stub_diagnose`, which returns the
decline category it was *given* as its one hypothesis at full
confidence -- the category comes from `CohortGroundTruth.
decline_category` in the first place (`bench.runner._handle_case_
arrival`), so top-1/top-3 accuracy is 100% and abstention is 0% for
every cohort case, by construction, not by measurement. Reporting that
as if it validated a diagnosis engine would be exactly the kind of
invented number METRICS-AND-KPIS' own doctrine forbids; the report
states the caveat plainly instead. The SS5.1 LLM-ablation table is
"not applicable this phase" for the same reason -- no statistical
significance ranking and no LLM exist yet (both P5 scope).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from recoup.bench.baseline import load_baseline_playbook
from recoup.bench.statistics import (
    BenchmarkStatistics,
    CaseOutcome,
    IncrementalComparison,
    compute_statistics,
    load_case_outcomes,
)
from recoup.domain.case import DEFAULT_ARM_WEIGHTS, Arm
from recoup.domain.money import Money
from recoup.domain.outcome import OutcomeKind
from recoup.planning.playbooks.loader import load_playbooks
from recoup.platform.models import ActionRow, BenchRun, CaseRow, PolicyDecisionRow

__all__ = [
    "BenchmarkReport",
    "DiagnosisQuality",
    "ExceptionEntry",
    "PlaybookBreakdown",
    "RunMetadata",
    "ValidityStatement",
    "build_report",
    "render_json",
    "render_markdown",
    "write_report",
]

# METRICS-AND-KPIS.md SS3.1's own cost table has no version field of its
# own -- this is the version of *that table* the playbooks in this repo
# are meant to match (T3.6 already fixed one drift: the baseline
# playbook's email step against an invented rate). Bump this string in
# the same PR as any change to SS3.1's figures.
_COST_TABLE_VERSION = "METRICS-AND-KPIS.md SS3.1 (2026-09 rates)"
# No live gateway is wired yet -- every bench run uses RazorpaySimulator.
_GATEWAY_MODE = "simulated"
# METRICS-AND-KPIS SS8 and PHASE-00's own `.gitignore` entry both fix
# this at the *repo* root's `bench/reports/`, not `services/core`'s --
# `make bench` (root Makefile) `cd`s into `services/core` before running
# the CLI, so a plain relative `Path("bench/reports")` would land inside
# the service directory instead of the repo-root path `.gitignore`
# already carves out. Anchored on this file's own location instead of
# the process CWD so it lands in the right place regardless of where
# the command was invoked from.
_DEFAULT_REPORTS_ROOT = Path(__file__).resolve().parents[5] / "bench" / "reports"


@dataclass(frozen=True, slots=True)
class RunMetadata:
    run_id: uuid.UUID
    seed: int
    cohort_size: int
    started_at: datetime
    completed_at: datetime
    arm_weights: Mapping[str, float]
    git_sha: str
    cohort_config_hash: str
    simulator_config_hash: str
    playbook_versions: Mapping[str, int]
    cost_table_version: str
    gateway_mode: str


@dataclass(frozen=True, slots=True)
class ValidityStatement:
    headline_ci_crosses_zero: bool
    per_arm_case_counts: Mapping[str, int]
    baseline_beats_control: bool  # A3.7's own sanity check on the simulator, not the product
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiagnosisQuality:
    top1_accuracy: float
    top3_accuracy: float
    calibration_error: float
    abstention_rate: float
    note: str


@dataclass(frozen=True, slots=True)
class ExceptionEntry:
    case_id: uuid.UUID
    arm: str
    case_state: str
    outcome_kind: str | None
    reason: str
    at_risk: Money
    recovered: Money


@dataclass(frozen=True, slots=True)
class PlaybookBreakdown:
    playbook_id: str
    case_count: int
    recovery_rate: float
    total_cost: Money
    total_recovered: Money


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    metadata: RunMetadata
    validity: ValidityStatement
    statistics: BenchmarkStatistics
    diagnosis: DiagnosisQuality
    exceptions: tuple[ExceptionEntry, ...]
    policy_denials: Mapping[str, int]
    playbook_breakdown: tuple[PlaybookBreakdown, ...]


def _diagnosis_quality() -> DiagnosisQuality:
    return DiagnosisQuality(
        top1_accuracy=1.0,
        top3_accuracy=1.0,
        calibration_error=0.0,
        abstention_rate=0.0,
        note=(
            "Not a measurement of diagnostic skill: this phase's diagnosis "
            "(diagnosis.engine.stub_diagnose) returns the decline category "
            "it was given as its one hypothesis at full confidence, and "
            "that category is the cohort's own ground truth in the first "
            "place. These numbers are a tautology of the stub's "
            "definition, reported here rather than omitted so the section "
            "is never silently missing. Real statistical/LLM diagnosis is "
            "P5 scope; the SS5.1 ablation table (statistics-only vs. "
            "statistics+LLM) is not applicable until it exists."
        ),
    )


def _build_validity_statement(stats: BenchmarkStatistics) -> ValidityStatement:
    per_arm_counts = {arm.value: stats.per_arm[arm].case_count for arm in Arm}
    baseline_beats_control = stats.baseline_vs_control.incremental_value.paise > 0
    warnings: list[str] = []
    if not baseline_beats_control:
        warnings.append(
            "A3.7 sanity check failed: baseline does not beat control "
            "(incremental value <= 0). If a fixed retry schedule recovers "
            "nothing above doing nothing, the simulated world does not "
            "resemble payments and the numbers below do not mean what "
            "they appear to."
        )
    if stats.treatment_vs_control.ci_crosses_zero:
        warnings.append(
            "The headline (treatment vs. control) 95% CI crosses zero: "
            "the data cannot rule out zero incremental effect at this "
            "sample size."
        )
    return ValidityStatement(
        headline_ci_crosses_zero=stats.treatment_vs_control.ci_crosses_zero,
        per_arm_case_counts=per_arm_counts,
        baseline_beats_control=baseline_beats_control,
        warnings=tuple(warnings),
    )


def _exception_reason(case: CaseOutcome) -> str:
    if case.outcome_kind is None:
        return (
            f"no terminal outcome recorded -- case was still in state "
            f"{case.case_state!r} when the run ended (case-expiry/"
            f"stopping rules are PHASE-04 scope, POLICY-ENGINE R2)"
        )
    if case.reason_code is not None:
        return case.reason_code
    # PARTIALLY_RECOVERED carries no reason_code (DOMAIN-MODEL SS9: the
    # invariant is scoped to non-recovery kinds) -- explain it in report
    # prose instead of leaving the reason blank.
    return f"{case.outcome_kind.value}, no reason_code recorded"


def _build_exception_list(cases: Sequence[CaseOutcome]) -> tuple[ExceptionEntry, ...]:
    exceptions = [
        ExceptionEntry(
            case_id=case.case_id,
            arm=case.arm.value,
            case_state=case.case_state,
            outcome_kind=case.outcome_kind.value if case.outcome_kind else None,
            reason=_exception_reason(case),
            at_risk=case.at_risk,
            recovered=case.recovered,
        )
        for case in cases
        if case.outcome_kind is not OutcomeKind.RECOVERED
    ]
    # Deterministic order (A3.2's spirit extends to report content, not
    # just the statistics): by case_id, not insertion order, since
    # `cases` itself arrives in whatever order the DB happened to return
    # rows in.
    return tuple(sorted(exceptions, key=lambda e: str(e.case_id)))


def _build_playbook_breakdown(cases: Sequence[CaseOutcome]) -> tuple[PlaybookBreakdown, ...]:
    by_playbook: dict[str, list[CaseOutcome]] = {}
    for case in cases:
        key = case.playbook_id or "(none -- control arm or unmatched diagnosis)"
        by_playbook.setdefault(key, []).append(case)

    breakdown: list[PlaybookBreakdown] = []
    for playbook_id, group in by_playbook.items():
        at_risk_total = sum((c.at_risk.paise for c in group), 0)
        recovered_total = sum((c.recovered.paise for c in group), 0)
        recovery_rate = recovered_total / at_risk_total if at_risk_total else 0.0
        breakdown.append(
            PlaybookBreakdown(
                playbook_id=playbook_id,
                case_count=len(group),
                recovery_rate=recovery_rate,
                total_cost=Money(sum((c.cost.paise for c in group), 0)),
                total_recovered=Money(recovered_total),
            )
        )
    return tuple(sorted(breakdown, key=lambda b: b.playbook_id))


async def _load_policy_denials(session: AsyncSession, run_id: uuid.UUID) -> dict[str, int]:
    """Grouped by rule (METRICS-AND-KPIS SS8 section 8) -- joined through
    `ActionRow` for this run's own cases, the same join `bench.
    statistics.load_case_outcomes` uses for cost, since `PolicyDecisionRow`
    itself carries no `bench_run_id`."""
    rows = (
        await session.execute(
            select(PolicyDecisionRow.rule_id, PolicyDecisionRow.verdict)
            .join(ActionRow, ActionRow.id == PolicyDecisionRow.action_id)
            .join(CaseRow, CaseRow.id == ActionRow.case_id)
            .where(CaseRow.bench_run_id == run_id, PolicyDecisionRow.verdict != "allow")
        )
    ).all()
    counts: dict[str, int] = {}
    for rule_id, _verdict in rows:
        key = rule_id or "(unknown rule)"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _git_sha() -> str:
    """Best-effort, never fatal: a report is still worth writing from a
    source tree with no `.git` (a built image, an extracted archive)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 -- fixed argv, no shell, no user input
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _resource_hash(package: str, filename: str) -> str:
    raw = resources.files(package).joinpath(filename).read_text("utf-8")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_report(
    *,
    metadata: RunMetadata,
    cases: Sequence[CaseOutcome],
    policy_denials: Mapping[str, int],
    rng_seed: int,
    bootstrap_resamples: int = 10_000,
) -> BenchmarkReport:
    stats = compute_statistics(cases, rng_seed=rng_seed, bootstrap_resamples=bootstrap_resamples)
    return BenchmarkReport(
        metadata=metadata,
        validity=_build_validity_statement(stats),
        statistics=stats,
        diagnosis=_diagnosis_quality(),
        exceptions=_build_exception_list(cases),
        policy_denials=dict(policy_denials),
        playbook_breakdown=_build_playbook_breakdown(cases),
    )


def _format_money(money: Money) -> str:
    sign = "-" if money.paise < 0 else ""
    return f"{sign}₹{abs(money.paise) / 100:,.2f}"


def _format_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _render_comparison(title: str, stats: BenchmarkStatistics, key: str) -> list[str]:
    c: IncrementalComparison = getattr(stats, key)
    lines = [
        f"### {title}",
        "",
        f"- Incremental rate: {_format_pct(c.incremental_rate)}",
        f"- Incremental value: {_format_money(c.incremental_value)}",
        f"- 95% CI: [{_format_pct(c.ci_low)}, {_format_pct(c.ci_high)}] "
        f"({'crosses zero' if c.ci_crosses_zero else 'excludes zero'})",
        f"- Bootstrap 95% CI ({10_000} resamples): "
        f"[{_format_pct(c.bootstrap_ci_low)}, {_format_pct(c.bootstrap_ci_high)}]",
        "",
    ]
    return lines


def render_markdown(report: BenchmarkReport) -> str:
    m = report.metadata
    v = report.validity
    s = report.statistics
    d = report.diagnosis
    lines: list[str] = []

    # 1. Run metadata
    lines += [
        f"# Benchmark report -- seed {m.seed}",
        "",
        "## 1. Run metadata",
        "",
        f"- Run id: `{m.run_id}`",
        f"- Seed: {m.seed}",
        f"- Cohort size: {m.cohort_size}",
        f"- Started: {m.started_at.isoformat()}",
        f"- Completed: {m.completed_at.isoformat()}",
        f"- Arm split: {', '.join(f'{arm}={weight:.0%}' for arm, weight in m.arm_weights.items())}",
        f"- Gateway mode: {m.gateway_mode}",
        f"- Git SHA: `{m.git_sha}`",
        f"- Cohort config hash: `{m.cohort_config_hash}`",
        f"- Simulator config hash: `{m.simulator_config_hash}`",
        f"- Cost table version: {m.cost_table_version}",
        "- Playbook versions: "
        + ", ".join(f"{pid}=v{ver}" for pid, ver in sorted(m.playbook_versions.items())),
        "",
    ]

    # 2. Validity statement -- before the headline, deliberately
    lines += [
        "## 2. Validity statement",
        "",
        f"- Headline CI crosses zero: **{v.headline_ci_crosses_zero}**",
        f"- Baseline beats control (A3.7 sanity check): **{v.baseline_beats_control}**",
        "- Case counts per arm: "
        + ", ".join(f"{arm}={count}" for arm, count in sorted(v.per_arm_case_counts.items())),
    ]
    if v.warnings:
        lines.append("")
        for warning in v.warnings:
            lines.append(f"> **Warning:** {warning}")
    lines.append("")

    # 3. Headline
    lines += ["## 3. Headline", ""]
    lines += _render_comparison("Treatment vs. control", s, "treatment_vs_control")
    lines += _render_comparison("Treatment vs. baseline", s, "treatment_vs_baseline")
    lines += _render_comparison("Baseline vs. control", s, "baseline_vs_control")
    lines += ["### Per-arm recovery rate", ""]
    for arm in Arm:
        a = s.per_arm[arm]
        lines.append(
            f"- {arm.value}: {_format_pct(a.recovery_rate)} recovered "
            f"({_format_money(a.recovered_total)} / {_format_money(a.at_risk_total)}, "
            f"{a.case_count} cases)"
        )
    lines.append("")

    # 4. Economics
    e = s.economics
    lines += [
        "## 4. Economics",
        "",
        f"- Total cost: {_format_money(e.total_cost)}",
        "- Cost per rupee recovered: "
        + (
            f"{e.cost_per_rupee_recovered:.4f}" if e.cost_per_rupee_recovered is not None else "n/a"
        ),
        f"- Net incremental value: {_format_money(e.net_incremental_value)}",
        "- ROI: " + (f"{e.roi:.2%}" if e.roi is not None else "n/a"),
        "- Mandate budget efficiency: not measurable this phase (no playbook step calls "
        "present_mandate yet -- POLICY-ENGINE R5, PHASE-04 scope)",
        "",
    ]

    # 5. Guardrails
    g = s.guardrails
    lines += [
        "## 5. Guardrails",
        "",
        f"- Contact fatigue index: {g.contact_fatigue_index:.2f} (guardrail: <= 2.0)",
        f"- Opt-out rate: {_format_pct(g.opt_out_rate)} (guardrail: <= 1.5%)",
        "- Quiet-hour violations: not measurable this phase (no quiet-hours rule wired yet -- "
        "POLICY-ENGINE R3, PHASE-04 scope; not asserted as zero)",
        "",
    ]

    # 6. Diagnosis quality
    lines += [
        "## 6. Diagnosis quality",
        "",
        f"- Top-1 accuracy: {_format_pct(d.top1_accuracy)}",
        f"- Top-3 accuracy: {_format_pct(d.top3_accuracy)}",
        f"- Calibration error (ECE): {d.calibration_error:.4f}",
        f"- Abstention rate: {_format_pct(d.abstention_rate)}",
        "",
        f"> {d.note}",
        "",
    ]

    # 7. Exception list -- never truncated
    lines += [
        "## 7. Exception list",
        "",
        f"{len(report.exceptions)} case(s) did not resolve to a full recovery. "
        "Every one is listed below, in full.",
        "",
    ]
    if report.exceptions:
        lines.append("| Case | Arm | State | Outcome | At risk | Recovered | Reason |")
        lines.append("|---|---|---|---|---|---|---|")
        for ex in report.exceptions:
            lines.append(
                f"| `{ex.case_id}` | {ex.arm} | {ex.case_state} | {ex.outcome_kind or '-'} "
                f"| {_format_money(ex.at_risk)} | {_format_money(ex.recovered)} | {ex.reason} |"
            )
    lines.append("")

    # 8. Policy denials grouped by rule
    lines += ["## 8. Policy denials", ""]
    if report.policy_denials:
        lines.append("| Rule | Denials |")
        lines.append("|---|---|")
        for rule_id, count in sorted(report.policy_denials.items()):
            lines.append(f"| {rule_id} | {count} |")
    else:
        lines.append("No denials recorded this run.")
    lines.append("")

    # 9. Per-playbook breakdown
    lines += ["## 9. Per-playbook breakdown", ""]
    lines.append("| Playbook | Cases | Recovery rate | Total cost | Total recovered |")
    lines.append("|---|---|---|---|---|")
    for pb in report.playbook_breakdown:
        lines.append(
            f"| {pb.playbook_id} | {pb.case_count} | {_format_pct(pb.recovery_rate)} "
            f"| {_format_money(pb.total_cost)} | {_format_money(pb.total_recovered)} |"
        )
    lines.append("")

    return "\n".join(lines)


def render_json(report: BenchmarkReport) -> dict[str, object]:
    m = report.metadata
    s = report.statistics

    def comparison_dict(c: IncrementalComparison) -> dict[str, object]:
        return {
            "incremental_rate": c.incremental_rate,
            "incremental_value": c.incremental_value.to_dict(),
            "ci_low": c.ci_low,
            "ci_high": c.ci_high,
            "ci_crosses_zero": c.ci_crosses_zero,
            "bootstrap_ci_low": c.bootstrap_ci_low,
            "bootstrap_ci_high": c.bootstrap_ci_high,
        }

    return {
        "metadata": {
            "run_id": str(m.run_id),
            "seed": m.seed,
            "cohort_size": m.cohort_size,
            "started_at": m.started_at.isoformat(),
            "completed_at": m.completed_at.isoformat(),
            "arm_weights": dict(m.arm_weights),
            "gateway_mode": m.gateway_mode,
            "git_sha": m.git_sha,
            "cohort_config_hash": m.cohort_config_hash,
            "simulator_config_hash": m.simulator_config_hash,
            "cost_table_version": m.cost_table_version,
            "playbook_versions": dict(m.playbook_versions),
        },
        "validity": {
            "headline_ci_crosses_zero": report.validity.headline_ci_crosses_zero,
            "per_arm_case_counts": dict(report.validity.per_arm_case_counts),
            "baseline_beats_control": report.validity.baseline_beats_control,
            "warnings": list(report.validity.warnings),
        },
        "statistics": {
            "per_arm": {
                arm.value: {
                    "case_count": s.per_arm[arm].case_count,
                    "at_risk_total": s.per_arm[arm].at_risk_total.to_dict(),
                    "recovered_total": s.per_arm[arm].recovered_total.to_dict(),
                    "recovery_rate": s.per_arm[arm].recovery_rate,
                }
                for arm in Arm
            },
            "treatment_vs_control": comparison_dict(s.treatment_vs_control),
            "treatment_vs_baseline": comparison_dict(s.treatment_vs_baseline),
            "baseline_vs_control": comparison_dict(s.baseline_vs_control),
            "economics": {
                "total_cost": s.economics.total_cost.to_dict(),
                "cost_per_rupee_recovered": s.economics.cost_per_rupee_recovered,
                "net_incremental_value": s.economics.net_incremental_value.to_dict(),
                "roi": s.economics.roi,
                "mandate_budget_efficiency": s.economics.mandate_budget_efficiency,
            },
            "guardrails": {
                "contact_fatigue_index": s.guardrails.contact_fatigue_index,
                "opt_out_rate": s.guardrails.opt_out_rate,
                "quiet_hour_violations": s.guardrails.quiet_hour_violations,
            },
        },
        "diagnosis": {
            "top1_accuracy": report.diagnosis.top1_accuracy,
            "top3_accuracy": report.diagnosis.top3_accuracy,
            "calibration_error": report.diagnosis.calibration_error,
            "abstention_rate": report.diagnosis.abstention_rate,
            "note": report.diagnosis.note,
        },
        "exceptions": [
            {
                "case_id": str(ex.case_id),
                "arm": ex.arm,
                "case_state": ex.case_state,
                "outcome_kind": ex.outcome_kind,
                "reason": ex.reason,
                "at_risk": ex.at_risk.to_dict(),
                "recovered": ex.recovered.to_dict(),
            }
            for ex in report.exceptions
        ],
        "policy_denials": dict(report.policy_denials),
        "playbook_breakdown": [
            {
                "playbook_id": pb.playbook_id,
                "case_count": pb.case_count,
                "recovery_rate": pb.recovery_rate,
                "total_cost": pb.total_cost.to_dict(),
                "total_recovered": pb.total_recovered.to_dict(),
            }
            for pb in report.playbook_breakdown
        ],
    }


async def write_report(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    out_root: Path = _DEFAULT_REPORTS_ROOT,
) -> Path:
    """Loads everything `build_report` needs for an already-completed
    run and writes `report.md`/`report.json` to
    `bench/reports/<seed>-<timestamp>/`. `<timestamp>` is the run's own
    `completed_at` -- the simulated clock's final position, not a
    wall-clock read -- so the output *path* is as reproducible as the
    report's own content (A3.2's spirit).
    """
    async with sessionmaker() as session:
        run = await session.get(BenchRun, run_id)
        if run is None:
            raise ValueError(f"no bench run {run_id}")
        if run.completed_at is None:
            raise ValueError(f"bench run {run_id} has not completed yet")
        cases = await load_case_outcomes(session, run_id)
        policy_denials = await _load_policy_denials(session, run_id)

    playbooks = load_playbooks()
    baseline = load_baseline_playbook()
    playbook_versions = {p.id: p.version for p in playbooks.values()}
    playbook_versions[baseline.id] = baseline.version

    raw_size = run.config.get("size")
    cohort_size = raw_size if isinstance(raw_size, int) else len(cases)

    metadata = RunMetadata(
        run_id=run_id,
        seed=run.seed,
        cohort_size=cohort_size,
        started_at=run.started_at,
        completed_at=run.completed_at,
        arm_weights={arm.value: weight for arm, weight in DEFAULT_ARM_WEIGHTS.items()},
        git_sha=_git_sha(),
        cohort_config_hash=_resource_hash("recoup.bench", "cohort.yaml"),
        simulator_config_hash=_resource_hash("recoup.gateway.simulator", "simulator.yaml"),
        playbook_versions=playbook_versions,
        cost_table_version=_COST_TABLE_VERSION,
        gateway_mode=_GATEWAY_MODE,
    )

    report = build_report(
        metadata=metadata, cases=cases, policy_denials=policy_denials, rng_seed=run.seed
    )

    out_dir = out_root / f"{run.seed}-{run.completed_at.strftime('%Y%m%dT%H%M%SZ')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(render_json(report), indent=2), encoding="utf-8"
    )
    return out_dir
