# Observability

| Field | Value |
|---|---|
| Document version | 1.0 |
| Stack | OpenTelemetry (traces) · Prometheus (metrics) · structlog (logs) |
| Related | [METRICS §7](../01-product/METRICS-AND-KPIS.md) · [TRD §8](TRD.md) |

---

## 1. The organising idea

Recoup already has an audit log that records everything that happened to every
case. Observability is not a second copy of that — it answers a different
question.

| Question | Answer lives in |
|---|---|
| What happened to this case, and can I prove it? | **Audit log** (durable, hash-chained, indefinite) |
| Why was this slow, and where did the time go? | **Traces** (sampled, short retention) |
| Is the system healthy right now? | **Metrics** |
| What was the process doing at 04:12? | **Logs** |

The link between them is the trace ID: **every audit event carries the
OpenTelemetry trace ID that produced it** (TR-74). An auditor reading a policy
denial from six weeks ago and an engineer debugging a latency spike are looking
at the same identifier from opposite directions.

## 2. Tracing

One span per pipeline stage, with the case ID on every span:

```
webhook.receive
└── event.normalise
    └── detection.run
        └── case.open
            └── diagnosis.run
                ├── diagnosis.slice          (db.statement recorded)
                ├── diagnosis.significance
                └── diagnosis.llm_rank       (model, tokens, cache hit, fallback)
            └── planning.build
                └── planning.timing_policy
            └── execution.attempt
                ├── policy.evaluate          (verdict, rule_id)
                ├── compliance.validate
                └── gateway.call             (provider, endpoint, status)
```

### 2.1 Attributes

| Span | Attributes |
|---|---|
| `webhook.receive` | `provider`, `event_type`, `duplicate` |
| `detection.run` | `leak_class`, `signal_created`, `deduplicated` |
| `diagnosis.llm_rank` | `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `fallback`, `fallback_reason` |
| `policy.evaluate` | `verdict`, `rule_id`, `case_id` |
| `gateway.call` | `provider`, `endpoint`, `status_code`, `simulated` |
| `execution.attempt` | `channel`, `attempt`, `idempotency_hit` |

`cache_read_tokens` on the LLM span is how prompt-cache effectiveness is
monitored in practice — a run where it stays zero means a silent invalidator
crept into the system prompt ([AI-DESIGN §8.2](AI-DESIGN.md)).

`simulated` on gateway spans keeps simulator and live traces distinguishable, so
nobody mistakes a benchmark trace for evidence of a live call.

### 2.2 Sampling

Head sampling at 100% in development and during benchmarks; 10% with tail-based
retention of all errors in a production-shaped deployment. Every trace that
touches a `DENY` or a failed action is always kept — the interesting traces are
the ones where something was refused.

## 3. Metrics

Full list in [METRICS §7](../01-product/METRICS-AND-KPIS.md). The ones that
actually get watched:

### 3.1 Golden signals

| Signal | Metric |
|---|---|
| Traffic | `recoup_signals_detected_total` |
| Errors | `recoup_actions_executed_total{outcome="failed"}` |
| Latency | `recoup_webhook_latency_seconds`, `recoup_case_state_duration_seconds` |
| Saturation | `recoup_outbox_pending`, `recoup_cases_open` |

### 3.2 Domain signals

These are the ones that would actually catch a bad day:

| Metric | Why it matters |
|---|---|
| `recoup_policy_decisions_total{verdict,rule_id}` | A sudden spike in `DENY` on one rule means either an upstream bug or a genuine compliance event. Both need attention. |
| `recoup_llm_schema_failures_total` | Rising fallback rate means the LLM path is degrading while the system silently keeps working — exactly the failure that hides itself. |
| `recoup_action_cost_rupees_total{channel}` | Live spend. Pairs with the daily cap. |
| `recoup_attribution_ambiguous_total` | Contested payment matches. A rise means the attribution rule needs revisiting. |
| `recoup_mandate_representations_total{outcome}` | Budget efficiency in real time. |
| `recoup_outbox_claim_expired_total` | Workers dying mid-action. |

### 3.3 Alerts

| Alert | Condition | Severity |
|---|---|---|
| Quiet-hour violation | `> 0` — ever | **Critical** |
| Cost cap approaching | Daily spend > 80% of cap | Warning |
| LLM fallback rate high | > 20% over 15 min | Warning |
| Outbox backlog | `pending` > 1000 for 5 min | Warning |
| Webhook latency | p95 > 2s for 5 min | Warning |
| Audit chain invalid | Verification failure | **Critical** |
| Gateway circuit open | Breaker open > 2 min | Warning |

Two are critical and both are integrity failures rather than availability ones.
A quiet-hour violation should be impossible — property test P3 asserts it — so if
the metric is ever non-zero, an invariant has been broken and that matters more
than any latency regression.

## 4. Logging

structlog, JSON, one event per line.

```json
{
  "event": "policy_denied",
  "level": "info",
  "timestamp": "2026-09-02T21:04:11.482+05:30",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "case_id": "018f2c...",
  "rule_id": "quiet_hours",
  "channel": "sms",
  "customer_id": "018f2b...",
  "service": "recoup-worker",
  "version": "0.4.2"
}
```

### 4.1 Rules

- **Never log PII.** A structlog processor drops known PII keys and redacts
  anything matching phone or email patterns. Belt and braces: domain objects do
  not contain PII in the first place ([DATA-MODEL §4](DATA-MODEL.md)).
- **Never log credentials.** The settings object masks secrets in `__repr__`, so
  `log.info("config", settings=settings)` cannot leak a key.
- **Always include `trace_id`.** A log line that cannot be correlated to a trace
  is a log line that will not help at 2 AM.
- **Log decisions, not narration.** `policy_denied` with a `rule_id` is useful.
  "Processing case..." is noise.

### 4.2 Levels

| Level | Use |
|---|---|
| `debug` | Development only |
| `info` | State transitions, decisions, executed actions |
| `warning` | Degraded but handled: LLM fallback, circuit open, claim expiry |
| `error` | Unhandled failure requiring investigation |
| `critical` | Integrity violation: audit chain invalid, quiet-hour breach |

`warning` is where the interesting things live. An LLM fallback is not an error —
the system handled it correctly — but a *pattern* of them is a signal.

## 5. Dashboards

Three, matching the three audiences from the PRD:

**Operations (Priya)** — at-risk by leak class, case pipeline funnel, recovery
rate trend, approval queue depth, active alerts.

**Economics (Arjun)** — spend by channel, cost per rupee recovered, ROI trend,
daily cap headroom, per-playbook profitability.

**Compliance (Meera)** — denials by rule over time, contact fatigue distribution,
opt-out rate, quiet-hour violations (expected: a flat zero line), consent
coverage.

Meera's dashboard being a first-class surface rather than an admin page is a
product statement: the system's refusals are as much a deliverable as its
recoveries.

## 6. What is built versus documented

Honest about scope:

| Capability | Status |
|---|---|
| Structured JSON logging with trace correlation | **Built** |
| Prometheus metrics endpoint | **Built** |
| OpenTelemetry spans across the pipeline | **Built** |
| Health and readiness endpoints | **Built** |
| Grafana dashboards | **Documented, JSON exported, not hosted** |
| Alertmanager routing | **Documented, not deployed** |
| Log aggregation (Loki/ELK) | **Not built** — logs go to stdout |
| Trace backend | **Local Jaeger via compose**, no hosted collector |

The instrumentation is real and exported. The hosted collection tier is a
deployment concern this project does not have, and the docker-compose stack
includes Jaeger and Prometheus so a reviewer can see traces and metrics locally
without pretending there is production infrastructure behind them.
