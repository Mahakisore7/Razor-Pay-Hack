# Recoup — Vision

> **Recoup** is a revenue recovery control plane for Indian payments.
> It finds money that is slipping away, works out *why*, chooses a bounded
> intervention, executes it under a compliance gate that no model can override,
> and then proves — against a holdout control group — how much it actually recovered.

- **Track:** 03 — AI Revenue Recovery (Razorpay /buildathon)
- **Status:** Design complete, implementation phased
- **Owner:** Mahakisore

---

## 1. The thesis

Indian digital payments fail a lot, and most of that failure is recoverable.

A merchant on Razorpay loses revenue through at least six distinct leaks, and
almost every one of them is treated today as an accepted cost of doing business:

| Leak | What actually happens | Typical handling today |
|---|---|---|
| Failed one-time payment | UPI timeout, bank downtime, issuer decline | Customer is shown "try again" and leaves |
| Failed mandate debit | UPI Autopay / eNACH presentation fails | Fixed cron retries, same time every day, until the retry budget burns out |
| Halted subscription | Consecutive debit failures trip the halt | Generic dunning email, then churn |
| Abandoned checkout | Drop-off at method selection, OTP, or PSP redirect | Nothing, or one templated email blast |
| Overdue B2B receivable | Invoice sails past due date | A human chases it, badly and inconsistently |
| Success-rate degradation | One issuer, BIN range, or PSP route silently starts failing | Noticed hours later, in a dashboard, by someone |

Each of these is individually small. In aggregate, for a mid-size merchant, this
is the difference between a good quarter and a bad one.

**The problem is not detection.** Every payment gateway can already tell you a
payment failed. The problem is everything after detection: knowing *why* it
failed, knowing whether it is worth chasing, knowing *when* and *how* to chase
it, knowing when to stop, and being able to prove afterwards that the chasing
caused the recovery rather than coincided with it.

That gap — between a failure event and a defensible, measured recovery — is what
Recoup closes.

## 2. Why the naive version of this is wrong

Almost every "payment recovery" or "dunning" tool on the market is one of two things:

**A retry cron.** Retry the charge at T+1h, T+24h, T+72h. Fixed schedule for
everyone. This ignores that an `insufficient_funds` decline on the 28th of the
month and an `issuer_down` failure at 2 AM are completely different problems with
completely different optimal retry times. It also burns through a UPI Autopay
mandate's limited re-presentation budget on retries that were never going to work.

**A dunning email sequence.** Send three increasingly urgent emails. This ignores
consent, quiet hours, channel cost, contact fatigue, and the fact that for a
₹149 subscription you have just spent more on outreach than the invoice is worth.

Both share a deeper flaw: **neither can tell you whether it worked.** They report
"recovery rate" — the fraction of at-risk revenue that eventually arrived. That
number is close to meaningless, because a large share of those customers would
have paid anyway. Without a control group, a recovery tool that does literally
nothing can report a 30% recovery rate and sound successful.

## 3. What Recoup does differently

Three commitments, and everything in the architecture follows from them.

### 3.1 Diagnosis before action

Recoup never jumps from *signal* to *action*. Every case passes through a
diagnosis stage that produces a **root cause with evidence attached** — not a
guess, but a ranked hypothesis grounded in pre-computed statistics over
comparable cases. A drop in success rate gets sliced by issuer, BIN range, PSP
route, instrument, and app version, with a significance test on each slice,
*before* any language model is allowed to have an opinion about it.

The diagnosis determines the playbook. `issuer_down` means wait for the issuer to
recover and retry — messaging the customer is pure noise and pure cost.
`insufficient_funds` means time the retry to the customer's inferred salary
cycle. `mandate_revoked` means stop retrying entirely and ask for re-authorisation.
These are different problems and they deserve different responses.

### 3.2 A policy gate the model cannot argue with

Every single outbound action — a retry, an SMS, a call, a payment link — is
evaluated by a deterministic policy engine before it executes. The engine is
plain code and plain rules. It has no LLM in it, by design, and it cannot be
persuaded, prompt-injected, or reasoned around.

It enforces mandate re-presentation caps, contact frequency limits, TRAI-aligned
quiet hours, consent and DND state, per-case cost ceilings (never spend more
chasing a rupee than the rupee is worth), amount thresholds requiring human
approval, and the stopping rules — including the ones that matter most: a
customer who opts out is never contacted again, and a case with a dispute or
chargeback filed against it halts immediately.

Crucially, **every denial is recorded** with the rule that fired. The audit trail
shows not only what Recoup did, but what it deliberately declined to do and why.
For a system that touches money, the second list is the more important one.

### 3.3 Honest measurement, or it doesn't count

Recoup holds out a randomised control group on every batch. Cases in the holdout
are detected, diagnosed, and recorded — and then nothing happens to them.

The headline metric is therefore not recovery rate. It is **incremental
recovery**: money recovered in the treated arm minus money recovered in the
holdout arm, scaled, with a confidence interval. Alongside it sits **cost per
rupee recovered**, so a recovery that cost more than it returned is visible as
the failure it is, and an **honest exception list** of cases the system could not
resolve and why.

A benchmark that only reports its wins is a demo. A benchmark that reports its
losses, its costs, and its blocked actions is a product.

## 4. Where AI belongs — and where it does not

This deserves to be stated plainly, because getting it wrong is the most common
failure mode in agentic fintech.

**AI is used where the problem is genuinely one of judgment over unstructured or
ambiguous input:** ranking and narrating root-cause hypotheses over pre-computed
statistics, proposing playbooks for failure codes never seen before, and
generating customer-facing copy in Hinglish and Indian vernaculars that a
template library could not cover.

**AI is deliberately absent from everything that must be exactly right:** signal
detection, statistical aggregation, retry-time selection, the policy gate,
message compliance validation, and outcome attribution. These are arithmetic,
rules, and calibrated statistics. A language model would make them less reliable,
less reproducible, and less defensible, in exchange for nothing.

The retry timing policy is a contextual bandit, not a prompt, because the
question "when should I retry this specific failed mandate" is a question about
calibrated probability, not language. The compliance validator is a hand-written
checker, not a model, because "is this message legal to send" must never have a
temperature parameter.

The full mapping, with reasoning for each decision, is in
[AI-DESIGN.md](../02-technical/AI-DESIGN.md).

## 5. What "done" looks like

Recoup is finished, for the purposes of this build, when a reviewer can:

1. Clone the repo, run `make demo`, and watch a batch of 2,000 synthetic at-risk
   cases flow end to end with no Razorpay credentials required.
2. Read a benchmark report showing incremental recovery against a control arm and
   against a naive fixed-retry baseline, with confidence intervals and cost.
3. Open any single case and see its complete, hash-chained timeline: signal,
   evidence, diagnosis, plan, every policy decision including the denials, every
   action, and the attributed outcome.
4. Add Razorpay test-mode keys and watch the same pipeline drive real test-mode
   payments, subscriptions, and payment links.
5. Trip the kill switch mid-batch and watch every in-flight action stop safely.

## 6. Non-goals

Stated explicitly, so scope stays honest:

- **Not a collections agency.** No legal escalation, no credit bureau reporting,
  no third-party recovery handoff.
- **Not a fraud or chargeback defence system.** That is Track 02. Recoup halts on
  dispute; it does not fight it.
- **Not a PSP or a router.** Recoup observes and acts through Razorpay; it does
  not move money itself.
- **Not a general marketing automation tool.** Every message Recoup sends is tied
  to a specific at-risk amount. No campaigns, no broadcasts.
- **Not a replacement for human judgment above the approval threshold.** High-value
  cases are gated to a human by design, not by limitation.

## 7. Reading order

| Document | What it answers |
|---|---|
| [PRD](../01-product/PRD.md) | What we are building and for whom |
| [TRD](../02-technical/TRD.md) | What it must technically do |
| [ARCHITECTURE](../02-technical/ARCHITECTURE.md) | How the pieces fit |
| [DOMAIN-MODEL](../02-technical/DOMAIN-MODEL.md) | The entities and state machines |
| [AI-DESIGN](../02-technical/AI-DESIGN.md) | Where AI is and is not used |
| [POLICY-ENGINE](../02-technical/POLICY-ENGINE.md) | The rules that bound every action |
| [METRICS](../01-product/METRICS-AND-KPIS.md) | How we prove it worked |
| [ROADMAP](../03-delivery/ROADMAP.md) | The build order |
