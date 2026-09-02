# Metrics and KPIs — The Honest Measurement Doctrine

| Field | Value |
|---|---|
| Document version | 1.0 |
| Status | Approved for build |
| Related | [PRD](PRD.md) · [VISION](../00-overview/VISION.md) · [TRD](../02-technical/TRD.md) |

This document defines every number Recoup reports, exactly how it is computed,
and — for the ones that are easy to inflate — why the naive version is wrong.

The governing rule: **if a metric can be improved by doing nothing differently,
it is not a metric.**

---

## 1. Why recovery rate is a lie

The industry-standard metric for a dunning or recovery product is:

```
recovery_rate = recovered_amount / at_risk_amount
```

This number is worse than useless, because a large fraction of at-risk payments
recover on their own. A customer whose card declined for insufficient funds on
the 28th very often pays on the 1st without anyone doing anything at all.

Concretely: a system with **no recovery logic whatsoever** — one that detects
failures, does nothing, and waits — will report a recovery rate in the 25–35%
range on a realistic cohort. A vendor can therefore ship an empty loop and claim
a 30% recovery rate without lying about the arithmetic.

The only way to know what a recovery system contributed is to withhold it from a
randomised subset and compare.

## 2. The headline metric: incremental recovery

### 2.1 Definition

Cases are randomly assigned at creation to one of three arms:

| Arm | Assignment | What happens |
|---|---|---|
| `control` | 10% | Detected, diagnosed, recorded. **No actions execute.** |
| `baseline` | 10% | Naive fixed retry schedule (T+1h, T+24h, T+72h) plus one generic dunning email. No diagnosis-driven routing, no timing policy, no cost ceiling. |
| `treatment` | 80% | Full Recoup pipeline. |

Assignment is deterministic given the seed and the case ID, so a benchmark is
reproducible. Assignment happens *before* diagnosis, so it cannot be influenced
by case characteristics.

### 2.2 Computation

```
recovery_rate(arm) = Σ recovered_amount(arm) / Σ at_risk_amount(arm)

incremental_rate  = recovery_rate(treatment) − recovery_rate(control)

incremental_value = incremental_rate × Σ at_risk_amount(all arms)
```

`incremental_value` is the headline. It answers: *how many rupees exist because
Recoup ran, that would not exist otherwise.*

### 2.3 Confidence interval

A point estimate without an interval is a claim without evidence. We report a 95%
CI on the difference of two proportions, weighted by amount:

```
SE = sqrt( p_t(1−p_t)/n_t + p_c(1−p_c)/n_c )
CI = (p_t − p_c) ± 1.96 × SE
```

Where `p` is the amount-weighted recovery proportion and `n` the effective sample
size per arm. Bootstrap resampling (10,000 draws) is used as a cross-check
because the amount distribution is heavy-tailed and the normal approximation is
not automatically safe.

**If the CI crosses zero, the report says so in plain language, at the top.** A
result that is not distinguishable from noise is reported as not distinguishable
from noise.

### 2.4 The three-way comparison

Reporting against `control` alone shows Recoup beats doing nothing — a low bar.
Reporting against `baseline` shows Recoup beats what a competent engineer would
build in a weekend, which is the comparison that actually matters.

Both are always reported:

| Comparison | What it proves |
|---|---|
| treatment vs control | Recovery logic contributes at all |
| treatment vs baseline | The diagnosis, timing policy, and playbooks contribute beyond a retry cron |
| baseline vs control | How much of the industry-standard number is just a retry loop |

## 3. Economic metrics

Recovery that costs more than it recovers is a loss reported as a win. Every
action carries a cost, and every cost is attributed.

### 3.1 Cost model

| Channel | Unit cost (₹) | Notes |
|---|---|---|
| Payment retry / re-presentation | 0.00 | No direct cost, but consumes mandate budget — tracked separately as a scarce resource |
| SMS | 0.18 | DLT-registered transactional |
| WhatsApp utility template | 0.35 | Meta utility category |
| Email | 0.02 | |
| Voice call (IVR) | 1.20 | Per connected minute, rounded up |
| Human review | 45.00 | Loaded cost of an ops analyst touch |

Costs are configuration, not constants in code, and the benchmark reports which
cost table it used.

### 3.2 Reported economics

```
total_cost              = Σ cost(action) over executed actions
cost_per_rupee_recovered = total_cost / incremental_value
net_incremental_value    = incremental_value − total_cost
roi                      = net_incremental_value / total_cost
```

**`cost_per_rupee_recovered` is reported next to the headline, not in an
appendix.** A value above ₹1.00 means the system destroyed value and the report
labels it as such.

### 3.3 Mandate budget as a currency

UPI Autopay and NACH mandates permit a limited number of re-presentations per
cycle. Spending that budget is a real cost even though it has no rupee price,
because a burned budget forecloses a later retry that might have succeeded.

Reported as:

```
mandate_budget_efficiency = successful_representations / total_representations
```

A baseline retry cron scores badly here by construction — it spends the whole
budget on a fixed schedule regardless of whether the failure was retryable. This
is one of the clearest places Recoup should win, and if it does not, the timing
policy is not working.

## 4. Customer-experience metrics

Recovery that churns customers is borrowing from next quarter. Three guardrail
metrics, all of which are *upper-bounded*, not maximised:

| Metric | Definition | Guardrail |
|---|---|---|
| **Contact fatigue index** | Mean outbound contacts per contacted customer per 7-day window | ≤ 2.0 |
| **Opt-out rate** | Customers who opted out / customers contacted | ≤ 1.5% |
| **Quiet-hour violations** | Actions executed inside quiet hours | **Exactly 0** |

The third is not a target, it is an invariant. A single violation is a failing
build, enforced by a property test, not a dashboard alert.

## 5. Diagnosis quality

The synthetic generator knows the true root cause of every case it emits, so
diagnosis is measurable against ground truth. (On live test-mode data it is not,
and the report says so rather than inventing a number.)

| Metric | Definition |
|---|---|
| **Top-1 accuracy** | Fraction of cases where the winning hypothesis equals ground truth |
| **Top-3 accuracy** | Fraction where ground truth appears in the top three hypotheses |
| **Calibration error (ECE)** | Expected calibration error over confidence buckets — does "0.8 confidence" mean right 80% of the time? |
| **Abstention rate** | Fraction where no slice reached significance and the system declined to diagnose |

Abstention is a feature. A diagnosis engine that always produces an answer is
producing noise on the hard cases. **The report shows abstention rate next to
accuracy**, because accuracy on a filtered subset is not comparable to accuracy
on everything.

### 5.1 Ablation: does the LLM earn its place?

Reported for every benchmark run, because "AI judgment" means being able to
answer this:

| Configuration | Top-1 accuracy | Cost | Latency |
|---|---|---|---|
| Statistics only, rank by significance | — | ₹0 | ~5ms |
| Statistics + LLM ranking | — | ₹— | ~1.2s |

If the LLM does not beat pure statistical ranking by a margin that justifies its
cost and latency, **the honest conclusion is to remove it**, and the report will
say so. The architecture supports running without it precisely so this question
can be asked.

## 6. Attribution — the metric behind the metrics

Every recovery number depends on deciding whether an inbound payment "counts."
Getting this wrong invalidates everything above, so it is deterministic and
conservative.

A payment is attributed to a case only when **all** hold:

1. Same customer reference.
2. Amount within tolerance: `|payment − at_risk| ≤ max(₹1, 0.5% of at_risk)`.
3. Payment timestamp falls inside the attribution window: **72 hours** from the
   most recent executed action on the case.
4. The case is not already terminal.

Deliberate consequences of this design:

- **Payments outside the window are not counted**, even if the case later recovers.
  This *understates* Recoup's performance. That is the correct direction to err.
- **Holdout cases use the same rule**, anchored to the case creation time rather
  than to an action. Otherwise the arms would be measured differently and the
  comparison would be invalid.
- Partial payments produce `PARTIALLY_RECOVERED` with the actual amount, not a
  rounded-up win.
- When two open cases could both claim a payment, it goes to the **older** case,
  and the ambiguity is logged. Never to both.

Attribution has 100% branch coverage and property-based tests asserting that no
payment is ever double-counted across cases.

## 7. Operational metrics

Exported as Prometheus metrics; not part of the headline report but required for
NFR verification.

| Metric | Type | Purpose |
|---|---|---|
| `recoup_signals_detected_total{leak_class}` | counter | Detection volume |
| `recoup_cases_open` | gauge | Pipeline depth |
| `recoup_case_state_duration_seconds{state}` | histogram | Where cases stall |
| `recoup_policy_decisions_total{verdict,rule_id}` | counter | What is being blocked and by what |
| `recoup_actions_executed_total{channel,outcome}` | counter | Execution volume and failure rate |
| `recoup_action_cost_rupees_total{channel}` | counter | Live cost tracking |
| `recoup_llm_calls_total{purpose,status}` | counter | Model usage and fallback rate |
| `recoup_llm_schema_failures_total` | counter | How often we fall back to deterministic ranking |
| `recoup_attribution_ambiguous_total` | counter | Contested payment matches |
| `recoup_webhook_latency_seconds` | histogram | NFR-1 verification |

## 8. The benchmark report

`make bench` emits `bench/reports/<seed>-<timestamp>/report.md` plus machine-readable
JSON. Section order is fixed, and it is deliberate — the caveats come before the
headline:

1. **Run metadata** — seed, cohort size, arm split, cost table version, playbook
   versions, git SHA, whether live or simulated.
2. **Validity statement** — whether the CI crosses zero, sample sizes per arm,
   and any assumption that failed.
3. **Headline** — incremental recovery value and rate, with CI, vs control and vs
   baseline.
4. **Economics** — total cost, cost per rupee recovered, net value, ROI, mandate
   budget efficiency.
5. **Guardrails** — contact fatigue, opt-out rate, quiet-hour violations.
6. **Diagnosis quality** — accuracy, calibration, abstention, LLM ablation.
7. **Exception list** — every unresolved case with its reason code, in full, not
   truncated.
8. **Policy denials** — grouped by rule, showing what the system refused to do.
9. **Per-playbook breakdown** — so a playbook that loses money is visible.

Sections 7 and 8 exist because a report that only contains section 3 is marketing.

## 9. Success criteria for the build

The build is successful if the benchmark report shows all of:

| Criterion | Threshold |
|---|---|
| Incremental recovery vs control | Positive, CI excluding zero |
| Incremental recovery vs baseline | Positive |
| Cost per rupee recovered | < ₹0.15 |
| Mandate budget efficiency vs baseline | Materially higher |
| Quiet-hour violations | 0 |
| Opt-out rate | ≤ 1.5% |
| Diagnosis top-1 accuracy | > statistical-only baseline, or the LLM is removed |
| Reproducibility | Identical summary from identical seed |

**If a criterion is not met, it is reported as not met.** The failure log
([`docs/05-submission/FAILURE-LOG.md`](../05-submission/FAILURE-LOG.md)) is where
that story gets told, and per the buildathon brief it is the first thing the
reviewers read.
