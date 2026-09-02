# Technical Requirements Document — Recoup

| Field | Value |
|---|---|
| Document version | 1.0 |
| Status | Approved for build |
| Related | [PRD](../01-product/PRD.md) · [ARCHITECTURE](ARCHITECTURE.md) · [ADRs](../04-adr/) |

The PRD says what to build. This says what it must technically satisfy, and with
what. Requirements are `TR-n` and each is verifiable.

---

## 1. Technology stack

Versions are pinned in lockfiles. Anything unpinned is a reproducibility bug.

### 1.1 Core service

| Component | Choice | Version | Notes |
|---|---|---|---|
| Language | Python | 3.12 | `match` statements, `StrEnum`, faster asyncio |
| Package manager | uv | ≥ 0.5 | Lockfile-first, reproducible resolution |
| Web framework | FastAPI | ≥ 0.115 | OpenAPI generated from the same Pydantic models used as the domain contract |
| Validation | Pydantic | v2 | Also serves as the LLM structured-output schema |
| ORM | SQLAlchemy | 2.0 async | `SKIP LOCKED` expressible without raw-SQL escape hatches |
| Migrations | Alembic | ≥ 1.14 | Autogenerate is a draft; every migration hand-reviewed |
| Database | PostgreSQL | 16 | Transactional integrity between state change and audit write |
| Cache / locks | Redis | 7 | Idempotency keys, rate limits, kill switch |
| LLM SDK | `anthropic` | latest | See [AI-DESIGN](AI-DESIGN.md) |
| Stats | NumPy, SciPy | current | Significance tests, bootstrap CIs |
| HTTP client | httpx | ≥ 0.27 | Async, timeouts mandatory |
| Logging | structlog | ≥ 24 | JSON, trace-correlated |
| Telemetry | OpenTelemetry SDK | current | Traces + metrics |
| CLI | Typer | ≥ 0.15 | `recoup` command |

### 1.2 Console

| Component | Choice | Version |
|---|---|---|
| Framework | Next.js (App Router) | 15 |
| Language | TypeScript (strict) | 5.x |
| Styling | Tailwind CSS | 4 |
| Components | shadcn/ui | current |
| Data fetching | RSC + TanStack Query | v5 |
| Charts | Recharts | 2.x |
| Tests | Vitest + Playwright | current |

### 1.3 Quality tooling

| Purpose | Tool | Gate |
|---|---|---|
| Lint + format | ruff | Zero warnings |
| Types (Python) | mypy `--strict` | Zero errors on `recoup.*` |
| Types (TS) | `tsc --noEmit` | Zero errors |
| Module boundaries | import-linter | All contracts pass |
| Tests | pytest, pytest-asyncio, Hypothesis, testcontainers | See §6 |
| Coverage | pytest-cov | ≥ 85% core, 100% branch on policy + attribution |
| Dependency audit | pip-audit, npm audit | No high/critical |
| Secret scanning | gitleaks | Zero findings |
| Container scanning | trivy | No high/critical |
| Commit hygiene | commitlint + pre-commit | Conventional Commits |

## 2. Functional technical requirements

### 2.1 Ingestion

- **TR-1** Webhook endpoint MUST verify the `X-Razorpay-Signature` HMAC-SHA256
  against the raw request body, using constant-time comparison. Verification
  MUST occur before parsing.
- **TR-2** The endpoint MUST return 2xx within **2 seconds at p95**, acknowledging
  after the durable raw write but before interpretation. (Razorpay redelivers on
  perceived failure; a slow handler manufactures duplicates.)
- **TR-3** Raw events MUST be stored verbatim with a unique constraint on the
  provider event ID; a replay MUST be a no-op via `ON CONFLICT DO NOTHING`.
- **TR-4** Interpretation MUST be re-runnable over stored raw events without
  re-fetching from Razorpay.
- **TR-5** Unparseable or unrecognised events MUST be stored and flagged, never
  discarded.

### 2.2 Detection

- **TR-6** Detectors MUST be pure functions of `(event, repository snapshot, clock)`.
- **TR-7** The clock MUST be injected. `datetime.now()` inside `recoup.domain` or
  `recoup.detection` is a lint failure — a hidden clock read makes replay
  non-deterministic.
- **TR-8** Deduplication MUST use a partial unique index on
  `(customer_id, at_risk_paise)` where the case state is non-terminal.
- **TR-9** L6 degradation detection MUST run on a rolling window with EWMA, with
  the smoothing factor and threshold in configuration.

### 2.3 Diagnosis

- **TR-10** Slice aggregation MUST execute as SQL, not in application memory.
- **TR-11** Significance MUST use a two-proportion z-test with a configurable
  threshold (default `p < 0.01`) and a minimum sample size (default 30).
- **TR-12** LLM payloads MUST pass the redaction assertion; a payload containing
  any PII-patterned field MUST raise before the network call.
- **TR-13** LLM calls MUST have an 8-second timeout and MUST fall back to
  statistical ranking on timeout, error, refusal, or schema violation.
- **TR-14** Diagnosis MUST be computed per cohort key
  `(leak_class, decline_category, time_bucket)`, not per case.
- **TR-15** The system prompt MUST be byte-stable across constructions, verified
  by test, so prompt caching is effective.

### 2.4 Planning

- **TR-16** Playbooks MUST load and schema-validate at startup; an invalid
  playbook MUST prevent boot rather than degrade at runtime.
- **TR-17** Plans MUST pin the playbook version, so editing a playbook never
  retroactively alters a running case.
- **TR-18** The planner MUST drop lowest-value steps until the plan fits the cost
  ceiling, and MUST audit each drop.
- **TR-19** Retry timing MUST come from the timing policy interface. Below 200
  observed outcomes for a context bucket, the bandit MUST defer to the warm-start
  prior rather than explore on live cases.

### 2.5 Policy and execution

- **TR-20** `PolicyEngine.evaluate` MUST be a pure function of `(action, context)`.
- **TR-21** The `recoup.policy` package MUST NOT transitively import `anthropic`,
  enforced by import-linter contract.
- **TR-22** The executor MUST assert an `ALLOW` decision exists for the same
  `(action_id, attempt)` before any side effect, and MUST raise otherwise.
- **TR-23** Every action MUST carry a derived idempotency key
  `sha256(case_id | step_id | attempt)`, checked against Redis with `SET NX`.
- **TR-24** Scheduled actions MUST be claimed with
  `SELECT ... FOR UPDATE SKIP LOCKED`, with a claim TTL after which an orphaned
  claim is reclaimable.
- **TR-25** The kill switch MUST be read on every execution attempt with no
  caching, and MUST take effect within one scheduler tick (≤ 5s).
- **TR-26** Dry-run mode MUST exercise every stage including policy evaluation and
  message validation, substituting only the final side effect.
- **TR-27** Action cost MUST be added to `case.cost_spent` in the same database
  transaction as the execution record.

### 2.6 Attribution

- **TR-28** Attribution MUST be deterministic per [METRICS §6](../01-product/METRICS-AND-KPIS.md).
- **TR-29** A payment MUST NOT be attributed to more than one case; contention
  MUST resolve to the older case and MUST emit `attribution_ambiguous`.
- **TR-30** Holdout cases MUST anchor their attribution window to case creation,
  not to an action, so arms are measured identically.
- **TR-31** Attribution MUST have 100% branch coverage.

### 2.7 Audit

- **TR-32** `audit_events` MUST reject `UPDATE` and `DELETE` via a database
  trigger, not application convention.
- **TR-33** Each event MUST store `prev_hash` and `hash` over a canonical JSON
  serialisation with sorted keys.
- **TR-34** `recoup audit verify` MUST detect any modification, insertion, or
  deletion and report the first divergent sequence number.
- **TR-35** `seq` MUST be gapless per case, enforced by a unique constraint on
  `(case_id, seq)`.
- **TR-36** PII in audit payloads MUST be masked at write time.

### 2.8 Benchmarking

- **TR-37** `make bench` MUST accept a seed and produce byte-identical summary
  output for identical seeds.
- **TR-38** All randomness MUST derive from the run seed via explicitly-passed
  generators. A bare `random.random()` or `numpy.random.*` global call in
  `recoup.*` is a lint failure.
- **TR-39** Arm assignment MUST be `hash(seed | case_id)`, computed before
  diagnosis and immutable thereafter.
- **TR-40** The report MUST emit both Markdown and JSON.

## 3. Performance requirements

| ID | Requirement | Target | Verification |
|---|---|---|---|
| TR-41 | Webhook ack latency | p95 < 2s, p99 < 5s | Load test in CI |
| TR-42 | Detection throughput | ≥ 500 events/s single worker | Benchmark |
| TR-43 | Policy evaluation | p99 < 10 ms | Micro-benchmark |
| TR-44 | Diagnosis (cached prefix) | p95 < 2s | Instrumented |
| TR-45 | Full benchmark, 2,000 cases | < 10 min on 8-core laptop | `make bench` timing |
| TR-46 | Console dashboard load | p95 < 1.5s | Playwright |
| TR-47 | Cold start to demo | < 5 min clean machine | Documented and timed |

TR-45 is the one that matters for the submission — a reviewer will not wait
longer than that, and a benchmark that cannot be re-run is a benchmark that
cannot be checked.

## 4. Security requirements

Full threat model in [SECURITY.md](SECURITY.md).

- **TR-48** No credential in source, logs, error messages, or committed config.
  gitleaks in CI and in pre-commit.
- **TR-49** Razorpay **test-mode keys only**. A key not matching `rzp_test_*` MUST
  fail startup with an explicit error.
- **TR-50** PII MUST be stored in a separate table behind an access-logged
  repository. Domain objects carry references, not values.
- **TR-51** Every outbound HTTP call MUST have an explicit timeout. An unbounded
  call is a lint failure.
- **TR-52** Webhook signature verification MUST use `hmac.compare_digest`.
- **TR-53** Console API access MUST be authenticated; approval and kill-switch
  actions MUST record the actor identity.
- **TR-54** Container images MUST run as a non-root user.
- **TR-55** Dependencies MUST be pinned by hash in the lockfile.

## 5. Reliability requirements

- **TR-56** No action may be lost on process termination — claimed-but-incomplete
  actions become reclaimable after the claim TTL.
- **TR-57** No action may execute twice — guaranteed by the idempotency key even
  across reclaim.
- **TR-58** Razorpay calls MUST retry with exponential backoff and jitter on 5xx
  and 429 only. 4xx MUST NOT be retried.
- **TR-59** A circuit breaker MUST open on repeated gateway failure and MUST
  surface as a DEFER, not a DENY — an unavailable provider is a timing problem.
- **TR-60** Database migrations MUST be backward-compatible within a minor
  version (expand/contract).
- **TR-61** Every terminal case state MUST be reachable and MUST be exercised by
  at least one test.

## 6. Testing requirements

| Layer | Requirement |
|---|---|
| **Unit** | Every domain invariant and every policy rule, individually |
| **Property (Hypothesis)** | The 9 invariants in [POLICY-ENGINE §6.2](POLICY-ENGINE.md), plus case-state and Money-arithmetic properties |
| **Golden fixtures** | ~60 `(action, context) → decision` pairs, doubling as compliance-reviewable documentation |
| **Integration** | Real Postgres and Redis via testcontainers. No mocked database — a mocked `SKIP LOCKED` tests nothing. |
| **Contract** | Simulator and live client MUST satisfy the same `PaymentGateway` conformance suite |
| **End-to-end** | Full pipeline in dry-run over a seeded cohort |
| **Adversarial** | Prompt-injection payloads through every text path, asserting no behaviour change |
| **No-LLM** | `make test-no-llm` — full suite with an LLM client that raises. MUST pass. |
| **Concurrency** | Parallel workers against one mandate, asserting the re-presentation cap holds |

- **TR-62** CI MUST fail on coverage regression.
- **TR-63** Tests MUST NOT depend on wall-clock time or network access. The clock
  is injected; the network is the simulator.
- **TR-64** The full suite MUST complete in under 5 minutes.

## 7. Interface requirements

- **TR-65** The REST API MUST be documented by generated OpenAPI 3.1, served at
  `/openapi.json`.
- **TR-66** Money MUST cross API boundaries as `{"paise": int, "currency": str}`,
  never as a decimal or float.
- **TR-67** Timestamps MUST be RFC 3339 with explicit offset. Naive datetimes are
  rejected at the boundary.
- **TR-68** List endpoints MUST use cursor pagination.
- **TR-69** Errors MUST follow RFC 9457 Problem Details.
- **TR-70** The case timeline endpoint MUST stream via SSE for live updates.

## 8. Operational requirements

- **TR-71** `/health/live` and `/health/ready` MUST be distinct; readiness MUST
  check database and Redis.
- **TR-72** Prometheus metrics at `/metrics`, per
  [METRICS §7](../01-product/METRICS-AND-KPIS.md).
- **TR-73** All logs JSON, carrying `trace_id`, `case_id` where applicable, and
  never raw PII.
- **TR-74** Every audit event MUST carry the OpenTelemetry trace ID, so an audit
  entry links to its distributed trace.
- **TR-75** Configuration MUST come from environment variables via a typed
  settings object; an invalid or missing required setting MUST fail startup.
- **TR-76** `docker compose up` MUST bring up a working system with no manual
  steps beyond `make demo`.

## 9. Constraints

| Constraint | Consequence |
|---|---|
| Razorpay test mode only | No production keys accepted; startup validates the key prefix |
| Synthetic benchmark data | Absolute recovery rates are not claimed as real-world; only internal arm comparison is claimed |
| Single-region, single-instance Postgres | No cross-region attribution concerns in scope |
| No PCI scope | Card data never stored or transmitted; only Razorpay tokens and last-4 |
| Solo build | Favours a modular monolith over a distributed system; recorded in [ADR-0002](../04-adr/ADR-0002-modular-monolith.md) |
| Messaging providers not integrated live | Channel adapters are simulated by default; the interface is real, the transport is stubbed. Stated plainly in the README rather than implied otherwise. |

The last one is worth being explicit about: **Recoup does not send real SMS.** The
channel adapter, cost accounting, compliance validation, and consent enforcement
are all real; the transport is a simulator. Claiming otherwise would be the kind
of overstatement this project's entire measurement doctrine exists to avoid.

## 10. Traceability

| PRD requirement | TRD requirements | Primary tests |
|---|---|---|
| FR-1, FR-2 (ingestion integrity) | TR-1..TR-5 | `test_webhook_signature`, `test_replay_idempotent` |
| FR-5..FR-8 (detection) | TR-6..TR-9 | `test_detectors_*`, `test_dedup_partial_index` |
| FR-9..FR-13 (diagnosis) | TR-10..TR-15 | `test_slicer_sql`, `test_redaction_blocks_pii`, `test_llm_fallback` |
| FR-14..FR-17 (planning) | TR-16..TR-19 | `test_playbook_load`, `test_plan_fits_ceiling` |
| FR-18..FR-23 (policy) | TR-20..TR-22 | `test_policy_properties` (P1–P9), golden fixtures |
| FR-24..FR-28 (execution) | TR-23..TR-27 | `test_idempotency`, `test_kill_switch`, `test_concurrent_mandate` |
| FR-29..FR-32 (attribution) | TR-28..TR-31 | `test_attribution_*` (100% branch) |
| FR-33..FR-36 (audit) | TR-32..TR-36 | `test_audit_chain`, `test_audit_immutable_trigger` |
| FR-37..FR-40 (benchmark) | TR-37..TR-40 | `test_bench_reproducible` |
| FR-41..FR-44 (console) | TR-46, TR-53, TR-70 | Playwright suite |
