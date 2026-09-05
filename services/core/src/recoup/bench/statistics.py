"""The benchmark's headline numbers (T3.6; PHASE-03-measurement.md,
METRICS-AND-KPIS.md SS2-4): pure functions over an already-loaded
`Sequence[CaseOutcome]`, no session, no clock, no RNG beyond the one
explicitly seeded for the bootstrap cross-check -- the same "pure
computation, separate repository glue" split `attribution.matcher`/
`attribution.engine` and `planning.planner`/`planning.repository`
already use. Loading `CaseOutcome`s from a `bench_run_id` is a thin,
separate, session-taking function precisely so everything below it is
testable with hand-built fixtures, no database required.

Every formula here is copied from METRICS-AND-KPIS.md, not re-derived:
recovery rate is amount-weighted (SS2.2) because a plain count-weighted
rate lets one large recovered case and nine small unrecovered ones look
identical to the reverse. The 95% CI (SS2.3) treats each arm's *case
count* as its effective sample size `n` against the amount-weighted
proportion `p` -- METRICS-AND-KPIS names `n` as "effective sample size
per arm" without a more precise definition, and case count is the
simplest reading consistent with "effective" (not literally weighting
by rupees, which would double-count the very weighting `p` already
applies). The bootstrap resamples *cases*, not rupees, for the same
reason -- each resample redraws a candidate set of cases, then applies
the same amount-weighted formula.

Two metrics METRICS-AND-KPIS SS3.3/SS4 also name -- mandate budget
efficiency and quiet-hour violations -- are not computed here, on
purpose: nothing in this codebase yet consumes a mandate's
representation budget for real (every playbook's payment_retry step
calls `retry_payment`, never `present_mandate`; T4.1's R9 rule exists and
is fully tested, but no case anywhere is ever associated with a real
`Mandate` -- that needs a schema change `mandate_budget.py`'s own
docstring explains, not this phase's scope) and R6's quiet hours, while
real and wired into `evaluate` as of T4.1, is kept a deliberate no-op
against every playbook step shipped so far (`bench/runner.py`'s own
comment on why). Reporting an invented number for either would be worse
than reporting none -- both come back `None`, and T3.7's report must say
why plainly rather than omit them silently.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from recoup.domain.action import Channel
from recoup.domain.case import Arm
from recoup.domain.identifiers import CaseId
from recoup.domain.money import Currency, Money
from recoup.domain.outcome import OutcomeKind
from recoup.platform.models import ActionRow, CaseRow, OutcomeRow, ScheduledActionRow

__all__ = [
    "ArmStatistics",
    "BenchmarkStatistics",
    "CaseOutcome",
    "EconomicStatistics",
    "GuardrailStatistics",
    "IncrementalComparison",
    "compute_statistics",
    "load_case_outcomes",
]

_BOOTSTRAP_RESAMPLES = 10_000
_Z_95 = 1.96
_CONTACT_FATIGUE_WINDOW = timedelta(days=7)
# Channels that *contact* a customer -- retry/link are gateway-side
# operations with no message delivered to a person (RAZORPAY-INTEGRATION
# SS1; also why METRICS-AND-KPIS SS3.1's cost table has no line item for
# either), so they are excluded from the contact-fatigue count.
_CONTACT_CHANNELS = frozenset(
    {Channel.SMS, Channel.WHATSAPP, Channel.EMAIL, Channel.VOICE, Channel.HUMAN_REVIEW}
)


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case_id: CaseId
    arm: Arm
    at_risk: Money
    recovered: Money  # zero for an unresolved or non-recovery case
    cost: Money  # sum of every executed action's actual cost for this case
    outcome_kind: OutcomeKind | None  # None: still open when this was loaded
    reason_code: str | None
    contact_events: tuple[datetime, ...]  # occurred_at of each executed contact-channel action
    # Both default so every existing hand-built fixture (test_bench_
    # statistics.py's `_case` helper) keeps compiling unchanged --
    # `load_case_outcomes` always supplies real values for both. Added
    # for T3.7's report writer (exception list needs the state a case
    # was left in when it has no terminal Outcome; per-playbook
    # breakdown needs which playbook ran it), rather than a second,
    # near-duplicate query over the same `cases` rows this one already
    # loads.
    case_state: str = "unknown"
    playbook_id: str | None = None


@dataclass(frozen=True, slots=True)
class ArmStatistics:
    arm: Arm
    case_count: int
    at_risk_total: Money
    recovered_total: Money
    recovery_rate: float  # amount-weighted; 0.0 if at_risk_total is zero


@dataclass(frozen=True, slots=True)
class IncrementalComparison:
    label: str
    incremental_rate: float
    incremental_value: Money
    ci_low: float
    ci_high: float
    ci_crosses_zero: bool
    bootstrap_ci_low: float
    bootstrap_ci_high: float


@dataclass(frozen=True, slots=True)
class EconomicStatistics:
    total_cost: Money
    cost_per_rupee_recovered: (
        float | None
    )  # None if incremental_value <= 0 -- undefined, not infinite
    net_incremental_value: Money
    roi: float | None
    mandate_budget_efficiency: None  # not measurable this phase -- see module docstring


@dataclass(frozen=True, slots=True)
class GuardrailStatistics:
    contact_fatigue_index: float  # mean contacts per *contacted* customer per 7-day window
    opt_out_rate: float  # suppressed-for-opt-out / contacted; 0.0 this phase (R2 not yet wired)
    quiet_hour_violations: None  # not measurable this phase -- see module docstring


@dataclass(frozen=True, slots=True)
class BenchmarkStatistics:
    per_arm: Mapping[Arm, ArmStatistics]
    treatment_vs_control: IncrementalComparison
    treatment_vs_baseline: IncrementalComparison
    baseline_vs_control: IncrementalComparison
    economics: EconomicStatistics
    guardrails: GuardrailStatistics


def _amount_weighted_rate(cases: Sequence[CaseOutcome]) -> float:
    at_risk_total = sum((c.at_risk.paise for c in cases), 0)
    if at_risk_total == 0:
        return 0.0
    recovered_total = sum((c.recovered.paise for c in cases), 0)
    return recovered_total / at_risk_total


def arm_statistics(cases: Sequence[CaseOutcome], arm: Arm) -> ArmStatistics:
    arm_cases = [c for c in cases if c.arm is arm]
    at_risk_total = Money(sum((c.at_risk.paise for c in arm_cases), 0))
    recovered_total = Money(sum((c.recovered.paise for c in arm_cases), 0))
    return ArmStatistics(
        arm=arm,
        case_count=len(arm_cases),
        at_risk_total=at_risk_total,
        recovered_total=recovered_total,
        recovery_rate=_amount_weighted_rate(arm_cases),
    )


def _bootstrap_incremental_ci(
    treatment_cases: Sequence[CaseOutcome],
    comparison_cases: Sequence[CaseOutcome],
    *,
    rng: random.Random,
    resamples: int,
) -> tuple[float, float]:
    """Both sequences are already known non-empty -- `compare_arms` is
    this function's only caller, and it raises before ever reaching
    here otherwise."""
    diffs: list[float] = []
    for _ in range(resamples):
        t_sample = rng.choices(treatment_cases, k=len(treatment_cases))
        c_sample = rng.choices(comparison_cases, k=len(comparison_cases))
        diffs.append(_amount_weighted_rate(t_sample) - _amount_weighted_rate(c_sample))
    diffs.sort()
    low_index = round(0.025 * (len(diffs) - 1))
    high_index = round(0.975 * (len(diffs) - 1))
    return (diffs[low_index], diffs[high_index])


def compare_arms(
    all_cases: Sequence[CaseOutcome],
    *,
    treatment: Arm,
    comparison: Arm,
    label: str,
    rng_seed: int,
    bootstrap_resamples: int = _BOOTSTRAP_RESAMPLES,
) -> IncrementalComparison:
    """METRICS-AND-KPIS SS2.2/SS2.3. `rng_seed` makes the bootstrap
    cross-check reproducible for a given benchmark seed and case set --
    pass the run's own seed, not a fresh one, so re-running statistics
    over the same data is byte-identical (A3.2's spirit).
    """
    treatment_cases = [c for c in all_cases if c.arm is treatment]
    comparison_cases = [c for c in all_cases if c.arm is comparison]
    if not treatment_cases or not comparison_cases:
        raise ValueError(
            f"compare_arms({label!r}): both arms need at least one case "
            f"({len(treatment_cases)} {treatment.value}, "
            f"{len(comparison_cases)} {comparison.value})"
        )

    p_t = _amount_weighted_rate(treatment_cases)
    p_c = _amount_weighted_rate(comparison_cases)
    n_t = len(treatment_cases)
    n_c = len(comparison_cases)

    incremental_rate = p_t - p_c
    at_risk_all = Money(sum((c.at_risk.paise for c in all_cases), 0))
    incremental_value = Money(round(incremental_rate * at_risk_all.paise))

    se = ((p_t * (1 - p_t) / n_t) + (p_c * (1 - p_c) / n_c)) ** 0.5
    ci_low = incremental_rate - _Z_95 * se
    ci_high = incremental_rate + _Z_95 * se

    rng = random.Random(f"{rng_seed}|{label}")  # noqa: S311 -- statistical resampling, not crypto
    bootstrap_low, bootstrap_high = _bootstrap_incremental_ci(
        treatment_cases, comparison_cases, rng=rng, resamples=bootstrap_resamples
    )

    return IncrementalComparison(
        label=label,
        incremental_rate=incremental_rate,
        incremental_value=incremental_value,
        ci_low=ci_low,
        ci_high=ci_high,
        ci_crosses_zero=ci_low <= 0.0 <= ci_high,
        bootstrap_ci_low=bootstrap_low,
        bootstrap_ci_high=bootstrap_high,
    )


def economics(cases: Sequence[CaseOutcome], *, incremental_value: Money) -> EconomicStatistics:
    """METRICS-AND-KPIS SS3.2. `incremental_value` is the
    treatment-vs-control comparison's own value -- the headline
    (SS2.2) -- not recomputed here, so the two never drift apart."""
    total_cost = Money(sum((c.cost.paise for c in cases), 0))
    net_incremental_value = incremental_value - total_cost
    cost_per_rupee_recovered = (
        total_cost.paise / incremental_value.paise if incremental_value.paise > 0 else None
    )
    roi = net_incremental_value.paise / total_cost.paise if total_cost.paise > 0 else None
    return EconomicStatistics(
        total_cost=total_cost,
        cost_per_rupee_recovered=cost_per_rupee_recovered,
        net_incremental_value=net_incremental_value,
        roi=roi,
        mandate_budget_efficiency=None,
    )


def guardrails(cases: Sequence[CaseOutcome]) -> GuardrailStatistics:
    """METRICS-AND-KPIS SS4. Contact fatigue counts *contacted*
    customers only (the denominator in "mean contacts per contacted
    customer") -- a case with zero contact-channel actions (a
    control/holdout case, or a treatment case whose only step was
    payment_retry) does not dilute the average toward zero."""
    contacted = [c for c in cases if c.contact_events]
    if not contacted:
        fatigue_index = 0.0
    else:
        per_case_max_window_count = [_max_7day_contact_count(c.contact_events) for c in contacted]
        fatigue_index = sum(per_case_max_window_count) / len(contacted)

    opted_out = sum(1 for c in contacted if c.reason_code == "customer_opt_out")
    opt_out_rate = opted_out / len(contacted) if contacted else 0.0

    return GuardrailStatistics(
        contact_fatigue_index=fatigue_index,
        opt_out_rate=opt_out_rate,
        quiet_hour_violations=None,
    )


def _max_7day_contact_count(occurred_at: Sequence[datetime]) -> int:
    """The most contacts falling inside any 7-day window starting at one
    of this case's own contact timestamps -- a sliding-window maximum,
    not a fixed calendar week, since a benchmark case's contacts are not
    aligned to any particular Monday."""
    ordered = sorted(occurred_at)
    best = 0
    for start in ordered:
        count = sum(1 for at in ordered if start <= at < start + _CONTACT_FATIGUE_WINDOW)
        best = max(best, count)
    return best


def compute_statistics(
    cases: Sequence[CaseOutcome], *, rng_seed: int, bootstrap_resamples: int = _BOOTSTRAP_RESAMPLES
) -> BenchmarkStatistics:
    per_arm = {arm: arm_statistics(cases, arm) for arm in Arm}
    treatment_vs_control = compare_arms(
        cases,
        treatment=Arm.TREATMENT,
        comparison=Arm.CONTROL,
        label="treatment_vs_control",
        rng_seed=rng_seed,
        bootstrap_resamples=bootstrap_resamples,
    )
    treatment_vs_baseline = compare_arms(
        cases,
        treatment=Arm.TREATMENT,
        comparison=Arm.BASELINE,
        label="treatment_vs_baseline",
        rng_seed=rng_seed,
        bootstrap_resamples=bootstrap_resamples,
    )
    baseline_vs_control = compare_arms(
        cases,
        treatment=Arm.BASELINE,
        comparison=Arm.CONTROL,
        label="baseline_vs_control",
        rng_seed=rng_seed,
        bootstrap_resamples=bootstrap_resamples,
    )
    return BenchmarkStatistics(
        per_arm=per_arm,
        treatment_vs_control=treatment_vs_control,
        treatment_vs_baseline=treatment_vs_baseline,
        baseline_vs_control=baseline_vs_control,
        economics=economics(cases, incremental_value=treatment_vs_control.incremental_value),
        guardrails=guardrails(cases),
    )


async def load_case_outcomes(
    session: AsyncSession, bench_run_id: uuid.UUID
) -> Sequence[CaseOutcome]:
    """The repository half: assembles `CaseOutcome`s for every case a
    benchmark run opened. Kept deliberately thin -- one query per table,
    joined in Python -- so the actual statistics above stay dependency-free
    and testable without a database.

    `.order_by(CaseRow.id)`: SQL makes no row-order guarantee without one
    -- T3.8's own reproducibility test found this table's scan order
    happening to hold stable in practice, not structurally guaranteed to.
    `compare_arms`' bootstrap resampling draws by list position
    (`random.Random.choices`), so an unordered fetch would make two
    otherwise-identical runs' resampled CIs liable to diverge for no
    reason a reader could see in this module alone -- a fixed order (any
    fixed order; `id` needs no join, unlike a content-derived key) is
    what actually makes this function's own promise -- reproducible
    statistics from a reproducible run -- hold structurally, not by luck
    of a query plan.
    """
    case_rows = (
        (
            await session.execute(
                select(CaseRow).where(CaseRow.bench_run_id == bench_run_id).order_by(CaseRow.id)
            )
        )
        .scalars()
        .all()
    )
    case_ids = [row.id for row in case_rows]

    outcomes_by_case = {
        row.case_id: row
        for row in (
            await session.execute(select(OutcomeRow).where(OutcomeRow.case_id.in_(case_ids)))
        )
        .scalars()
        .all()
    }

    cost_by_case: dict[uuid.UUID, int] = {}
    contacts_by_case: dict[uuid.UUID, list[datetime]] = {}
    action_rows = (
        await session.execute(
            select(ScheduledActionRow, ActionRow)
            .join(ActionRow, ActionRow.id == ScheduledActionRow.action_id)
            .where(ScheduledActionRow.case_id.in_(case_ids), ScheduledActionRow.status == "done")
        )
    ).all()
    for scheduled, action in action_rows:
        cost_by_case[scheduled.case_id] = cost_by_case.get(scheduled.case_id, 0) + action.cost_paise
        if Channel(action.channel) in _CONTACT_CHANNELS:
            # `status == "done"` above guarantees `mark_done` already set
            # this -- it is the one place a scheduled action ever
            # completes (execution.outbox.mark_done's own docstring).
            assert scheduled.executed_at is not None
            contacts_by_case.setdefault(scheduled.case_id, []).append(scheduled.executed_at)

    results: list[CaseOutcome] = []
    for row in case_rows:
        outcome = outcomes_by_case.get(row.id)
        results.append(
            CaseOutcome(
                case_id=CaseId(row.id),
                arm=Arm(row.arm),
                at_risk=Money(row.at_risk_paise, Currency.INR),
                recovered=Money(outcome.recovered_paise, Currency.INR) if outcome else Money(0),
                cost=Money(cost_by_case.get(row.id, 0), Currency.INR),
                outcome_kind=OutcomeKind(outcome.kind) if outcome else None,
                reason_code=outcome.reason_code if outcome else None,
                contact_events=tuple(contacts_by_case.get(row.id, ())),
                case_state=row.state,
                playbook_id=row.playbook_id,
            )
        )
    return tuple(results)
