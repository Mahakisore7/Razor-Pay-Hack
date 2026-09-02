# ADR-0006 — Mandatory randomised holdout

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

Recovery products report *recovery rate*: recovered amount over at-risk amount.
The number is easy to compute and nearly impossible to interpret, because a large
share of at-risk payments recover with no intervention at all. A customer whose
card declined on the 28th often pays on the 1st regardless.

A system with no recovery logic — one that detects failures and waits — reports a
recovery rate in the 25–35% range on a realistic cohort.

## Decision

Every benchmark run assigns cases to three arms: **10% control** (detected,
diagnosed, never acted on), **10% baseline** (naive fixed retry plus generic
dunning), **80% treatment** (full Recoup). The headline metric is **incremental
recovery** — treatment minus control — reported with a 95% confidence interval.

The holdout is not optional and not configurable to zero.

## Rationale

Without a control arm there is no way to distinguish the system's contribution
from the counterfactual. Every other number in the report depends on this one being
real.

The baseline arm matters as much as the control. Beating "do nothing" is a low bar.
Beating "what a competent engineer builds in a weekend" is the comparison that
tells you whether the diagnosis, the timing policy, and the playbooks contribute
anything beyond a retry cron.

Making the holdout mandatory rather than configurable is deliberate. A configurable
holdout is a holdout that gets set to zero under pressure, at exactly the moment
the numbers most need to be trustworthy.

## Alternatives rejected

**Report recovery rate only.** Industry standard, and uninterpretable. Rejected.

**Historical before/after comparison.** Confounded by seasonality, cohort mix, and
every other change shipped in the window.

**Synthetic counterfactual model.** Predict what would have happened without
intervention. Rejected: the prediction would itself need validation against a
holdout, so it moves the problem rather than solving it.

**Configurable holdout, default 10%.** Rejected for the reason above.

## Consequences

**Positive.** The headline number means something. Confidence intervals make
statistical significance visible. The three-way comparison isolates what the
intelligence contributes from what any retry loop would.

**Negative.** Roughly 10% of at-risk value is deliberately not recovered. On a real
merchant that is real money forgone in exchange for measurement.

**Negative.** The headline number will be substantially smaller than the recovery
rate a competitor would advertise. A treatment arm recovering 42% against a control
recovering 31% yields an incremental 11 points — and 11 is a less impressive
number to put in a pitch than 42.

**Accepted.** Reporting 42% would be reporting mostly other people's work. The
smaller number is the true one, and a reviewer who understands the distinction will
value it more than the larger one. A reviewer who does not is not the audience for
this project.
