# Product Requirements Document — Recoup

| Field | Value |
|---|---|
| Product | Recoup — Revenue Recovery Control Plane |
| Track | 03 — AI Revenue Recovery |
| Document version | 1.0 |
| Status | Approved for build |
| Author | Mahakisore |
| Related | [VISION](../00-overview/VISION.md) · [TRD](../02-technical/TRD.md) · [METRICS](METRICS-AND-KPIS.md) |

---

## 1. Problem statement

A merchant processing payments in India loses a material fraction of intended
revenue to failures that are individually small, individually recoverable, and
collectively ignored. The tooling that exists treats recovery as a scheduling
problem — retry on a fixed cron, send a fixed email sequence — and reports a
metric (recovery rate) that cannot distinguish its own contribution from
customers who would have paid regardless.

Recoup exists to convert revenue leakage from an accepted cost into a measured,
governed, and provably reduced one.

### 1.1 Who has this problem

Any merchant on Razorpay with either recurring revenue or non-trivial payment
volume. The pain scales with three things: subscription share of revenue, average
order value, and the fraction of traffic on UPI (where failure rates are highest
and mandate retry budgets are tightest).

### 1.2 Why now

Three things changed recently and make this both more painful and more solvable:

- **Recurring payments went mainstream in India.** UPI Autopay volumes have grown
  fast, which means mandate debit failure is now a first-order revenue problem
  rather than a rounding error. It also comes with hard re-presentation caps that
  punish naive retry loops.
- **Compliance tightened.** RBI pre-debit notification rules and TRAI DLT
  enforcement mean the "just send more emails" approach is not merely crude, it
  is a regulatory exposure. Recovery now has to be governed.
- **The reasoning gap closed.** Diagnosing *why* a cohort of payments is failing
  used to require an analyst. It is now tractable to do statistically and have a
  model narrate and rank the findings — provided the model is kept away from the
  arithmetic.

### 1.3 Evidence the problem is real

The failure taxonomy in this document is derived from Razorpay's published error
code documentation and standard UPI/NACH decline reasons. The synthetic data
generator ([`docs/02-technical/DATA-MODEL.md`](../02-technical/DATA-MODEL.md))
encodes failure-mode distributions that mirror the shape of publicly documented
Indian payment failure patterns — heavy on UPI timeouts and issuer-side declines,
with a long tail of instrument-specific codes.

We are explicit about this: **the benchmark runs on synthetic data with a
documented, seeded generator.** We do not claim real-merchant results. What we
claim is that the pipeline is correct, the measurement is honest, and the same
code path drives Razorpay test-mode when credentials are supplied.

## 2. Goals and non-goals

### 2.1 Goals

| # | Goal | How it is verified |
|---|---|---|
| G1 | Detect all six leak classes deterministically from event streams | Detector unit tests with golden fixtures |
| G2 | Diagnose root cause with attached statistical evidence | Diagnosis accuracy measured against generator ground truth |
| G3 | Choose and schedule a bounded intervention per case | Plan conformance tests against playbook definitions |
| G4 | Gate every outbound action on a deterministic compliance engine | Property-based tests: no action ever executes without an ALLOW |
| G5 | Measure incremental recovery against a randomised holdout | Benchmark report with confidence intervals |
| G6 | Produce a complete, tamper-evident audit trail per case | Hash-chain verification command; every action traceable to a decision |
| G7 | Run end to end with zero external credentials | `make demo` on a clean clone |
| G8 | Drive real Razorpay test-mode when credentials are present | Integration suite against test-mode sandbox |
| G9 | Stop safely on command, mid-flight | Kill-switch test: no action executes after trip, no state lost |

### 2.2 Non-goals

Restated from the vision, because scope discipline is a product decision:

- No collections escalation, legal action, or credit bureau reporting.
- No chargeback defence or fraud detection (that is Track 02).
- No money movement outside Razorpay's APIs.
- No broadcast marketing. Every message is bound to a specific at-risk amount.
- No autonomous action above the human-approval threshold.
- No production Razorpay keys under any circumstance.

## 3. Personas

### P1 — Priya, Revenue Operations Lead (primary)

Runs revenue ops at a D2C subscription business doing ~₹4 Cr/month, ~60%
recurring. Her week is consumed by a spreadsheet of failed debits and a set of
manual dunning campaigns she does not trust and cannot evaluate.

- **Wants:** to know how much is at risk right now, what is being done about it,
  and whether any of it is working.
- **Fears:** annoying good customers into churning; getting the company a TRAI
  complaint; being unable to answer "did your tool actually make money?"
- **Success:** she can point at a number in a board deck and defend how it was
  computed.

### P2 — Arjun, Finance Controller (secondary)

Owns the P&L. Cares about net recovery, not gross. Will kill any tool whose cost
per rupee recovered is not visible.

- **Wants:** cost attribution per channel, per playbook, per case.
- **Fears:** spending ₹40 of SMS to recover a ₹149 subscription.
- **Success:** he can see cost-per-rupee-recovered and set a ceiling that the
  system actually enforces.

### P3 — Meera, Compliance Officer (gatekeeper)

Approves or blocks anything that contacts customers or touches mandates.

- **Wants:** proof that quiet hours, DND, consent, and mandate caps are enforced
  in code and not by convention; a log of what was blocked.
- **Fears:** an LLM improvising a message or a retry outside policy.
- **Success:** she can read the policy rules as source, and audit denials.

### P4 — Dev, Platform Engineer (operator)

Runs the thing. Cares about idempotency, replay, and blast radius.

- **Wants:** a kill switch, dry-run mode, deterministic replay of any case.
- **Fears:** a bug that double-charges a customer.
- **Success:** he can replay any case from the audit log and get the same result.

## 4. Scope — the six leak classes

Each leak class is a first-class detector plus at least one playbook.

| ID | Leak | Signal source | Primary recovery instrument |
|---|---|---|---|
| L1 | Failed one-time payment | `payment.failed` webhook | Retry with routing hint, or payment link |
| L2 | Failed mandate debit | `subscription.charged` failure, mandate debit result | Bandit-timed re-presentation within budget |
| L3 | Halted subscription | `subscription.halted` webhook | Re-authorisation flow + payment link |
| L4 | Abandoned checkout | Order created without terminal payment inside window | Payment link with context-aware copy |
| L5 | Overdue receivable | Invoice due date passed, no matching settlement | Escalating reminder ladder + virtual account |
| L6 | Success-rate degradation | Rolling window change detection on aggregate SR | Route/instrument advisory + hold on doomed retries |

**Phase 1 implements L1, L2, L3 in depth.** L4 and L5 follow in Phase 2. L6 is
the diagnosis-engine showcase and lands with Phase 2 because it feeds the others.

Rationale: L1–L3 share the retry/mandate machinery and produce the cleanest
measurable recovery. L6 is architecturally the most interesting but is a
*diagnostic* input to the others rather than a standalone recovery loop.

## 5. Functional requirements

Requirements are numbered `FR-n` and are traceable to tests. `MUST` / `SHOULD` /
`MAY` follow RFC 2119.

### 5.1 Ingestion

- **FR-1** The system MUST ingest Razorpay webhook events with signature
  verification, rejecting any event failing HMAC validation.
- **FR-2** Ingestion MUST be idempotent on Razorpay event ID. A replayed webhook
  MUST NOT create a second signal.
- **FR-3** The system MUST support bulk import of historical payment, subscription,
  and invoice records for cold-start and benchmarking.
- **FR-4** Every ingested event MUST be persisted raw before interpretation, so
  detection logic can be re-run against history.

### 5.2 Detection

- **FR-5** Detectors MUST be deterministic and free of model inference.
- **FR-6** Each detector MUST emit a `Signal` carrying leak class, at-risk amount,
  customer reference, source event IDs, and detection timestamp.
- **FR-7** The system MUST NOT create a second open case for a customer and
  at-risk amount already covered by an open case (deduplication).
- **FR-8** Success-rate degradation detection MUST use a change-detection method
  (CUSUM or EWMA) rather than a static threshold, and MUST report the affected
  slice.

### 5.3 Diagnosis

- **FR-9** Diagnosis MUST compute candidate slices (issuer, BIN range, PSP route,
  instrument, method, app version, time bucket) using database aggregation only.
- **FR-10** Each candidate slice MUST carry a significance test result; slices
  failing significance MUST NOT be presented as hypotheses.
- **FR-11** The LLM MUST receive only pre-computed, aggregated statistics — never
  raw customer records, never PII.
- **FR-12** The LLM MUST return a ranked hypothesis list conforming to a strict
  schema; a non-conforming response MUST fall back to the deterministic
  rank-by-significance ordering.
- **FR-13** Every diagnosis MUST record the evidence it was based on, so it can be
  re-derived.

### 5.4 Planning

- **FR-14** Plans MUST be instantiated from a versioned playbook, and the playbook
  version MUST be recorded on the case.
- **FR-15** A plan MUST NOT contain a step type the playbook does not permit.
- **FR-16** Retry timing MUST come from the timing policy (bandit in treatment,
  fixed schedule in baseline), never from the LLM.
- **FR-17** Every plan MUST declare a total cost ceiling and MUST NOT schedule
  steps whose expected cost exceeds it.

### 5.5 Policy gate

- **FR-18** No action may execute without a recorded `ALLOW` policy decision
  issued within the same execution attempt.
- **FR-19** The policy engine MUST contain no model inference.
- **FR-20** The engine MUST enforce, at minimum: mandate re-presentation cap,
  contact frequency cap per channel, quiet hours, consent and DND state, per-case
  cost ceiling, per-action amount threshold, and the global kill switch.
- **FR-21** Every `DENY` and `DEFER` MUST be persisted with the rule identifier
  and the inputs that triggered it.
- **FR-22** Stopping rules MUST terminate the case, not merely block the action:
  customer opt-out, dispute filed, retry budget exhausted, max case age reached.
- **FR-23** Policy rules MUST be declarative and reviewable as source, versioned
  with the codebase.

### 5.6 Execution

- **FR-24** Every action MUST carry an idempotency key derived from case ID, step
  ID, and attempt number.
- **FR-25** Action execution MUST be durable across process restart — a claimed
  action MUST either complete or be safely re-claimed.
- **FR-26** The system MUST support `dry-run` mode where actions are fully
  planned and gated but produce no external side effect.
- **FR-27** Outbound messages MUST pass a deterministic compliance validator
  (template registration, opt-out presence, prohibited-language check) before send.
- **FR-28** A tripped kill switch MUST prevent all subsequent action execution
  within one scheduler tick, without losing queued state.

### 5.7 Attribution and outcome

- **FR-29** Attribution MUST be deterministic: a payment is credited to a case
  only if it matches on customer, amount tolerance, and falls inside the
  attribution window.
- **FR-30** A case MUST reach exactly one terminal outcome.
- **FR-31** Cases that cannot be resolved MUST be recorded as exceptions with a
  machine-readable reason code, and MUST appear in the benchmark report.
- **FR-32** Holdout cases MUST be diagnosed and recorded but MUST NOT produce
  actions.

### 5.8 Audit

- **FR-33** All state transitions, decisions, and actions MUST append to an
  immutable audit log.
- **FR-34** Audit events MUST be hash-chained; a verification command MUST detect
  any modification.
- **FR-35** The audit log MUST be sufficient to replay a case deterministically.
- **FR-36** PII in the audit log MUST be stored masked, with the unmasked value
  reachable only through an access-logged path.

### 5.9 Benchmarking

- **FR-37** The system MUST provide a single command that generates a seeded
  cohort, runs all three arms, and emits a report.
- **FR-38** The report MUST include incremental recovery with a confidence
  interval, not only gross recovery rate.
- **FR-39** The report MUST include cost per rupee recovered, contact volume, and
  the full exception list.
- **FR-40** Benchmark runs MUST be reproducible from the seed.

### 5.10 Console

- **FR-41** Operators MUST be able to view at-risk value, case pipeline, and
  outcomes over a time range.
- **FR-42** Operators MUST be able to open any case and see its full timeline
  including policy denials.
- **FR-43** Operators MUST be able to trip and clear the kill switch from the UI,
  with the actor recorded.
- **FR-44** Cases pending human approval MUST surface in an approval queue with
  approve/reject actions, both audited.

## 6. Non-functional requirements

| ID | Requirement | Target |
|---|---|---|
| NFR-1 | Detection latency from webhook receipt to signal | p95 < 2s |
| NFR-2 | Benchmark throughput | ≥ 2,000 cases end-to-end in < 10 min on a laptop |
| NFR-3 | API availability under demo load | No unhandled 5xx across a full benchmark run |
| NFR-4 | Determinism | Same seed ⇒ byte-identical benchmark summary |
| NFR-5 | Test coverage on policy engine and attribution | 100% branch coverage; property tests on invariants |
| NFR-6 | Test coverage overall | ≥ 85% line coverage on the core package |
| NFR-7 | Type safety | `mypy --strict` clean on core; `tsc --strict` clean on console |
| NFR-8 | Secret handling | No credential in source or logs; gitleaks clean in CI |
| NFR-9 | PII exposure to LLM | Zero. Enforced by a redaction layer with its own tests |
| NFR-10 | Cold start | `docker compose up` to working demo in < 5 min on a clean machine |
| NFR-11 | Traceability | Every action links to case, plan, policy decision, and trace ID |

## 7. Product principles

These resolve arguments during the build. When a decision is contested, the
higher-numbered principle yields to the lower.

1. **Never act without a recorded reason.** Every side effect traces to a
   diagnosis, a playbook, and an ALLOW decision.
2. **Deterministic where it must be right.** Money arithmetic, compliance, and
   measurement contain no model inference.
3. **Report the losses.** Exceptions, denials, and costs ship in the same report
   as the wins, at the same prominence.
4. **The customer's peace outranks the merchant's rupee.** When cost ceilings,
   frequency caps, or consent conflict with recovery, recovery loses.
5. **Reproducible or it didn't happen.** Any result a reviewer cannot regenerate
   from a seed is not a result.
6. **Boring technology in the money path.** Novelty is allowed in diagnosis and
   copy generation. It is not allowed in execution, policy, or attribution.

## 8. User journeys

### 8.1 Priya's morning (primary journey)

1. Opens the console. Sees ₹ at risk today, split by leak class, against yesterday.
2. Sees that L2 (mandate debits) spiked overnight, flagged by the degradation detector.
3. Opens the diagnosis: 71% of the spike concentrates on one issuer, `z = 4.8`,
   `p < 0.001`. The narration says the issuer's failure rate went from 4% to 31%
   in a two-hour window and recommends holding re-presentations until recovery,
   because burning mandate budget against a down issuer wastes the budget.
4. Sees Recoup has already deferred 340 re-presentations for that issuer, and that
   the deferral is a `DEFER` decision, not a silent drop, with a scheduled retry.
5. Approves the two cases above the ₹25,000 auto-action threshold sitting in her
   approval queue.
6. Checks the weekly benchmark: incremental recovery ₹X with a CI, cost per rupee
   recovered ₹Y, 43 exceptions listed by reason.

### 8.2 A single case, end to end

1. `payment.failed` arrives for ₹2,499 with `insufficient_funds`.
2. Detector emits an L1 signal. Case opens, at-risk ₹2,499. Randomiser assigns
   it to `treatment` (10% would have gone to holdout).
3. Diagnosis: not an issuer outage — the slice test is insignificant. Root cause
   `insufficient_funds`, confidence 0.86, evidence attached.
4. Playbook `insufficient-funds-v3` selected. Timing policy proposes a retry on
   the 1st at 10:30 IST (inferred salary-cycle proximity), plus a payment-link SMS
   at T+4h.
5. Policy gate on the SMS: customer has consent, is not on DND, is within
   frequency cap, and 16:20 IST is outside quiet hours ⇒ `ALLOW`. Compliance
   validator confirms DLT template match and opt-out footer. Sent.
6. Policy gate on the retry: mandate budget 2 of 3 used ⇒ `ALLOW`, budget
   decremented.
7. Customer pays via link at T+31h. Attribution matches on customer and amount
   inside the 72h window ⇒ case closes `RECOVERED`, attributed to the SMS step.
8. Audit trail: 14 hash-chained events, including the one `DENY` on a second SMS
   that would have breached the frequency cap.

### 8.3 The failure path (equally important)

1. Same case, but the customer replies STOP to the SMS.
2. Opt-out recorded on the consent ledger.
3. The stopping rule fires. The scheduled retry is cancelled, not merely blocked.
4. Case closes `SUPPRESSED` with reason `customer_opt_out`.
5. It appears in the exception list in the benchmark report. It counts against
   recovery rate. That is correct and it is not hidden.

## 9. Release plan

| Milestone | Contents | Gate |
|---|---|---|
| **M0 — Foundations** | Repo, CI, domain model, migrations, simulator skeleton | CI green on empty test suite |
| **M1 — Loop closed** | L1–L3 detection, diagnosis, policy gate, executor, attribution, audit | One case flows end to end in dry-run |
| **M2 — Measured** | Benchmark harness, three arms, holdout, report | 2,000-case run produces incremental number |
| **M3 — Governed** | Full policy rule set, compliance validator, kill switch, approval queue | Property tests pass; kill switch verified |
| **M4 — Visible** | Next.js console: dashboard, case timeline, approvals, kill switch | Reviewer can drive the demo from UI |
| **M5 — Live** | Razorpay test-mode integration behind the same adapter | Integration suite green against sandbox |
| **M6 — Submission** | README, architecture doc, pitch video, failure log | Clean-clone `make demo` verified by a third party |

Detail per milestone lives in [`docs/03-delivery/`](../03-delivery/ROADMAP.md).

## 10. Risks

| Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|
| Synthetic data makes results unfalsifiable | Judges discount the numbers | High | Publish the generator, its distributions, and its ground truth. Report against a *baseline* and *control*, so the comparison is internally valid regardless of realism. |
| LLM output breaks the pipeline | Cases stall | Medium | Strict schema validation with deterministic fallback ordering. The LLM is never on the critical path for correctness. |
| Razorpay test-mode API changes or rate-limits | Live demo fails | Medium | Simulator is the default; live mode is opt-in. Demo never depends on network. |
| Scope creep across six leak classes | Nothing finished well | High | Phase 1 is L1–L3 only, enforced by the roadmap. L4–L6 are explicitly deferred. |
| Bandit under-trained on short runs | Timing policy looks arbitrary | Medium | Warm-start from the generator's known-good priors; report calibration; fall back to fixed schedule below a sample threshold. |
| Over-engineering the console | Time sink, no scoring value | Medium | Console is M4, after the loop is measured. Ships shadcn defaults, no custom design system. |

## 11. Open questions

| # | Question | Owner | Needed by |
|---|---|---|---|
| Q1 | Does the demo target UPI Autopay or eNACH as the primary mandate rail in test mode? | Build | M5 |
| Q2 | Do we ship a voice channel (Hinglish) or keep it as a documented extension point? | Build | M3 |
| Q3 | Holdout fraction — 10% or 15%? Trade-off between measurement precision and recovered revenue. | Build | M2 |

Answers land as ADRs in [`docs/04-adr/`](../04-adr/).
