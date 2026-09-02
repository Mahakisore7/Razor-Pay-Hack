# Phase 4 — Governance

| Field | Value |
|---|---|
| Duration | 3 days |
| Depends on | P3 |
| Blocks | P5 |
| Tag | `v0.4.0` |

**Goal:** the complete policy rule set, the stopping rules, the compliance
validator, playbook depth, the kill switch, and the approval queue — with the
nine property invariants proven.

This is the phase that makes Recoup trustworthy rather than merely functional,
and it is the last one that must complete before the 5 September submission.

---

## Tasks

### T4.1 — Complete rule set
Implement R1–R11 from [POLICY-ENGINE §3](../02-technical/POLICY-ENGINE.md), one
file per rule, each individually unit-tested:

- [ ] R1 kill switch (global + per-playbook)
- [ ] R2 stopping rules — **terminate the case, not just the action**
- [ ] R3 domain guards
- [ ] R4 consent, evaluated at `due_at` from the ledger fold
- [ ] R5 DND, promotional only
- [ ] R6 quiet hours — **customer timezone**, DEFER not DENY
- [ ] R7 frequency caps — counted **across all cases** for the customer
- [ ] R8 cost ceiling + global daily cap
- [ ] R9 mandate budget, reserved atomically before the gateway call
- [ ] R10 approval threshold
- [ ] R11 rate limits
- [ ] Evaluation order per §2.2, so the recorded `rule_id` is the most
      fundamental reason

### T4.2 — Stopping rules
- [ ] `customer_opt_out` → `SUPPRESSED`
- [ ] `dispute_filed` → `SUPPRESSED`, immediate, never reopened
- [ ] `already_paid` → `RECOVERED`
- [ ] `max_attempts_reached` → `LOST`
- [ ] `max_case_age` → `EXPIRED`
- [ ] `mandate_revoked` → `ESCALATED`
- [ ] `promise_to_pay_active` → defer the whole case
- [ ] Each **cancels scheduled actions**, not merely blocks the current one

### T4.3 — Compliance validator
- [ ] DLT template registry and conformance matching
- [ ] Sender ID check
- [ ] Opt-out presence on promotional messages
- [ ] Prohibited-language blocklist, English + Hinglish
- [ ] **Exact amount match against `case.at_risk`**
- [ ] Payment link integrity — Razorpay-issued, for this case
- [ ] Encoding-aware length (UCS-2 for Devanagari)
- [ ] PII minimisation
- [ ] **Rejection never auto-fixes** — falls back to a static template

### T4.4 — Playbook depth
- [ ] `insufficient-funds-v3`, `issuer-down-v1`, `mandate-revoked-v1`,
      `generic-v1` (for abstentions)
- [ ] Loader constraints: `cost_ceiling_pct` in (0, 10]; declared channels exist;
      mandate-consuming steps flagged
- [ ] Invalid playbook prevents boot rather than degrading at runtime
- [ ] `issuer-down` correctly does **not** message the customer — waiting is the
      right action, and messaging is pure cost

### T4.5 — Kill switch
- [ ] Redis-backed, global and per-playbook
- [ ] Read on every execution attempt, no caching
- [ ] Effective within one scheduler tick (≤ 5s)
- [ ] Claims retained, state preserved, nothing lost
- [ ] API endpoints with mandatory reason and actor recording
- [ ] Test: trip mid-benchmark, assert zero subsequent executions and zero lost
      actions

### T4.6 — Approval queue
- [ ] Cases above threshold → `AWAITING_APPROVAL`
- [ ] Approve / reject endpoints, actor audited, reason required on reject
- [ ] Rejection closes the case `SUPPRESSED`
- [ ] Test: no action executes on an unapproved case above threshold

### T4.7 — Property tests P1–P9
The nine invariants from [POLICY-ENGINE §6.2](../02-technical/POLICY-ENGINE.md):

- [ ] P1 `evaluate` always returns exactly one verdict, never raises
- [ ] P2 cost never exceeds the ceiling
- [ ] P3 **no message in quiet hours, any timezone**
- [ ] P4 no message without consent at `due_at`
- [ ] P5 **re-presentations never exceed the cap, under concurrency**
- [ ] P6 after a stopping rule, no subsequent `ALLOW`
- [ ] P7 frequency cap holds across all of a customer's cases
- [ ] P8 determinism — identical inputs, identical verdict
- [ ] P9 control-arm cases execute zero actions

### T4.8 — Golden decision fixtures
- [ ] ~60 `(action, context) → expected decision` pairs
- [ ] Every rule, every boundary: one minute before quiet hours, exactly at the
      cap, one paise under the ceiling
- [ ] Readable as compliance documentation — Meera reads these, not the code

### T4.9 — Adversarial suite
- [ ] Injection payloads in name fields, SMS replies, and support notes
- [ ] LLM narration arguing for urgency — assert no behaviour change
- [ ] Malformed playbook with `cost_ceiling_pct: 500` — rejected at load
- [ ] Assert 100% branch coverage on `policy`

---

## Acceptance criteria

| # | Criterion |
|---|---|
| A4.1 | **All nine property tests pass** |
| A4.2 | `policy` and `attribution` at 100% branch coverage |
| A4.3 | Kill switch halts execution within one tick, losing nothing |
| A4.4 | Every denial is audited with `rule_id` and inputs |
| A4.5 | A rejected message falls back to a template, never auto-fixed |
| A4.6 | The adversarial suite passes with no behaviour change |
| A4.7 | An unapproved above-threshold case executes nothing |
| A4.8 | Benchmark re-run: guardrail metrics present, quiet-hour violations **zero** |

A4.1 is the phase gate and the submission gate. These nine properties are the
compliance guarantees the product claims; unproven, the claim is marketing.

---

## PR breakdown

| PR | Branch | Contents |
|---|---|---|
| 1 | `feat/policy-rules-consent-dnd` | R4, R5 |
| 2 | `feat/policy-rules-timing` | R6, R7, R11 |
| 3 | `feat/policy-rules-economics` | R8, R9, R10 |
| 4 | `feat/policy-stopping-rules` | T4.2 |
| 5 | `feat/compliance-validator` | T4.3 |
| 6 | `feat/playbook-registry` | T4.4 |
| 7 | `feat/kill-switch` | T4.5 |
| 8 | `feat/approval-queue` | T4.6 |
| 9 | `test/policy-property-invariants` | T4.7, T4.8 |
| 10 | `test/adversarial-suite` | T4.9 |

---

## Risks

| Risk | Mitigation |
|---|---|
| Property tests are slow and get skipped | Cap Hypothesis examples in CI, run the deep profile nightly. Never mark them skip. |
| Quiet-hours timezone logic is subtly wrong | P3 generates timezones across UTC-12..UTC+14. Do not test only IST. |
| Frequency caps counted per case instead of per customer | Called out explicitly in the rule and in P7. This is the single most common way recovery tooling becomes harassment. |
| Mandate budget race under concurrent workers | P5 runs parallel workers against one mandate. Reserve atomically before the gateway call, not after. |
| Rule interactions produce surprising verdicts | Golden fixtures cover boundaries; evaluation order is documented and tested. |
