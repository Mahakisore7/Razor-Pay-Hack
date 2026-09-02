# Phase 5 — Intelligence

| Field | Value |
|---|---|
| Duration | 3 days |
| Depends on | P4 |
| Blocks | P6, P7 |
| Tag | `v0.5.0` |

**Goal:** replace the stub diagnosis with real slice statistics plus LLM
hypothesis ranking, and replace fixed retry timing with a contextual bandit.

Both are measured against what they replace. The benchmark already exists (P3),
so this phase can answer "did the intelligence help?" the moment it ships —
including the answer "no."

---

## Tasks

### T5.1 — Slice aggregation
- [ ] SQL aggregation over comparable cases by issuer, BIN range, PSP route,
      instrument, method, app version, time bucket
- [ ] Executed as SQL, never in application memory (TR-10)
- [ ] Baseline rates from a rolling comparison window
- [ ] Query-count assertions to prevent N+1

### T5.2 — Significance testing
- [ ] Two-proportion z-test per slice
- [ ] Configurable threshold (default `p < 0.01`), minimum sample 30
- [ ] Slices failing significance are **excluded**, not down-ranked
- [ ] `ABSTAIN` when no slice reaches significance
- [ ] Tests against known distributions with known answers

### T5.3 — L6 degradation detection
- [ ] EWMA change detection on rolling success rate
- [ ] Affected slice reported with the signal
- [ ] Configurable smoothing and threshold
- [ ] Test against the simulator's injected issuer outages — **detection rate and
      false-positive rate both reported**

### T5.4 — Redaction layer
- [ ] Strips all PII before any model payload is constructed
- [ ] **Asserts on its own output** — raises if any PII-patterned field survives
- [ ] Runs in production and in tests, on every call
- [ ] Test: a payload containing a phone number raises before the network call

### T5.5 — LLM hypothesis ranking
- [ ] Pydantic `RankedDiagnosis` / `Hypothesis` schemas
- [ ] `client.messages.parse(..., output_format=RankedDiagnosis)` with
      `model="claude-opus-5"`, `thinking={"type": "adaptive"}`,
      `output_config={"effort": "medium"}`
- [ ] Refusal fallbacks: `betas=["server-side-fallback-2026-07-01"]`,
      `fallbacks="default"`
- [ ] `root_cause` as a closed enum
- [ ] **Evidence-ID existence check** — a hypothesis citing a slice we did not
      compute is rejected
- [ ] 8s timeout; fallback to significance ranking on timeout, error, refusal, or
      schema violation
- [ ] `method` and `fallback_reason` recorded on every diagnosis
- [ ] **Per-cohort diagnosis**, keyed `(leak_class, decline_category, time_bucket)`

### T5.6 — Prompt caching
- [ ] System prompt as a module-level constant, no interpolation
- [ ] Playbook registry rendered with sorted keys
- [ ] `cache_control: {"type": "ephemeral", "ttl": "1h"}`
- [ ] **Byte-stability test**: `build_diagnosis_prompt() == build_diagnosis_prompt()`
- [ ] `cache_read_input_tokens` exported as a metric and asserted non-zero across
      a benchmark run

### T5.7 — Retry timing bandit
- [ ] Contextual bandit, Thompson sampling over discretised time buckets
- [ ] Features: decline category, day-of-month, hour, instrument, issuer, attempt
      number, mandate budget remaining
- [ ] Warm-start priors from generator-known-good values
- [ ] **Below 200 observed outcomes for a context bucket, defer to the prior** —
      do not explore on live cases (TR-19)
- [ ] Calibration measurement (ECE) reported

### T5.8 — Copy generation
- [ ] LLM-generated Hinglish / regional copy, `effort: "low"`
- [ ] Output passed to the P4 compliance validator
- [ ] Static template fallback on rejection
- [ ] Test: generated copy with a wrong amount is rejected

### T5.9 — Measurement
- [ ] Diagnosis top-1 / top-3 accuracy against simulator ground truth
- [ ] Calibration error (ECE)
- [ ] Abstention rate reported **next to** accuracy
- [ ] LLM fallback rate reported
- [ ] **Ablation table**: statistical-only vs Opus 5 vs Sonnet 5 — accuracy, cost,
      latency
- [ ] `make test-no-llm` — full suite with an LLM client that raises. Must pass.

---

## Acceptance criteria

| # | Criterion |
|---|---|
| A5.1 | **`make test-no-llm` passes** |
| A5.2 | The redactor raises on any PII-bearing payload |
| A5.3 | A malformed LLM response falls back without stalling the case |
| A5.4 | `cache_read_input_tokens` is non-zero across a benchmark run |
| A5.5 | Diagnosis accuracy measured and reported against ground truth |
| A5.6 | The ablation table is populated with real numbers |
| A5.7 | L6 detects the simulator's injected outages, with FP rate reported |
| A5.8 | Bandit beats the fixed schedule on mandate budget efficiency — **or the report says it did not** |

A5.1 is the phase gate: it proves the LLM contributes quality, not correctness.

A5.8 is the honesty gate. If the bandit loses, that goes in the report and in
[FAILURE-LOG](../05-submission/FAILURE-LOG.md). A negative result reported well is
worth more than a positive result obtained by tuning until it appeared.

---

## PR breakdown

| PR | Branch | Contents |
|---|---|---|
| 1 | `feat/diagnosis-slicer` | T5.1 |
| 2 | `feat/diagnosis-significance` | T5.2 |
| 3 | `feat/detection-l6-degradation` | T5.3 |
| 4 | `feat/llm-redaction-layer` | T5.4 |
| 5 | `feat/llm-hypothesis-ranking` | T5.5 |
| 6 | `perf/prompt-caching` | T5.6 |
| 7 | `feat/timing-bandit` | T5.7 |
| 8 | `feat/copy-generation` | T5.8 |
| 9 | `feat/diagnosis-evaluation` | T5.9 |

PR 4 lands before PR 5. No model call is ever made from a commit where the
redactor does not exist.

---

## Risks

| Risk | Mitigation |
|---|---|
| Bandit under-trained on 2,000 cases and loses to the fixed schedule | Warm-start priors; defer below the sample threshold; report calibration honestly. If it loses, report it. |
| LLM ranking does not beat z-score ordering | That is a valid finding. The ablation exists to detect it, and the architecture runs without the LLM. Removing it would be the correct response. |
| Prompt cache silently ineffective | Byte-stability test plus a non-zero `cache_read_input_tokens` assertion in the benchmark. |
| LLM cost surprises on a large run | Per-cohort diagnosis (~10x reduction); Batch API for benchmarks (50% cost); cost tracked per run. |
| Slice aggregation is slow at 2,000 cases | Index the slice columns; assert query counts; profile before optimising. |
