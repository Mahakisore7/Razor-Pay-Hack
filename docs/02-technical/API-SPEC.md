# API Specification

| Field | Value |
|---|---|
| Document version | 1.0 |
| Base path | `/api/v1` |
| Spec | OpenAPI 3.1, generated at `/openapi.json` |
| Related | [TRD §7](TRD.md) · [DOMAIN-MODEL](DOMAIN-MODEL.md) |

This document covers conventions and the contracts that are not obvious from the
generated schema. The generated OpenAPI is authoritative for field-level detail.

---

## 1. Conventions

### 1.1 Money

```json
{ "paise": 249900, "currency": "INR" }
```

Never a decimal string, never a float, at any boundary. A client that wants
"₹2,499.00" formats it; the wire format stays exact. This costs a little
ergonomics and removes an entire class of rounding bug.

### 1.2 Timestamps

RFC 3339 with explicit offset: `2026-09-02T16:30:00+05:30`. Naive datetimes are
rejected at the boundary with a 422 rather than silently assumed UTC.

### 1.3 Pagination

Cursor-based. Offset pagination over a table receiving concurrent inserts skips
and repeats rows.

```json
{ "items": [...], "next_cursor": "eyJpZCI6...", "has_more": true }
```

### 1.4 Errors — RFC 9457

```json
{
  "type": "https://recoup.dev/errors/policy-denied",
  "title": "Action denied by policy",
  "status": 409,
  "detail": "Contact frequency cap exceeded for customer in 7d window",
  "instance": "/api/v1/cases/018f.../actions/018f...",
  "rule_id": "frequency_cap",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

`trace_id` on every error, so a user-reported failure maps to a trace without
guesswork.

### 1.5 Idempotency

Mutating endpoints accept `Idempotency-Key`. A repeat with the same key returns
the original response rather than acting twice.

## 2. Endpoints

### 2.1 Webhooks

```
POST /webhooks/razorpay
```

Unauthenticated by design — authenticity comes from the HMAC signature, not from
a bearer token. Returns 200 after durable raw write (TR-2). Returns 400 on
signature mismatch, and **never** reveals why: a verbose signature error is an
oracle.

### 2.2 Cases

```
GET  /api/v1/cases                      # filter: state, arm, leak_class, date range
GET  /api/v1/cases/{id}
GET  /api/v1/cases/{id}/timeline        # full audit chain, chronological
GET  /api/v1/cases/{id}/timeline/stream # SSE, live updates
GET  /api/v1/cases/{id}/diagnosis
GET  /api/v1/cases/{id}/plan
GET  /api/v1/cases/{id}/policy-decisions # includes DENY and DEFER
POST /api/v1/cases/{id}/suppress        # manual stop; actor recorded
```

The timeline response is the product's centrepiece. It returns **every** event
including denials, so the answer to "what did it do and what did it refuse to do"
is a single request:

```json
{
  "case_id": "018f...",
  "at_risk": { "paise": 249900, "currency": "INR" },
  "state": "recovered",
  "arm": "treatment",
  "chain_valid": true,
  "events": [
    { "seq": 1, "kind": "signal_detected",  "occurred_at": "...", "payload": {...} },
    { "seq": 2, "kind": "arm_assigned",     "occurred_at": "...", "payload": {"arm": "treatment"} },
    { "seq": 6, "kind": "policy_denied",    "occurred_at": "...",
      "payload": {"rule_id": "frequency_cap", "inputs": {"contacts_7d": 3, "cap": 3}} },
    { "seq": 7, "kind": "action_executed",  "occurred_at": "...",
      "payload": {"channel": "sms", "cost": {"paise": 18, "currency": "INR"}} },
    { "seq": 9, "kind": "payment_attributed", "occurred_at": "...", "payload": {...} }
  ]
}
```

`chain_valid` is computed on read by verifying the hash chain. A tampered log
surfaces in the UI rather than requiring someone to run a CLI command.

### 2.3 Approvals

```
GET  /api/v1/approvals                  # pending queue
POST /api/v1/approvals/{case_id}/approve
POST /api/v1/approvals/{case_id}/reject   # body: { "reason": "..." }
```

Both write audit events with the authenticated actor. Rejection requires a
reason — an unexplained rejection is not auditable.

### 2.4 Kill switch

```
GET    /api/v1/kill-switch
POST   /api/v1/kill-switch              # body: { "scope": "global"|"playbook:<id>", "reason": "..." }
DELETE /api/v1/kill-switch/{scope}
```

The only runtime-mutable control in the system. It can only ever make Recoup do
*less*, which is why it is safe to expose while every policy threshold requires a
reviewed PR.

Reason is mandatory on trip. Actor recorded on both trip and clear.

### 2.5 Dashboard

```
GET /api/v1/dashboard/summary           # ?from=&to=  at-risk, recovered, open, by leak class
GET /api/v1/dashboard/leaks             # breakdown by leak class and arm
GET /api/v1/dashboard/economics         # cost, cost-per-rupee, ROI
GET /api/v1/dashboard/compliance        # denials by rule, contact fatigue, opt-outs
```

`/compliance` exists as a first-class dashboard endpoint, not an admin
afterthought. Meera's view is a product surface.

### 2.6 Benchmarks

```
POST /api/v1/bench/runs                 # { "seed": 42, "cohort_size": 2000 }
GET  /api/v1/bench/runs
GET  /api/v1/bench/runs/{id}
GET  /api/v1/bench/runs/{id}/report     # ?format=md|json
GET  /api/v1/bench/runs/{id}/exceptions  # full list, never truncated
```

`/exceptions` returns every unresolved case with its reason code. It is paginated
but not sampled — the honest exception list is a deliverable
([METRICS §8](../01-product/METRICS-AND-KPIS.md)), and an API that truncated it
would undermine the point.

### 2.7 Operations

```
GET /health/live      # process alive
GET /health/ready     # DB + Redis reachable
GET /metrics          # Prometheus
```

Liveness and readiness are distinct. A single `/health` that checks dependencies
causes orchestrators to kill a healthy process during a brief database blip.

## 3. Authentication

| Surface | Mechanism |
|---|---|
| Webhooks | HMAC signature only |
| Console API | Session cookie, `SameSite=Lax`, `HttpOnly`, `Secure` |
| CLI / service | Bearer token |
| `/health/*` | None |
| `/metrics` | Network-restricted |

Roles: `viewer` (read), `operator` (approve, kill switch), `admin` (config).
Every state-changing call records the actor in the audit log — an approval
without an attributable human is not an approval.

## 4. Streaming

`GET /api/v1/cases/{id}/timeline/stream` — Server-Sent Events.

```
event: audit
data: {"seq": 12, "kind": "action_executed", ...}

event: heartbeat
data: {"ts": "2026-09-02T16:30:00+05:30"}
```

SSE rather than WebSocket: the stream is one-directional, SSE reconnects on its
own, and it survives proxies that mishandle upgrade requests. Heartbeats every
15s keep intermediaries from timing out an idle connection.

## 5. Versioning

`/api/v1` in the path. Additive changes ship in place; breaking changes get
`/v2` with both served during a deprecation window. Sunset dates are announced
via the `Sunset` header.

## 6. Rate limits

| Surface | Limit |
|---|---|
| Webhooks | 1000/min (well above expected delivery) |
| Console read | 300/min per session |
| Console write | 60/min per session |
| Bench runs | 5/hour |

429 responses carry `Retry-After`.
