# ADR-0002 — Modular monolith over microservices

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

The pipeline has six clearly separable stages. The reflex is to make each one a
service. This is a solo build at modest scale with hard transactional
requirements between state changes and audit writes.

## Decision

One deployable API service with **strictly enforced internal module boundaries**,
plus separate worker and scheduler processes sharing the same codebase. Boundaries
enforced by `import-linter` contracts in CI.

## Rationale

Domain invariant I4 requires every state transition to write exactly one audit
event. Across a service boundary that becomes a distributed transaction — a saga,
an outbox per service, or accepted inconsistency. In one process it is a database
transaction.

Microservices buy independent scaling and independent deployment. Neither is
needed here: there is one deployer, and no stage has scaling needs different
enough to justify the cost.

What microservices genuinely provide is *enforced* boundaries. `import-linter`
provides the same enforcement at build time without the runtime cost. A boundary
violation fails CI exactly as an unavailable service would fail a call — but at
compile time, where it is far cheaper to fix.

## Alternatives rejected

**Microservices per stage.** Distributed transactions for the audit invariant,
network failure modes, six deployments, and no scaling benefit at this size.

**Serverless functions.** Cold starts hurt webhook latency (TR-41), and the
durable outbox pattern fits poorly with ephemeral compute.

**A monolith without enforced boundaries.** Would drift into a tangle within
weeks, and would make the "policy cannot import a model" guarantee unenforceable.
That guarantee is the product's central claim, so structural enforcement is a
requirement rather than a nicety.

## Consequences

**Positive.** Transactional integrity between state and audit. One deploy, one log
stream, one trace. Trivially debuggable. Boundaries still enforced.

**Negative.** All stages scale together. A slow diagnosis stage occupies capacity
the executor could otherwise use.

**Mitigation.** Because boundaries are strict and dependencies point inward,
extracting a module later is mechanical rather than a rewrite. The
[scaling path](../02-technical/ARCHITECTURE.md#91-scaling-path-documented-not-built)
documents the thresholds at which that becomes worthwhile.
