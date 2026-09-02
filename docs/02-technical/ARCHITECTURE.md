# Architecture — Recoup

| Field | Value |
|---|---|
| Document version | 1.0 |
| Status | Approved for build |
| Related | [TRD](TRD.md) · [DOMAIN-MODEL](DOMAIN-MODEL.md) · [ADRs](../04-adr/) |

Diagrams use Mermaid and render natively on GitHub.

---

## 1. Architectural stance

Recoup is a **control plane**. It decides what should happen to at-risk revenue.
Razorpay is the **data plane** — it is the only thing that actually moves money.
Recoup never holds funds, never settles, and never acts outside Razorpay's APIs.

Four properties drive every structural decision:

| Property | Structural consequence |
|---|---|
| **Every side effect is governed** | A single choke point — the Executor — through which all outbound actions pass, with the Policy Gate immediately upstream of it |
| **Everything is replayable** | Raw events persisted before interpretation; an append-only hash-chained audit log as the system of record |
| **Nothing is lost on crash** | Postgres-backed durable outbox with `FOR UPDATE SKIP LOCKED` claiming, not an in-memory queue |
| **Benchmarks are reproducible** | Gateway behind an interface with a seeded simulator implementation; all randomness derived from one seed |

A deliberate consequence: **the LLM sits outside the critical path.** If every
model call failed, cases would still detect, diagnose (by significance ranking),
plan, gate, execute, and attribute. Quality would drop; correctness would not.

## 2. C4 Level 1 — System context

```mermaid
graph TB
    subgraph Humans
        OPS["Priya<br/>Revenue Ops"]
        FIN["Arjun<br/>Finance Controller"]
        CMP["Meera<br/>Compliance"]
        ENG["Dev<br/>Platform Engineer"]
    end

    RECOUP["<b>Recoup</b><br/>Revenue Recovery Control Plane<br/><i>detect - diagnose - decide - act - measure</i>"]

    subgraph External
        RZP["Razorpay<br/><i>test mode</i><br/>payments, subscriptions,<br/>mandates, payment links"]
        LLM["Anthropic Claude API<br/><i>hypothesis ranking,<br/>copy generation</i>"]
        MSG["Messaging providers<br/><i>SMS / WhatsApp / Email</i>"]
        CUST["End customer<br/><i>the person who owes money</i>"]
    end

    OPS -->|monitors, approves| RECOUP
    FIN -->|reads economics| RECOUP
    CMP -->|audits denials| RECOUP
    ENG -->|operates, kill switch| RECOUP

    RECOUP <-->|webhooks in,<br/>retries and links out| RZP
    RECOUP -->|aggregated stats only,<br/>never PII| LLM
    RECOUP -->|gated, validated messages| MSG
    MSG --> CUST
    CUST -->|pays| RZP

    classDef core fill:#1a1a1a,stroke:#e8b04b,stroke-width:3px,color:#fff
    classDef ext fill:#2a2a2a,stroke:#666,color:#ddd
    classDef human fill:#333,stroke:#888,color:#fff
    class RECOUP core
    class RZP,LLM,MSG,CUST ext
    class OPS,FIN,CMP,ENG human
```

## 3. C4 Level 2 — Containers

```mermaid
graph TB
    subgraph Client
        UI["<b>Ops Console</b><br/>Next.js 15 App Router<br/>TypeScript, Tailwind, shadcn/ui"]
    end

    subgraph "Recoup Backend"
        API["<b>API Service</b><br/>FastAPI + Pydantic v2<br/><i>REST, SSE, webhook receiver</i>"]
        WORKER["<b>Worker Pool</b><br/>async Python<br/><i>claims and runs scheduled actions</i>"]
        SCHED["<b>Scheduler</b><br/><i>ticks the outbox,<br/>promotes due actions</i>"]
    end

    subgraph Stores
        PG[("<b>PostgreSQL 16</b><br/>cases, signals, plans,<br/>audit chain, outbox")]
        REDIS[("<b>Redis 7</b><br/>idempotency keys,<br/>rate limits, kill switch")]
    end

    subgraph "Gateway Adapter"
        IFACE["<b>PaymentGateway</b><br/><i>interface</i>"]
        SIM["<b>Simulator</b><br/>seeded, offline,<br/>deterministic"]
        LIVE["<b>Razorpay Client</b><br/>test mode only"]
    end

    subgraph Observability
        OTEL["OpenTelemetry<br/>traces"]
        PROM["Prometheus<br/>metrics"]
        LOGS["structlog<br/>JSON logs"]
    end

    UI -->|REST + SSE| API
    API --> PG
    API --> REDIS
    API -.->|enqueue| PG
    SCHED -->|SKIP LOCKED claim| PG
    SCHED --> WORKER
    WORKER --> PG
    WORKER --> REDIS
    WORKER --> IFACE
    API --> IFACE
    IFACE -.-> SIM
    IFACE -.-> LIVE
    LIVE -->|HTTPS| RZP["Razorpay API"]

    API --> OTEL
    WORKER --> OTEL
    API --> PROM
    WORKER --> PROM
    API --> LOGS
    WORKER --> LOGS

    classDef svc fill:#1a1a1a,stroke:#e8b04b,stroke-width:2px,color:#fff
    classDef store fill:#243b53,stroke:#4a90d9,color:#fff
    classDef adapter fill:#2d2416,stroke:#c9a227,color:#fff
    class API,WORKER,SCHED,UI svc
    class PG,REDIS store
    class IFACE,SIM,LIVE adapter
```

### 3.1 Why these containers

**One API service, not microservices.** At this scale, splitting detection,
diagnosis, and execution into separate deployables would buy nothing and cost
transactional integrity. A case's state transition and its audit event must be
written in one transaction — that is far easier in one process. The module
boundaries are enforced in code (see §5), so extraction later is mechanical.
Recorded as [ADR-0002](../04-adr/ADR-0002-modular-monolith.md).

**Scheduler separate from workers.** The scheduler's job is to promote due
actions from the outbox and hand them off; it is single-purpose and easy to
reason about. Workers are horizontally scalable and stateless.

**Postgres for the queue, not a broker.** Discussed in
[ADR-0003](../04-adr/ADR-0003-postgres-outbox-over-broker.md). Summary: the
outbox is already needed for durability and audit; adding a broker would mean two
sources of truth about whether an action is pending.

## 4. C4 Level 3 — The pipeline

The core is a six-stage pipeline. Each stage has one responsibility and a typed
input and output.

```mermaid
flowchart LR
    EV["Raw Events<br/><i>webhooks, imports</i>"]

    subgraph "1 - Ingestion"
        VER["HMAC verify"]
        RAW["Persist raw"]
        NORM["Normalise<br/>to canonical<br/>decline taxonomy"]
    end

    subgraph "2 - Detection"
        DET["Detectors<br/>L1..L6"]
        DEDUP["Dedupe against<br/>open cases"]
        SIG["Signal"]
    end

    subgraph "3 - Diagnosis"
        SLICE["Slice + aggregate<br/><i>SQL only</i>"]
        TEST["Significance test<br/><i>two-proportion z</i>"]
        RANK["LLM ranks + narrates<br/><i>schema-validated</i>"]
        DX["Diagnosis<br/>+ evidence"]
    end

    subgraph "4 - Planning"
        PB["Select playbook<br/><i>by root cause</i>"]
        TIME["Timing policy<br/><i>contextual bandit</i>"]
        PLAN["Intervention Plan<br/><i>scheduled steps</i>"]
    end

    subgraph "5 - Gate + Execute"
        GATE["<b>Policy Gate</b><br/><i>deterministic</i>"]
        VALID["Compliance<br/>validator"]
        EXEC["Executor<br/><i>idempotent</i>"]
    end

    subgraph "6 - Attribution"
        MATCH["Match inbound<br/>payments"]
        OUT["Outcome"]
    end

    EV --> VER --> RAW --> NORM --> DET --> DEDUP --> SIG
    SIG --> SLICE --> TEST --> RANK --> DX
    DX --> PB --> TIME --> PLAN
    PLAN --> GATE
    GATE -->|ALLOW| VALID --> EXEC
    GATE -->|DENY / DEFER| AUDIT2["Audit only<br/><i>no side effect</i>"]
    EXEC --> MATCH --> OUT

    AUDIT[("Audit chain<br/><i>every stage writes</i>")]
    NORM -.-> AUDIT
    SIG -.-> AUDIT
    DX -.-> AUDIT
    PLAN -.-> AUDIT
    GATE -.-> AUDIT
    EXEC -.-> AUDIT
    OUT -.-> AUDIT

    classDef ai fill:#3d2f14,stroke:#e8b04b,stroke-width:2px,color:#fff
    classDef det fill:#14293d,stroke:#4a90d9,stroke-width:2px,color:#fff
    classDef gate fill:#3d1414,stroke:#d94a4a,stroke-width:3px,color:#fff
    class RANK ai
    class SLICE,TEST,DET,DEDUP,TIME,MATCH det
    class GATE,VALID gate
```

**Legend:** amber = LLM involved · blue = deterministic · red = policy choke point.

Note that amber appears exactly once, and it is downstream of the significance
test and upstream of nothing that touches money.

## 5. Module structure

Enforced by `import-linter` in CI. A violation fails the build.

```
services/core/src/recoup/
├── domain/              # Pure. Entities, value objects, state machines.
│   ├── case.py          #   No I/O. No framework imports. No LLM.
│   ├── signal.py
│   ├── diagnosis.py
│   ├── plan.py
│   ├── action.py
│   ├── outcome.py
│   └── money.py         #   Integer paise. Never float.
│
├── policy/              # Deterministic gate. Depends only on domain.
│   ├── engine.py        #   Evaluate(action, context) -> Decision
│   ├── rules/           #   One file per rule, individually testable
│   └── stopping.py      #   Case-terminating rules
│
├── detection/           # Signal detectors. Depends on domain + repositories.
│   ├── detectors/       #   L1..L6
│   └── changepoint.py   #   CUSUM / EWMA
│
├── diagnosis/           # Slice statistics, significance, LLM ranking.
│   ├── slicer.py        #   SQL aggregation
│   ├── significance.py  #   Hypothesis tests
│   ├── ranker.py        #   LLM call, schema-validated, with fallback
│   └── redaction.py     #   PII stripping before any model call
│
├── planning/            # Playbooks and timing.
│   ├── playbooks/       #   Versioned YAML + loader
│   ├── timing/          #   Contextual bandit, Thompson sampling
│   └── planner.py
│
├── execution/           # The only module permitted outbound side effects.
│   ├── executor.py      #   Asserts an ALLOW exists before acting
│   ├── outbox.py        #   Durable claim/complete
│   ├── channels/        #   sms, whatsapp, email, voice, payment_retry, link
│   └── compliance.py    #   Message validator (deterministic)
│
├── attribution/         # Payment matching, outcome assignment.
│
├── gateway/             # Razorpay abstraction.
│   ├── interface.py     #   Protocol
│   ├── razorpay_client.py
│   └── simulator/       #   Seeded, deterministic
│
├── audit/               # Append-only hash-chained log + verifier.
│
├── bench/               # Cohort generator, arm runner, report writer.
│
├── api/                 # FastAPI. Thin. Depends on everything, owned by nothing.
│
└── platform/            # Config, DB session, telemetry, clock, RNG.
```

### 5.1 Dependency rules

```mermaid
graph BT
    DOM["domain<br/><i>pure</i>"]
    PLAT["platform"]
    POL["policy"]
    DET["detection"]
    DX["diagnosis"]
    PLAN["planning"]
    GW["gateway"]
    EXEC["execution"]
    ATTR["attribution"]
    AUD["audit"]
    BENCH["bench"]
    API["api"]

    POL --> DOM
    DET --> DOM
    DX --> DOM
    PLAN --> DOM
    GW --> DOM
    ATTR --> DOM
    AUD --> DOM
    EXEC --> DOM
    EXEC --> POL
    EXEC --> GW
    EXEC --> AUD
    PLAN --> DX
    BENCH --> DET
    BENCH --> DX
    BENCH --> PLAN
    BENCH --> EXEC
    BENCH --> ATTR
    API --> BENCH
    API --> EXEC
    API --> ATTR

    classDef pure fill:#1a3d1a,stroke:#4ad94a,color:#fff
    class DOM,PLAT pure
```

Hard rules, checked in CI:

1. `domain` imports nothing from Recoup except `domain`. No SQLAlchemy, no
   FastAPI, no `anthropic`, no `datetime.now`.
2. Nothing imports `api`.
3. Only `execution` may perform outbound side effects. Any HTTP client
   instantiation outside `execution`, `gateway`, or `diagnosis.ranker` is a
   violation.
4. `policy` must not import `diagnosis`, `planning`, or anything with an LLM
   client in its transitive closure. The gate cannot be model-influenced, and
   this is enforced structurally rather than by convention.

Rule 4 is the architectural expression of the product's central promise.

## 6. Key runtime flows

### 6.1 Webhook to case

```mermaid
sequenceDiagram
    autonumber
    participant RZP as Razorpay
    participant API as API Service
    participant PG as Postgres
    participant DET as Detection
    participant DX as Diagnosis

    RZP->>API: POST /webhooks/razorpay
    API->>API: verify HMAC (reject on fail)
    API->>PG: INSERT raw_event (ON CONFLICT DO NOTHING)
    Note over API,PG: idempotent on razorpay event id
    API-->>RZP: 200 OK (fast ack, < 2s)

    API->>DET: normalise + detect (async)
    DET->>PG: check open case for (customer, amount)
    alt duplicate
        DET->>PG: append audit: signal_deduplicated
    else new
        DET->>PG: INSERT signal, INSERT case (DETECTED)
        DET->>PG: assign arm from seeded hash(case_id)
        DET->>DX: enqueue diagnosis
    end
```

The fast ack matters: Razorpay retries webhooks it considers failed, and a slow
handler turns into duplicate delivery. We ack after durable raw write, before
interpretation.

### 6.2 Diagnosis — where the LLM is allowed in

```mermaid
sequenceDiagram
    autonumber
    participant DX as Diagnosis
    participant PG as Postgres
    participant SIG as Significance
    participant RED as Redaction
    participant LLM as Claude
    participant CASE as Case

    DX->>PG: aggregate comparable cases by slice
    Note over DX,PG: issuer, BIN, PSP route, instrument,<br/>method, app version, time bucket
    PG-->>DX: slice counts + failure rates
    DX->>SIG: two-proportion z-test per slice
    SIG-->>DX: significant slices only (p < 0.01)

    alt no significant slice
        DX->>CASE: diagnosis = ABSTAIN
        Note over CASE: counted in abstention rate,<br/>routed to generic playbook
    else significant slices exist
        DX->>RED: slice stats
        RED->>RED: assert no PII fields present
        RED-->>LLM: aggregated numbers only
        LLM-->>DX: ranked hypotheses (JSON)
        alt schema valid
            DX->>CASE: diagnosis = LLM ranking
        else schema invalid or timeout
            DX->>CASE: diagnosis = rank by significance
            Note over DX: llm_schema_failures_total++
        end
    end
```

Two guarantees visible here: the model never sees a customer record, and the
model failing degrades quality rather than breaking the pipeline.

### 6.3 Action execution — the choke point

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Scheduler
    participant PG as Postgres
    participant W as Worker
    participant GATE as Policy Gate
    participant VAL as Compliance Validator
    participant RD as Redis
    participant GW as Gateway

    SCH->>PG: SELECT ... WHERE due_at <= now()<br/>FOR UPDATE SKIP LOCKED LIMIT n
    PG-->>SCH: claimed actions
    SCH->>W: dispatch

    W->>RD: check global kill switch
    alt kill switch tripped
        W->>PG: release claim, audit: halted_by_kill_switch
        Note over W: no side effect, state preserved
    else running
        W->>GATE: evaluate(action, context)
        GATE->>PG: read consent, contact history,<br/>mandate budget, case cost
        GATE-->>W: ALLOW / DENY / DEFER + rule_id

        alt DENY
            W->>PG: audit: policy_denied(rule_id, inputs)
            Note over W: action never executes
        else DEFER
            W->>PG: reschedule to next permitted window
        else ALLOW
            W->>VAL: validate message (if messaging)
            VAL-->>W: pass / fail
            W->>RD: SETNX idempotency_key
            alt key already exists
                Note over W: duplicate suppressed
            else first attempt
                W->>GW: execute
                GW-->>W: result
                W->>PG: audit: action_executed + cost
            end
        end
    end
```

The `SETNX` on Redis plus the durable outbox row gives at-least-once delivery
with effective exactly-once semantics: a crash between gateway call and audit
write is recovered by the idempotency key on retry.

### 6.4 Case state machine

```mermaid
stateDiagram-v2
    [*] --> DETECTED
    DETECTED --> DIAGNOSING
    DIAGNOSING --> PLANNED
    DIAGNOSING --> ABSTAINED : no significant slice
    ABSTAINED --> PLANNED : generic playbook

    PLANNED --> AWAITING_APPROVAL : amount > threshold
    AWAITING_APPROVAL --> EXECUTING : approved
    AWAITING_APPROVAL --> SUPPRESSED : rejected

    PLANNED --> HOLDOUT : arm == control
    HOLDOUT --> AWAITING_OUTCOME

    PLANNED --> EXECUTING
    EXECUTING --> EXECUTING : next step due
    EXECUTING --> AWAITING_OUTCOME : plan exhausted
    EXECUTING --> SUPPRESSED : stopping rule fires

    AWAITING_OUTCOME --> RECOVERED : payment attributed
    AWAITING_OUTCOME --> PARTIALLY_RECOVERED
    AWAITING_OUTCOME --> LOST : window closed
    AWAITING_OUTCOME --> EXPIRED : max case age

    EXECUTING --> ESCALATED : needs human
    ESCALATED --> EXECUTING : resolved
    ESCALATED --> SUPPRESSED : abandoned

    RECOVERED --> [*]
    PARTIALLY_RECOVERED --> [*]
    LOST --> [*]
    EXPIRED --> [*]
    SUPPRESSED --> [*]
```

Transitions are enforced in `domain/case.py` as an explicit table. An illegal
transition raises rather than silently corrupting state, and is covered by
property tests asserting every case reaches exactly one terminal state.

## 7. Data architecture

Detail in [DATA-MODEL.md](DATA-MODEL.md). The structurally significant choices:

| Choice | Reason |
|---|---|
| **Money as integer paise**, never float | `0.1 + 0.2 != 0.3`. A rounding error in a recovery system is a correctness bug. Enforced by a `Money` value object with no float constructor. |
| **`raw_events` before interpretation** | Detection logic can be re-run over history when a detector changes. Without it, a detector bug is unrecoverable. |
| **Append-only `audit_events` with `prev_hash`** | The audit log is the system of record. Mutable audit is not audit. |
| **Outbox table, not a broker** | One source of truth for "is this action pending". See ADR-0003. |
| **`consent_ledger` append-only** | Consent state is derived by folding the ledger, so we can answer "was this customer opted in *at the time we contacted them*" — the question compliance actually asks. |
| **Arm assignment stored on the case** | Reproducibility. A benchmark that re-randomises on re-run is not a benchmark. |

## 8. The gateway abstraction

The single most important testability decision.

```python
class PaymentGateway(Protocol):
    async def retry_payment(self, req: RetryRequest) -> PaymentResult: ...
    async def create_payment_link(self, req: LinkRequest) -> PaymentLink: ...
    async def fetch_payment(self, payment_id: str) -> Payment: ...
    async def present_mandate(self, req: MandateDebitRequest) -> DebitResult: ...
    async def fetch_subscription(self, sub_id: str) -> Subscription: ...
```

Two implementations:

| | `RazorpaySimulator` | `RazorpayClient` |
|---|---|---|
| Network | None | HTTPS to test mode |
| Determinism | Total — seeded RNG | None |
| Failure modes | Injected from a configured distribution | Whatever the sandbox does |
| Used by | Benchmarks, tests, `make demo` | Integration suite, live demo |
| Credentials | None | Test-mode keys only |

The simulator is not a mock. It models issuer-level failure correlation, time-of-day
success-rate variation, mandate re-presentation budgets, salary-cycle effects on
`insufficient_funds`, and injected issuer outages — because those are exactly the
phenomena the diagnosis engine claims to detect, and a simulator without them
would let us claim a capability we never tested.

Recorded as [ADR-0004](../04-adr/ADR-0004-simulator-first-gateway.md).

## 9. Deployment

```mermaid
graph TB
    subgraph "Local / Demo - docker compose"
        C1["api:8000"]
        C2["worker"]
        C3["scheduler"]
        C4["console:3000"]
        C5[("postgres:16")]
        C6[("redis:7")]
    end
    C1 --> C5
    C1 --> C6
    C2 --> C5
    C2 --> C6
    C3 --> C5
    C4 --> C1
```

`make demo` brings this up, migrates, seeds a cohort, runs a benchmark, and
prints the report path. No credentials required — the simulator is the default
gateway. Adding `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` to `.env` switches the
adapter to live test mode with no other change.

### 9.1 Scaling path (documented, not built)

Honest about what this architecture is not yet:

| Load | What breaks first | Fix |
|---|---|---|
| ~10k cases/day | Nothing | — |
| ~100k cases/day | Scheduler tick contention on the outbox | Partition outbox by `due_at` bucket; multiple scheduler shards |
| ~1M cases/day | Single Postgres write throughput | Split audit chain to its own instance; move outbox to a broker with an idempotency store |
| Multi-region | Clock skew in attribution windows | Anchor attribution to gateway-reported timestamps only |

Temporal was considered for durable workflow execution and rejected for this
scale — the reasoning, and the threshold at which it becomes correct, is in
[ADR-0003](../04-adr/ADR-0003-postgres-outbox-over-broker.md).

## 10. Technology choices

| Layer | Choice | Why this and not the obvious alternative |
|---|---|---|
| Core language | Python 3.12 | The diagnosis layer is statistics and the timing policy is a bandit. Doing that in TypeScript means reimplementing scipy. |
| API | FastAPI + Pydantic v2 | Pydantic models are the domain contract *and* the OpenAPI spec *and* the LLM response schema. One definition, three uses. |
| ORM | SQLAlchemy 2.0 async | Mature async support; `FOR UPDATE SKIP LOCKED` expressible without raw SQL escape hatches everywhere. |
| Migrations | Alembic | Autogenerate with review. Every migration reviewed by hand; autogenerate is a draft, not an answer. |
| DB | PostgreSQL 16 | Needs transactional integrity between state change and audit write. Also `SKIP LOCKED`, partial indexes, and `jsonb` for evidence blobs. |
| Cache/locks | Redis 7 | Idempotency keys and kill switch need sub-ms reads and TTLs. |
| Package manager | uv | Fast, lockfile-first, reproducible. |
| LLM | Anthropic Claude | Schema-constrained tool use for hypothesis ranking; strong at Indian-language copy. Model selection in [AI-DESIGN](AI-DESIGN.md). |
| Console | Next.js 15 App Router | Server components mean the case timeline renders without a client-side data layer. |
| UI kit | shadcn/ui + Tailwind | Ships in hours, looks credible, no design-system time sink. |
| Tests | pytest + hypothesis + testcontainers | Hypothesis for policy invariants — the properties matter more than the examples. |
| Types | mypy strict, tsc strict | Non-negotiable in a money path. |
| Telemetry | OpenTelemetry + Prometheus + structlog | Vendor-neutral; every audit event carries the trace ID. |

## 11. Where this architecture could be wrong

Stated plainly, because pretending otherwise is worse.

- **The modular monolith bet.** If the diagnosis layer turns out to need very
  different scaling than execution, the single deployable becomes a constraint.
  Mitigated by strict import boundaries, so extraction is mechanical rather than
  a rewrite. Confidence: high that this is right at this scale.
- **The bandit may not have enough data.** Contextual bandits need volume to
  beat a well-chosen fixed schedule. On a 2,000-case benchmark, the bandit could
  plausibly lose to the baseline. Mitigation is warm-starting from generator
  priors and reporting calibration honestly — and if it loses, the report says it
  lost. Confidence: medium.
- **Simulator realism bounds every claim.** The results are only as meaningful as
  the generator's failure distributions. This is why the comparison is internal
  (treatment vs baseline vs control on the *same* generator) rather than a claim
  about absolute real-world recovery rates. Confidence: the internal comparison is
  valid; absolute numbers are not claimed.
- **72-hour attribution window is a judgment call.** Too short and we undercount;
  too long and we credit coincidence. Chosen conservatively (undercounting), and
  sensitivity across 24h/72h/7d windows is reported so the choice is visible
  rather than buried.
