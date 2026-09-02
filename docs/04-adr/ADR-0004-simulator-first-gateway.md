# ADR-0004 — Simulator-first gateway abstraction

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

Recoup acts through Razorpay. Benchmarks must be reproducible, tests must not hit
the network, and a reviewer must be able to run the whole thing without a Razorpay
account.

## Decision

One `PaymentGateway` Protocol with two implementations: a **seeded deterministic
simulator (the default)** and a live test-mode client (opt-in). Both verified
against a shared conformance suite.

## Rationale

Reproducibility is a hard requirement (TR-37): the same seed must produce the same
benchmark. That is impossible against a live API.

Reviewability is close behind. A reviewer who must create a Razorpay account and
configure webhooks before seeing anything will not see anything. `make demo` has to
work on a clean clone with no credentials.

Crucially, the simulator is **not a mock**. It models issuer outages, salary-cycle
effects on `insufficient_funds`, diurnal success variation, mandate re-presentation
budgets, and latent customer payment propensity — because those are exactly the
phenomena the diagnosis engine and timing bandit claim to exploit. A simulator
returning uniform random failures would let the system "detect an issuer outage"
that was never simulated, and the benchmark would measure nothing.

The conformance suite exists because a simulator that drifts from the real client's
contract lets us pass tests against fiction.

## Alternatives rejected

**Mock the HTTP layer.** Brittle, couples tests to wire format, and cannot model
the correlated failure structure the diagnosis engine needs.

**Record/replay against real sandbox traffic.** Reproducible, but cannot generate
the counterfactuals a three-arm benchmark requires — there is no recording of what
would have happened in the control arm.

**Live sandbox only.** Non-reproducible, network-dependent, rate-limited, and
raises the barrier for a reviewer to nearly prohibitive.

## Consequences

**Positive.** Fully reproducible benchmarks. Fast offline tests. Zero-credential
demo. Failure modes injectable on demand.

**Negative.** **Results are only as meaningful as the simulator's realism.** This
is the central limitation of the entire project, and it is stated as such rather
than buried.

**Mitigation.** Every simulator parameter is published in the benchmark report, and
claims are strictly limited to the *internal* comparison between arms on the same
generator. Absolute real-world recovery rates are never claimed. A reviewer can read
exactly what world produced the numbers and judge accordingly — which is what makes
a synthetic result honest rather than unfalsifiable.
