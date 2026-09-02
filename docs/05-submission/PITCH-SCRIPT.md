# Pitch Script — 5 minutes

| Field | Value |
|---|---|
| Length | 5:00 hard cap |
| Format | Screen recording, live software, voiceover |
| Slides | **None** — the brief asks for a video of it working |

---

## Principle

They said they read "a repo that actually runs" and "a 5-minute video of it
working." So the video is the software running, narrated. No title cards, no
architecture animations, no team photo.

The one thing most submissions will not show: **the system refusing to do
something.** That gets 45 seconds here, because it is the product.

---

## Timing

| Segment | Time | Content |
|---|---|---|
| 1 | 0:00–0:35 | The problem, with a number on screen |
| 2 | 0:35–1:00 | What Recoup is, in one sentence |
| 3 | 1:00–2:15 | One case, end to end, live |
| 4 | 2:15–3:00 | **The denial** — what it refused to do |
| 5 | 3:00–4:05 | The benchmark and the control arm |
| 6 | 4:05–4:35 | Where AI is, and where it deliberately is not |
| 7 | 4:35–5:00 | What broke |

---

## 1 — The problem *(0:00–0:35)*

*Screen: the dashboard, at-risk value by leak class.*

> A merchant on Razorpay loses revenue six ways: failed payments, failed mandate
> debits, halted subscriptions, abandoned checkouts, overdue invoices, and
> success-rate degradation nobody noticed for four hours.
>
> The tools that exist retry on a cron and send three dunning emails. Then they
> report a recovery rate — and that number is close to meaningless, because a
> third of these customers would have paid anyway. A system that does *nothing*
> reports a 30% recovery rate.

**Note:** lead with the flaw in the incumbent metric. It frames everything after
it, and it is the thesis.

## 2 — What it is *(0:35–1:00)*

*Screen: the architecture diagram, briefly.*

> Recoup is a revenue recovery control plane. It diagnoses why revenue is at risk,
> chooses a bounded intervention, executes it through a compliance gate that no
> model can override, and measures what it actually recovered against a randomised
> holdout.

Ten seconds on the diagram. No longer. Back to live software.

## 3 — One case, end to end *(1:00–2:15)*

*Screen: case timeline, live.*

> Here is a real case. ₹2,499, UPI mandate debit, `insufficient_funds`.
>
> Diagnosis first — not a guess. It sliced comparable failures by issuer, BIN
> range, PSP route, and instrument, ran a significance test on each, and found no
> issuer outage. So this is a genuine funds problem, not infrastructure. That
> distinction decides everything downstream: an issuer outage means wait, and
> messaging the customer is pure cost.
>
> The playbook schedules a retry timed to the customer's inferred salary cycle,
> plus a payment link by SMS. Every step is gated.
>
> The customer paid 31 hours later. Attribution matched on customer and amount
> inside the window and credited the SMS step.
>
> Fourteen audit events, hash-chained. This is the whole history.

*Scroll the timeline slowly. Let the density register.*

## 4 — The denial *(2:15–3:00)*

*Screen: scroll to the red `policy_denied` entry.*

> This is the part I would look at first.
>
> Recoup wanted to send a second SMS. It didn't. The frequency cap — three
> contacts across all channels in seven days, counted across *every* case for this
> customer, not per case — was already met.
>
> The denial is recorded with the rule that fired and the exact inputs the engine
> saw. So the audit trail shows not just what the system did, but what it refused
> to do and why.

*Screen: `.importlinter` contract.*

> And the policy engine cannot be argued with, because it structurally cannot
> reach a language model. That is a lint rule. It fails the build.

*Screen: trip the kill switch. Show execution stopping.*

> One switch, everything stops, nothing is lost.

## 5 — The benchmark *(3:00–4:05)*

*Screen: benchmark report.*

> Two thousand cases, three arms. Eighty percent get the full system. Ten percent
> get a naive fixed-retry cron with generic dunning — what a competent engineer
> builds in a weekend. And ten percent get **nothing at all**.
>
> That last ten percent is why any of these numbers mean something.
>
> Recovery rate in the control arm: *[X]%*. Those people paid on their own.
> Treatment: *[Y]%*. The number I'm claiming is the difference — *[Z]* — with a
> confidence interval.

*Screen: scroll to cost, then to exceptions.*

> Cost per rupee recovered, because recovery that costs more than it returns is a
> loss reported as a win. And the full exception list — every case it could not
> resolve, with the reason. Not truncated.
>
> Same seed, same numbers, every time. You can re-run it.

## 6 — Where AI is, and is not *(4:05–4:35)*

*Screen: the decision table from AI-DESIGN.*

> Eighteen stages. The model is used in four.
>
> It ranks root-cause hypotheses — deciding a BIN-range slice is downstream of an
> issuer slice is real payments reasoning over correlated evidence. And it writes
> Hinglish copy, which templates can't cover.
>
> It is deliberately absent from detection, the statistics, retry timing, the
> policy gate, compliance validation, and attribution. Those need to be exactly
> right, and a model would make them less reliable in exchange for nothing.
>
> `make test-no-llm` runs the whole suite with the model raising on every call.
> It passes. The model contributes quality. It never contributes correctness.

## 7 — What broke *(4:35–5:00)*

*Screen: FAILURE-LOG.*

> *[Pick the best one. Symptom, root cause, and what it changed about the design.]*
>
> *[If a component lost to its baseline, say so here. It is the strongest thing
> you can say.]*
>
> It's all in the repo. `make demo`, no credentials needed.

---

## Production notes

| | |
|---|---|
| Record at 1080p; verify text is legible when scaled down | |
| Pre-run everything — no live loading spinners | |
| Script it, rehearse twice, then speak naturally | |
| Cut segment 2 before cutting segment 4 if over time | |
| Audio matters more than video; use a real microphone | |
| Test the unlisted link in a private window before submitting | |

## What to cut if over 5:00

In order:

1. The architecture diagram in segment 2 (drop to one sentence)
2. Half of segment 6 (the table speaks for itself on screen)
3. The kill-switch demo in segment 4

**Never cut:** the denial (segment 4) or the control arm (segment 5). Those are
the two things that distinguish this from every other submission.
