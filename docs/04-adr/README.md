# Architecture Decision Records

One decision per file. An ADR captures *why* a choice was made, what was
rejected, and what it costs — so a future reader can tell a deliberate decision
from an accident.

Format: MADR-lite. Status is `Proposed`, `Accepted`, `Superseded by ADR-nnnn`, or
`Deprecated`. **ADRs are never edited after acceptance** — they are superseded by
a new one, so the reasoning as it stood at the time stays visible.

| ADR | Decision | Status |
|---|---|---|
| [0001](ADR-0001-track-and-product-thesis.md) | Track 03, and the incremental-recovery thesis | Accepted |
| [0002](ADR-0002-modular-monolith.md) | Modular monolith over microservices | Accepted |
| [0003](ADR-0003-postgres-outbox-over-broker.md) | Postgres outbox over a message broker | Accepted |
| [0004](ADR-0004-simulator-first-gateway.md) | Simulator-first gateway abstraction | Accepted |
| [0005](ADR-0005-llm-off-critical-path.md) | LLM confined to the non-critical path | Accepted |
| [0006](ADR-0006-mandatory-holdout.md) | Mandatory randomised holdout | Accepted |
| [0007](ADR-0007-integer-paise-money.md) | Money as integer paise | Accepted |
