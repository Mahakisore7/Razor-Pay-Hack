# ADR-0001 — Track 03, and the incremental-recovery thesis

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

The Razorpay buildathon offers five tracks, each with a stated bar. Track 03's is:
*"Don't just identify the problem. Show measured money recovered across a batch,
with compliant escalation, stopping rules, and an audit trail."*

Judging is on problem taste, build quality, AI judgment, and failure recovery.

## Decision

Build for **Track 03 — AI Revenue Recovery**, with the product thesis that the
differentiator is not detection or intervention but **honest measurement of
incremental recovery against a randomised control**.

## Rationale

Track 03 is the only track whose bar forces a hard number out of the build, which
is the strongest available answer to both problem taste and build quality.

It is a genuine closed loop — detect, diagnose, decide, act, measure — so it
exercises agentic reasoning *and* deterministic guardrails. Its explicit demands
for stopping rules and compliant escalation are places where the right answer is
*not* a model, which is precisely the AI-judgment criterion.

The thesis follows from a flaw in how this problem is normally solved. Recovery
rate is the industry-standard metric and it is close to meaningless, because a
large fraction of at-risk payments recover unaided. A tool that does nothing can
report 30% and sound successful. Holding out a randomised control arm is the only
way to know what the system contributed, and almost nothing in this space does it.

## Alternatives rejected

**Track 01 (Agentic Commerce).** The most on-trend and therefore the most crowded.
Differentiating against many similar submissions is harder than being excellent in
a less contested track.

**Track 02 (Risk Manager).** Lives or dies on dataset quality. Honest
precision/recall on synthetic data is a weak claim, and sourcing credible labelled
Indian fraud data was not feasible in the time available.

**Track 04 (Finance Controller).** Very demoable match-rate number, but
reconciliation is a matching problem more than an agentic one, leaving less room
to demonstrate judgment about where AI belongs.

## Consequences

**Positive.** A defensible headline number. A natural fit for compliance depth. A
less contested field.

**Negative.** The holdout arm means deliberately *not* recovering roughly 10% of
at-risk value, so the gross recovery number is lower than it could be. Accepted: a
smaller number that is trustworthy beats a larger one that is not.

**Risk.** The measurement doctrine only means something if it is applied to our own
claims as well. If the treatment arm does not beat baseline, the report must say so.
