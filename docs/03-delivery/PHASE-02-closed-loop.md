# Phase 2 — Closed Loop

| Field | Value |
|---|---|
| Duration | 3 days |
| Depends on | P1 |
| Blocks | P3 |
| Tag | `v0.2.0` |

**Goal:** one case traverses the entire pipeline — webhook to outcome — with a
complete, verifiable audit chain, in dry-run.

**Deliberately stubbed:** diagnosis returns the decline category directly (no
slicing, no LLM), and one minimal playbook exists. This phase proves the *shape*
of the pipeline. Intelligence is P5, depth is P4.

The order within this phase matters: **the policy gate is built before the
executor.** There must be no commit in this repo where an action could execute
without a gate.

---

## Tasks

### T2.1 — Ingestion
- [ ] `POST /webhooks/razorpay`
- [ ] HMAC-SHA256 over the **raw body**, read before the JSON parser touches it
- [ ] `hmac.compare_digest`; 400 on mismatch with no detail leaked
- [ ] `raw_events` insert with `ON CONFLICT DO NOTHING` on provider event ID
- [ ] Ack after durable write, before interpretation (TR-2)
- [ ] Normalisation to the canonical decline taxonomy
- [ ] Unknown event types stored and flagged, never dropped
- [ ] Bulk import path for historical records
- [ ] Tests: valid signature, invalid signature, replay is a no-op, unknown type

### T2.2 — Detection (L1–L3)
- [ ] `Detector` protocol; pure functions of `(event, snapshot, clock)`
- [ ] **L1** failed one-time payment ← `payment.failed`
- [ ] **L2** failed mandate debit ← subscription charge failure
- [ ] **L3** halted subscription ← `subscription.halted`
- [ ] Deduplication against the partial unique index
- [ ] Case creation with arm assignment from `hash(seed | case_id)`, before diagnosis
- [ ] Golden fixture tests per detector

### T2.3 — Stub diagnosis
- [ ] `Diagnosis` produced directly from the decline category
- [ ] `method = STATISTICAL`, confidence 1.0, evidence empty
- [ ] Interface shaped so P5 substitutes the real engine without touching callers

### T2.4 — Minimal planning
- [ ] Playbook YAML loader with schema validation at startup
- [ ] One playbook: `insufficient-funds-v1` (retry, then payment link)
- [ ] Plan instantiation with **pinned playbook version**
- [ ] Fixed-schedule timing (bandit is P5)
- [ ] Cost ceiling computed; steps dropped to fit, each drop audited

### T2.5 — Policy gate skeleton *(before the executor)*
- [ ] `PolicyEngine.evaluate(action, ctx)` — pure function
- [ ] `PolicyContext` as values only, no repositories
- [ ] Four rules to start: kill switch, domain guards, consent, cost ceiling
- [ ] All three verdicts wired: `ALLOW`, `DENY`, `DEFER`
- [ ] Every decision persisted with `rule_id` and `inputs`
- [ ] import-linter contract verified: `policy` cannot reach `anthropic`

### T2.6 — Outbox and scheduler
- [ ] `scheduled_actions` claiming with `FOR UPDATE SKIP LOCKED`
- [ ] Claim TTL and reclaim of expired claims
- [ ] Scheduler loop promoting due actions
- [ ] Concurrency test: N workers, disjoint claims, no double-claim

### T2.7 — Executor
- [ ] **Asserts an `ALLOW` for the same `(action_id, attempt)`; raises otherwise**
- [ ] Redis `SET NX` idempotency check on the derived key
- [ ] Channel registry; `payment_retry` and `payment_link` via the gateway
- [ ] Messaging channels stubbed but cost-accounted
- [ ] Cost added to `case.cost_spent` **in the same transaction** as execution
- [ ] `dry-run` mode exercising every stage except the final side effect
- [ ] Tests: no ALLOW ⇒ raise; duplicate key ⇒ suppressed; crash mid-action ⇒
      reclaimed and not duplicated

### T2.8 — Attribution
- [ ] Deterministic matcher: customer, amount tolerance, 72h window
- [ ] Contention resolves to the older case; `attribution_ambiguous` emitted
- [ ] Holdout anchors to case creation, not to an action (TR-30)
- [ ] Terminal outcome assignment with mandatory `reason_code` on non-recovery
- [ ] **100% branch coverage** (TR-31)
- [ ] Property test: no payment is ever attributed to two cases

### T2.9 — Audit wiring
- [ ] Every state transition writes exactly one audit event, in the same
      transaction as the transition (I4)
- [ ] Trace ID on every event
- [ ] PII masked at write
- [ ] `recoup audit verify --case <id>` CLI

### T2.10 — End-to-end test
- [ ] Webhook fixture → case → plan → gate → execute → attribute → `RECOVERED`
- [ ] Audit chain verifies
- [ ] The suppressed path: opt-out mid-flight cancels scheduled steps and closes
      `SUPPRESSED` with a reason code

---

## Acceptance criteria

| # | Criterion |
|---|---|
| A2.1 | One case flows webhook → `RECOVERED` in dry-run |
| A2.2 | Its audit chain verifies, gapless, with every stage represented |
| A2.3 | **An action with no `ALLOW` raises rather than executing** |
| A2.4 | Replayed webhook creates no second signal |
| A2.5 | Two workers never claim the same scheduled action |
| A2.6 | A worker killed mid-action does not produce a duplicate side effect |
| A2.7 | Attribution has 100% branch coverage |
| A2.8 | Opt-out mid-plan cancels remaining steps and closes the case |
| A2.9 | The stub diagnosis is swappable without changing any caller |

A2.3 is the phase gate. Everything this product claims rests on it.

---

## PR breakdown

| PR | Branch | Contents |
|---|---|---|
| 1 | `feat/webhook-ingestion` | T2.1 |
| 2 | `feat/detection-l1-l3` | T2.2 |
| 3 | `feat/planning-playbook-loader` | T2.3, T2.4 |
| 4 | `feat/policy-gate-skeleton` | T2.5 |
| 5 | `feat/outbox-scheduler` | T2.6 |
| 6 | `feat/executor` | T2.7 |
| 7 | `feat/attribution` | T2.8 |
| 8 | `feat/audit-wiring` | T2.9 |
| 9 | `test/end-to-end-loop` | T2.10 |

PR 4 lands before PR 6. Enforced by ordering, and visible in the history.

---

## Risks

| Risk | Mitigation |
|---|---|
| HMAC fails because the framework re-serialises the body | Read `await request.body()` before parsing. Test with a real Razorpay-shaped payload, not a hand-built dict. |
| Attribution edge cases discovered late | 100% branch coverage in this phase, not later. Property test double-attribution now. |
| Outbox claiming subtly wrong under concurrency | Real Postgres in tests. A mocked `SKIP LOCKED` tests the mock. |
| Temptation to add the real diagnosis here | It is stubbed on purpose. Pipeline shape first, intelligence after it can be measured. |
