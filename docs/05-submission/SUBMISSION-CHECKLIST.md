# Submission Checklist

The application form asks for exactly 12 things. This is the prepared answer set.

---

## About you

| # | Field | Answer |
|---|---|---|
| 1 | Full name | *(fill in)* |
| 2 | College | *(fill in)* |
| 3 | Graduation year | *(fill in)* |
| 4 | In person from September | *(yes / no)* |
| 5 | 6 or 12 months | *(pick)* |
| 6 | Resume file | *(attach)* |

## About the build

| # | Field | Answer |
|---|---|---|
| 7 | Track | **03 — AI Revenue Recovery** |
| 8 | Project name | **Recoup** |
| 9 | What it solves | *(draft below)* |
| 10 | GitHub repo URL | `https://github.com/Mahakisore7/Razor-Pay-Hack` — **must be public** |
| 11 | 5-min pitch video | *(unlisted link — see [PITCH-SCRIPT](PITCH-SCRIPT.md))* |
| 12 | What broke, and how you got out | *(from [FAILURE-LOG](FAILURE-LOG.md))* |

---

## Field 9 — What it solves *(draft)*

> Indian merchants lose revenue to failed payments, failed mandate debits, halted
> subscriptions, abandoned checkouts and overdue invoices. The tools that exist
> treat this as a scheduling problem — retry on a cron, send three dunning emails
> — and report a metric that cannot distinguish their contribution from customers
> who would have paid anyway.
>
> Recoup is a revenue recovery control plane. It diagnoses *why* revenue is at
> risk using slice statistics with significance testing, chooses a bounded
> intervention from a versioned playbook, executes it through a deterministic
> compliance gate that no model can override, and measures what it recovered
> against a randomised holdout.
>
> The headline number is incremental recovery, not recovery rate, because a
> system that does nothing reports a 30% recovery rate. Every action is gated,
> every denial is audited, and the report ships its exception list and its costs
> at the same prominence as its wins.

*(Trim to the form's limit. Keep the holdout sentence — it is the differentiator.)*

---

## Field 12 — What broke *(guidance)*

Pick **two or three** entries from FAILURE-LOG. Choose for what they reveal, not
for how dramatic they were:

1. One that was **subtle and dangerous** — a race condition, a silent
   non-determinism, a compliance gap that tests initially missed.
2. One that **changed the design**, not just the code — where the fix was a new
   invariant, property test, or structural constraint.
3. Optionally, one where **something we built did not work** — a component that
   lost to its baseline, reported honestly rather than quietly removed.

The third kind is the most valuable and the least commonly submitted. A candidate
who says "the bandit lost to the fixed schedule, here is the measurement, here is
what I concluded" demonstrates more than one who reports only successes.

Write it as: symptom → investigation → root cause → fix → **what it changed**.

---

## Pre-submission gate

Do not submit until every box is checked.

### Repository
- [ ] Public
- [ ] CI green on `main`
- [ ] `v1.0.0` tagged, CHANGELOG generated
- [ ] README leads with what it is and the headline number
- [ ] **Clean-clone `make demo` verified by a third party on a different machine**
- [ ] No secret in any commit, across full history (`gitleaks detect --log-opts="--all"`)
- [ ] LICENSE present

### Claims
- [ ] Every README claim matches what the code does
- [ ] Simulated components are labelled simulated — especially SMS
- [ ] Benchmark numbers in README, video, and committed report are identical
- [ ] Report states its limitations before its headline
- [ ] Exception list complete, not truncated
- [ ] Negative results reported, not omitted

### Video
- [ ] Under 5 minutes
- [ ] Shows working software, not slides
- [ ] Shows a policy **denial**, not only successes
- [ ] Shows the control arm and explains why it exists
- [ ] Audio audible; screen text legible at 1080p
- [ ] Uploaded unlisted, link tested in a private window

### Documents
- [ ] FAILURE-LOG has real dated entries with actual error output
- [ ] ADRs record the decisions that were actually made
- [ ] Architecture diagram current

---

## What the reviewers said they read

From the buildathon materials, in their words:

| They said | Where we answer it |
|---|---|
| "a repo that actually runs" | `make demo`, clean-clone verified, CI green |
| "a 5-minute video of it working" | [PITCH-SCRIPT](PITCH-SCRIPT.md) — live software, no slides |
| "what broke at 2 AM, and how you got out" | [FAILURE-LOG](FAILURE-LOG.md), written during the build |
| "Problem taste — did you pick something that actually matters" | [VISION](../00-overview/VISION.md) §1 |
| "Build quality — does it run, is it structured, would you trust it" | Policy gate, audit chain, property tests, [DEFINITION-OF-DONE](../03-delivery/DEFINITION-OF-DONE.md) |
| "AI judgment — the right tool in the right place, and where you chose not to use one" | [AI-DESIGN](../02-technical/AI-DESIGN.md) — the 18-stage decision table |
| "Failure recovery — what broke, and what you did about it" | FAILURE-LOG, plus the kill switch and graceful degradation in the product itself |

The last row is worth noting: "failure recovery" is answered twice — once in the
log about *our* failures, and once in the product, which is designed to handle
*its own* failures gracefully. Both are relevant, and the second is the stronger
answer.
