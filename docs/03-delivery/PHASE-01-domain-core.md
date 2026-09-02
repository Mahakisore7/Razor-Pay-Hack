# Phase 1 — Domain Core

| Field | Value |
|---|---|
| Duration | 2 days |
| Depends on | P0 |
| Blocks | P2 |
| Tag | `v0.1.0` |

**Goal:** the pure domain package, the database schema with its constraints, and
a deterministic simulator. No pipeline yet — just the vocabulary and the physics.

---

## Tasks

### T1.1 — Money
- [ ] Frozen dataclass, integer paise, `Currency` enum
- [ ] Construct from `int` or `Decimal` **only**; float raises `TypeError`
- [ ] Arithmetic; cross-currency operations raise
- [ ] `allocate(ratios)` — remainder distributed deterministically, sum exact
- [ ] Serialise as `{"paise": int, "currency": str}`
- [ ] Property tests: allocation always sums to the original; no float path exists

### T1.2 — Identifiers and references
- [ ] `NewType` IDs: `CaseId`, `SignalId`, `ActionId`, `AuditEventId`
- [ ] UUIDv7 generation
- [ ] `CustomerRef` with `contact_hash`, no PII

### T1.3 — Decline taxonomy
- [ ] `DeclineCategory` enum with the full canonical set
- [ ] Static properties: `retryable`, `customer_action_required`, `retry_horizon`
- [ ] `config/decline_taxonomy.yaml` mapping Razorpay reasons → categories
- [ ] Loader with validation; unmapped → `UNKNOWN` (non-retryable)
- [ ] Test: every enum member has all three properties defined

### T1.4 — Signal and Case
- [ ] `Signal` frozen dataclass with invariants (`at_risk > 0`, non-empty sources)
- [ ] `Case` with the state machine as an explicit transition table
- [ ] `IllegalTransition` raised on any transition not in the table
- [ ] `Arm` enum, assigned from `hash(seed | case_id)`
- [ ] Property tests: every case reaches exactly one terminal state (I1); a
      `control` case never enters `EXECUTING` (I7)

### T1.5 — Plan, Action, PolicyDecision, Outcome
- [ ] Dataclasses per [DOMAIN-MODEL](../02-technical/DOMAIN-MODEL.md)
- [ ] Derived idempotency key `sha256(case_id|step_id|attempt)`
- [ ] `Outcome` constructor rejects a non-recovery outcome without a `reason_code`

### T1.6 — Mandate and consent
- [ ] `Mandate` with re-presentation budget accounting
- [ ] Debit above `max_amount` or outside validity raises in the domain layer
- [ ] `ConsentEvent` and the `consent_at(events, channel, when)` fold
- [ ] Test: absence of any consent record evaluates to `False`, never `True`

### T1.7 — Audit
- [ ] `AuditEvent` with canonical JSON serialisation (sorted keys)
- [ ] Hash chain: `hash = sha256(canonical(event minus hash))`
- [ ] `verify_chain(events)` returning the first divergent `seq`
- [ ] Tests: tampering with a payload, reordering, and deleting an event are each
      detected

### T1.8 — Database schema
- [ ] SQLAlchemy 2.0 models for all tables
- [ ] Alembic initial migration, **hand-reviewed** — autogenerate misses CHECK
      constraints, partial indexes, and triggers, which is everything interesting
- [ ] `audit_events` immutability trigger
- [ ] `cases_open_dedup` partial unique index
- [ ] `cost_within_ceiling` and `resolved_iff_terminal` CHECK constraints
- [ ] Integration tests against real Postgres asserting each constraint **rejects**
      the thing it is supposed to reject

### T1.9 — Simulator
- [ ] `PaymentGateway` Protocol
- [ ] `RazorpaySimulator` implementing it, seeded, no network
- [ ] Model: issuer outages (correlated bursts), salary-cycle `insufficient_funds`,
      diurnal success variation, mandate budgets, instrument-specific rates,
      latent customer payment propensity, intervention response, network faults
- [ ] `config/simulator.yaml` — every parameter externalised
- [ ] Ground-truth recording, write-only from the simulator side
- [ ] Test: no module outside `bench.evaluation` imports the ground-truth table
- [ ] **Determinism test: two independent runs at the same seed, byte-identical**

### T1.10 — Platform
- [ ] Injected `Clock` protocol with `SystemClock` and `FrozenClock`
- [ ] ruff rule banning `datetime.now()` in `domain`, `detection`, `policy`
- [ ] Seeded RNG passed explicitly; ruff rule banning global `random.*`

---

## Acceptance criteria

| # | Criterion |
|---|---|
| A1.1 | `Money(2499.99)` raises `TypeError` |
| A1.2 | `Money(100).allocate([1,1,1])` sums to exactly 100 paise |
| A1.3 | An illegal case transition raises `IllegalTransition` |
| A1.4 | `UPDATE audit_events` is rejected by the database |
| A1.5 | Two open cases for the same customer and amount cannot both exist |
| A1.6 | Tampering with any audit payload is detected by `verify_chain` |
| A1.7 | **Simulator: same seed ⇒ byte-identical output, verified twice** |
| A1.8 | `consent_at` returns `False` when no record exists |
| A1.9 | `mypy --strict` clean; all import-linter contracts pass |

**A1.7 is the phase gate.** Every number this project will ever report depends on
it. If it does not hold, stop and fix it before proceeding — a benchmark built on
a non-deterministic simulator is unfalsifiable, which is the exact failure mode
this project exists to avoid.

---

## PR breakdown

| PR | Branch | Contents |
|---|---|---|
| 1 | `feat/domain-money` | T1.1, T1.2 |
| 2 | `feat/domain-decline-taxonomy` | T1.3 |
| 3 | `feat/domain-case-state-machine` | T1.4, T1.5 |
| 4 | `feat/domain-mandate-consent` | T1.6 |
| 5 | `feat/audit-hash-chain` | T1.7 |
| 6 | `feat/db-schema-and-constraints` | T1.8 |
| 7 | `feat/gateway-simulator` | T1.9 |
| 8 | `feat/platform-clock-rng` | T1.10 |

---

## Risks

| Risk | Mitigation |
|---|---|
| Simulator realism is guessed and results become meaningless | Publish every parameter in the report. Claim only the *internal* arm comparison, never absolute real-world rates. |
| Over-modelling the simulator burns the phase | Model only the phenomena a downstream component claims to detect. If nothing detects it, do not simulate it. |
| Hidden non-determinism (dict ordering, set iteration, float accumulation) | The two-run byte-identical test catches it. Run it in CI, not just locally. |
| Alembic autogenerate silently drops constraints | Every migration hand-reviewed; integration tests assert each constraint rejects |
