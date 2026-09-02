# Glossary

Shared vocabulary for Recoup. Terms are used with exactly these meanings in code,
docs, database columns, and API fields. Where a term has a loose industry meaning
and a precise Recoup meaning, the Recoup meaning wins.

---

## Domain — Recoup concepts

| Term | Meaning |
|---|---|
| **Leak** | A category of revenue loss. One of six: failed payment, failed mandate debit, halted subscription, abandoned checkout, overdue receivable, success-rate degradation. |
| **Signal** | A detected, timestamped instance of a leak. Immutable. Produced by deterministic detectors, never by a model. |
| **Case** | The unit of recovery work. Wraps one at-risk amount for one customer, and owns a state machine from `DETECTED` to a terminal outcome. |
| **At-risk amount** | The rupee value a case is trying to recover. Fixed at case creation; never re-estimated upward. |
| **Diagnosis** | A ranked set of root-cause hypotheses with attached evidence and a confidence score. Produced by the diagnosis engine (statistics) and narrated by the LLM. |
| **Root cause** | The winning hypothesis in a diagnosis. Drives playbook selection. |
| **Playbook** | A named, versioned recovery strategy for one root cause. Declares which steps are permissible, in what order, with what timing model. |
| **Intervention plan** | A concrete, scheduled sequence of steps instantiated from a playbook for one specific case. |
| **Step** | One planned unit of a plan (for example, "retry the charge", "send a payment link by SMS"). Becomes zero or more actions. |
| **Action** | One atomic outbound side effect. The only thing that touches the outside world. Always policy-gated, always idempotent, always audited. |
| **Policy decision** | The verdict of the policy engine on a proposed action: `ALLOW`, `DENY`, or `DEFER`, with the rule that decided it. |
| **Stopping rule** | A policy rule that terminates a case rather than blocking a single action. Example: customer opted out, dispute filed, retry budget exhausted. |
| **Outcome** | The terminal resolution of a case: `RECOVERED`, `PARTIALLY_RECOVERED`, `LOST`, `EXPIRED`, `SUPPRESSED`, or `ESCALATED`. |
| **Attribution** | The deterministic process that decides whether an inbound payment counts as recovered by a case, using a bounded attribution window and amount matching. |
| **Exception** | A case Recoup could not resolve, recorded with a machine-readable reason. Exceptions are a deliverable, not a failure to hide. |
| **Holdout** | A randomised subset of cases that are detected and diagnosed but never acted on, so incremental lift can be measured. |
| **Arm** | One experimental condition in a benchmark run: `control` (holdout), `baseline` (naive fixed retry), or `treatment` (full Recoup). |
| **Kill switch** | A global or per-playbook flag that halts all action execution immediately without losing state. |
| **Audit event** | An append-only, hash-chained record of anything that happened to a case. The audit log is the system of record. |

## Payments — Indian context

| Term | Meaning |
|---|---|
| **UPI** | Unified Payments Interface. India's real-time account-to-account rail, operated by NPCI. |
| **UPI Autopay** | UPI's recurring mandate product. Lets a merchant debit a customer on a schedule after one-time authorisation, within a capped amount and frequency. |
| **Mandate** | A customer's standing authorisation for a merchant to debit them. Has a max amount, frequency, validity window, and a limited re-presentation budget. |
| **Re-presentation** | Retrying a failed mandate debit. Rail rules cap how many times this may be done per cycle — burning the budget on doomed retries is a real cost. |
| **eNACH / NACH** | National Automated Clearing House. Bank-account-based recurring debit, slower and more batch-oriented than UPI Autopay. |
| **Emandate** | Card- or account-based recurring authorisation registered with the issuer. |
| **PSP** | Payment Service Provider. In UPI, the app or bank routing the transaction. |
| **Issuer** | The customer's bank or card issuer — the entity that ultimately approves or declines. |
| **Acquirer** | The merchant's bank. |
| **BIN** | Bank Identification Number. The leading digits of a card that identify the issuer; a useful diagnosis slice. |
| **Settlement** | The transfer of collected funds to the merchant's bank account, net of fees, on a schedule. |
| **Chargeback / dispute** | A customer-initiated reversal. In Recoup, a hard stop — never a thing to be recovered against. |
| **Decline code** | The reason string a rail returns for a failure. Recoup normalises these across rails into a canonical taxonomy. |
| **Payment link** | A Razorpay-hosted URL that lets a customer pay a specific amount without a checkout integration. Recoup's primary customer-facing recovery instrument. |
| **Order** | A Razorpay object representing an intent to collect a specific amount. |
| **Virtual account / Smart Collect** | A per-customer bank account number that auto-reconciles inbound transfers. Used for B2B receivables. |
| **Test mode** | Razorpay's sandbox. All Recoup live-integration work runs here. No production keys, ever. |

## Compliance and regulation

| Term | Meaning |
|---|---|
| **RBI** | Reserve Bank of India. Sets rules on recurring payments, auto-debit notification, and tokenisation. |
| **NPCI** | National Payments Corporation of India. Operates UPI and NACH; sets mandate and re-presentation rules. |
| **TRAI** | Telecom Regulatory Authority of India. Governs commercial communication — registered templates, sender IDs, and time-of-day restrictions. |
| **DLT** | Distributed Ledger Technology registry. TRAI-mandated registration of commercial SMS templates and headers. An unregistered template will not deliver. |
| **DND / DNC** | Do Not Disturb / Do Not Call registry. A customer on DND must not receive promotional contact. |
| **Quiet hours** | The window in which non-transactional outbound contact is prohibited. Recoup enforces 21:00–09:00 IST by default, tighter than the legal floor. |
| **Pre-debit notification** | RBI requirement to notify a customer before an auto-debit above a threshold. Recoup treats it as a mandatory action, not an option. |
| **Consent** | The recorded, revocable permission to contact a customer on a given channel. Absence of consent is treated as refusal. |

## Statistics and ML

| Term | Meaning |
|---|---|
| **Incremental recovery** | Recovery in the treatment arm minus recovery in the control arm. The only headline number Recoup reports. |
| **Lift** | Incremental recovery expressed as a ratio over control. |
| **Holdout / control arm** | See *Holdout*. Mandatory on every benchmark run. |
| **Contextual bandit** | An online learning method that chooses among actions given context and learns from observed reward. Recoup uses one for retry-time selection. |
| **Thompson sampling** | The exploration strategy used by the bandit: sample from each arm's posterior and pick the argmax. |
| **CUSUM / EWMA** | Cumulative-sum and exponentially-weighted-moving-average change detectors. Used for success-rate degradation, in place of static thresholds. |
| **Two-proportion z-test** | The significance test applied to each diagnosis slice before it is allowed to become a hypothesis. |
| **Attribution window** | The bounded time period after an action during which a payment may be credited to it. Outside the window, no credit. |
| **Calibration** | Whether predicted probabilities match observed frequencies. Reported for the bandit; an uncalibrated model is a broken one. |

## Engineering

| Term | Meaning |
|---|---|
| **Control plane** | The layer that decides what should happen. Recoup is a control plane; Razorpay is the data plane that executes money movement. |
| **Gateway adapter** | The interface abstracting Razorpay. Two implementations: live test-mode client and deterministic simulator. |
| **Simulator** | A seeded, offline implementation of the gateway adapter, so benchmarks are reproducible and the demo needs no credentials. |
| **Outbox** | The Postgres table durably holding scheduled actions, claimed by workers with `FOR UPDATE SKIP LOCKED`. |
| **Idempotency key** | A caller-supplied unique key ensuring a retried request produces one effect, not two. Mandatory on every action. |
| **Hash chain** | Each audit event stores the hash of its predecessor, so tampering with history is detectable. |
| **ADR** | Architecture Decision Record. A short document capturing one decision, its context, and its consequences. See [`docs/04-adr/`](../04-adr/). |
