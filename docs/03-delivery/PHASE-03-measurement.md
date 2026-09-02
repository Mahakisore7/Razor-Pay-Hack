# Phase 3 — Measurement

| Field | Value |
|---|---|
| Duration | 2 days |
| Depends on | P2 |
| Blocks | P4 |
| Tag | `v0.3.0` |

**Goal:** the three-arm benchmark harness, built *before* the features it will
measure.

This ordering is the most consequential decision in the roadmap. Building
measurement after features produces a harness that inherits the features'
assumptions and flatters them. Building it first means every subsequent change
is measurable from the moment it exists, and a feature that does not move the
number is visible as such.

---

## Tasks

### T3.1 — Cohort generator
- [ ] Seeded generator producing N at-risk cases with realistic composition
- [ ] Configurable mix across leak classes L1–L3
- [ ] Realistic distributions: amounts (heavy-tailed), decline categories,
      issuers, instruments, customer propensity
- [ ] Ground truth recorded for every case
- [ ] `config/cohort.yaml` — every parameter externalised and reported
- [ ] Test: same seed ⇒ identical cohort, verified twice

### T3.2 — Arm assignment
- [ ] `arm = f(hash(seed | case_id))` — deterministic, computed at case creation
- [ ] Split: 10% control, 10% baseline, 80% treatment (configurable)
- [ ] Assignment happens **before** diagnosis, so it cannot be influenced by case
      characteristics
- [ ] Immutable once set
- [ ] Test: assignment distribution matches configuration within tolerance
- [ ] **Property test P9: a `control` case accumulates zero executed actions**

### T3.3 — Baseline arm
- [ ] Naive implementation: fixed retries at T+1h, T+24h, T+72h
- [ ] One generic dunning email, no diagnosis-driven routing
- [ ] No timing policy, no cost ceiling, no root-cause branching
- [ ] Deliberately *competent but naive* — this is what a good engineer builds in
      a weekend, and it is the comparison that actually matters

### T3.4 — Control arm
- [ ] Cases detected, diagnosed, and recorded
- [ ] **Zero actions executed** — enforced at the state machine (I7) and asserted
      in the data (I3)
- [ ] Attribution anchored to case creation, not to an action, so arms are
      measured identically (TR-30)

### T3.5 — Benchmark runner
- [ ] `recoup bench run --seed N --size M`
- [ ] Simulated time advancement — a 21-day case horizon must run in minutes
- [ ] All three arms in one run over one cohort
- [ ] Progress output; run recorded in `bench_runs`
- [ ] Target: 2,000 cases end to end in under 10 minutes (TR-45)

### T3.6 — Statistics
- [ ] Amount-weighted recovery rate per arm
- [ ] Incremental rate and value: treatment − control, treatment − baseline
- [ ] 95% CI on the difference of proportions
- [ ] **Bootstrap cross-check**, 10,000 resamples — the amount distribution is
      heavy-tailed, so the normal approximation is not automatically safe
- [ ] Explicit "CI crosses zero" determination
- [ ] Cost aggregation; cost per rupee recovered; net value; ROI
- [ ] Mandate budget efficiency
- [ ] Guardrails: contact fatigue index, opt-out rate, quiet-hour violations

### T3.7 — Report writer
- [ ] Markdown and JSON output to `bench/reports/<seed>-<timestamp>/`
- [ ] **Fixed section order** per [METRICS §8](../01-product/METRICS-AND-KPIS.md) —
      validity statement *before* the headline
- [ ] Run metadata: seed, git SHA, config hashes, playbook versions, gateway mode
- [ ] Full exception list, **never truncated**
- [ ] Policy denials grouped by rule
- [ ] Per-playbook breakdown
- [ ] Simulator parameters published in the report

### T3.8 — Reproducibility
- [ ] `make bench SEED=42` twice ⇒ byte-identical summary
- [ ] CI job running a small benchmark (200 cases) and asserting determinism
- [ ] Any non-determinism found is fixed, not tolerated

---

## Acceptance criteria

| # | Criterion |
|---|---|
| A3.1 | `make bench SEED=42` completes 2,000 cases in < 10 min |
| A3.2 | **Two runs at the same seed produce byte-identical summaries** |
| A3.3 | The report contains an incremental number with a CI |
| A3.4 | Control-arm cases have zero executed actions |
| A3.5 | The exception list is complete, not sampled |
| A3.6 | Cost per rupee recovered is reported next to the headline |
| A3.7 | Baseline beats control (else the simulated world is unrealistic) |
| A3.8 | The report states plainly whether the CI crosses zero |

**A3.2 is the phase gate.** A benchmark a reviewer cannot re-run is not evidence.

A3.7 is a sanity check on the simulator rather than on the product: if a retry
cron recovers nothing above doing nothing, the generated world does not resemble
payments and the numbers mean nothing.

---

## PR breakdown

| PR | Branch | Contents |
|---|---|---|
| 1 | `feat/bench-cohort-generator` | T3.1 |
| 2 | `feat/bench-arm-assignment` | T3.2 |
| 3 | `feat/bench-baseline-arm` | T3.3, T3.4 |
| 4 | `feat/bench-runner` | T3.5 |
| 5 | `feat/bench-statistics` | T3.6 |
| 6 | `feat/bench-report` | T3.7 |
| 7 | `test/bench-reproducibility` | T3.8 |

---

## Risks

| Risk | Mitigation |
|---|---|
| Hidden non-determinism (dict ordering, `set` iteration, float accumulation, parallel completion order) | The byte-identical test catches it. Run in CI. Sort everything that is iterated. |
| Simulated time advancement introduces subtle ordering bugs | Single logical clock, advanced explicitly. No wall-clock reads anywhere in the run. |
| Treatment beats control trivially because the simulator rewards any action | Baseline arm exists precisely to detect this. If treatment ≈ baseline, the intelligence is not contributing and the report must say so. |
| 10-minute target missed | Profile early. Batch database writes, and batch diagnosis by cohort (P5) rather than per case. |
| Temptation to tune the simulator until numbers look good | **Named as a risk here on purpose.** Simulator config changes require a PR with an explicit rationale, and the config hash is recorded on every report. |

The last row is the integrity risk of this entire project. The mitigation is
procedural rather than technical: any change to the simulator after benchmark
numbers exist must be justified in writing, in a PR, and the report records which
config produced it.
