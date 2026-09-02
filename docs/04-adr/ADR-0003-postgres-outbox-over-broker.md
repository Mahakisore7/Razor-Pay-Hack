# ADR-0003 — Postgres outbox over a message broker

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

Recovery actions are scheduled minutes to weeks ahead. Execution must survive
process restarts, must not duplicate side effects, and must be auditable. The
conventional answers are a broker (RabbitMQ, SQS), a task queue (Celery, ARQ), or
a durable workflow engine (Temporal).

## Decision

A **Postgres-backed outbox table**, claimed with `SELECT ... FOR UPDATE SKIP
LOCKED`, with a claim TTL for reclaiming orphans, plus a Redis idempotency key per
action.

## Rationale

The decisive argument is **one source of truth**. With a broker, "is this action
pending?" has two possible answers — the broker's and the database's — and after a
partial failure they can disagree. For a system that retries charges, that
disagreement is a double-charge.

The outbox row is also already needed: it is the audit record of what was
scheduled. Making it the queue means the queue and the audit trail cannot diverge.

`SKIP LOCKED` gives correct multi-worker claiming without an external broker.
Postgres is already a required dependency; a broker would add a new one, with its
own failure modes and operational surface, in exchange for throughput this project
does not need.

Temporal was seriously considered. Durable workflow execution is genuinely the
right abstraction for multi-week recovery plans, and it provides replay and
versioning for free. It was rejected on operational weight: a server, a database,
workers, and a programming model, for a workload the outbox handles in about fifty
lines. It becomes the correct choice above roughly 1M actions/day, or when
workflow versioning across long-running cases becomes painful.

## Alternatives rejected

**Celery / ARQ.** No durable scheduling weeks ahead without a separate store,
which reintroduces the two-sources-of-truth problem.

**Cron scanning the database.** Effectively this design, but without safe
multi-worker claiming.

**Temporal.** Right abstraction, wrong scale. Threshold documented above.

## Consequences

**Positive.** One source of truth. Queue state queryable with SQL. Crash recovery
via claim TTL. No new infrastructure. Auditable by construction.

**Negative.** Throughput bounded by Postgres write capacity. Scheduler polling adds
up to one tick of latency. Reimplements a small amount of what a broker gives free.

**Accepted limits.** Polling at 5s is well inside requirements — recovery actions
are scheduled hours ahead, so seconds of scheduling latency are irrelevant.
