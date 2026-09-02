<div align="center">

# Recoup

**A revenue recovery control plane for Indian payments.**

Finds money that is slipping away, works out *why*, executes a bounded recovery
under a compliance gate no model can override — and proves, against a randomised
control group, how much it actually recovered.

*Razorpay /buildathon · Track 03 — AI Revenue Recovery*

</div>

---

> **Status:** Design complete, implementation phased. This repository currently
> contains the full product and technical specification. Build progress tracks
> [`docs/03-delivery/ROADMAP.md`](docs/03-delivery/ROADMAP.md); benchmark numbers
> below are published once Phase 3 lands, and this notice is removed when they do.

## The problem

A merchant on Razorpay loses revenue six ways — failed payments, failed mandate
debits, halted subscriptions, abandoned checkouts, overdue invoices, and
success-rate degradation nobody noticed for four hours.

The tools that exist retry on a cron and send three dunning emails. Then they
report a **recovery rate**, and that number is close to meaningless: a large share
of at-risk payments recover on their own. **A system that does nothing reports a
30% recovery rate.**

So the interesting problem is not detection. It is knowing *why* a payment failed,
whether it is worth chasing, when and how to chase it, when to stop — and being
able to prove afterwards that the chasing caused the recovery rather than
coinciding with it.

## What Recoup does

```
signal → diagnosis → playbook → policy gate → execution → attribution
         (statistics,   (versioned,   (deterministic,  (idempotent,  (deterministic,
          then LLM)      pinned)       never a model)   audited)      conservative)
```

Three commitments, and the architecture follows from them:

**1. Diagnosis before action.** Every case is sliced by issuer, BIN range, PSP
route, and instrument, with a significance test on each slice, *before* anything
acts. `issuer_down` means wait — messaging the customer is pure cost.
`insufficient_funds` means time the retry to their salary cycle. `mandate_revoked`
means stop retrying and ask for re-authorisation.

**2. A policy gate that cannot be argued with.** Every outbound action passes a
deterministic engine enforcing mandate re-presentation caps, contact frequency
limits, quiet hours, consent, cost ceilings, and stopping rules. It contains no
model, and `import-linter` fails the build if one is ever imported into it. Every
**denial** is audited with the rule that fired — the audit trail shows what the
system refused to do, not only what it did.

**3. Honest measurement.** 10% of cases are a randomised holdout that receives
nothing. The headline metric is **incremental recovery** — treatment minus control,
with a confidence interval — alongside cost per rupee recovered and a complete,
untruncated list of the cases it could not resolve.

## Quickstart

**No Razorpay account or API key required.** The gateway defaults to a seeded,
offline simulator.

```bash
git clone https://github.com/Mahakisore7/Razor-Pay-Hack.git
cd Razor-Pay-Hack
make demo
```

That brings up the stack, migrates, generates a seeded cohort, runs a three-arm
benchmark, and prints the report path. Target: under 5 minutes on a clean machine.

```bash
make bench SEED=42        # reproducible benchmark
make test                 # full suite
make test-no-llm          # full suite with the LLM raising on every call — must pass
recoup audit verify --case <id>   # verify a case's hash-chained audit trail
```

To drive real Razorpay **test mode**, add `rzp_test_*` keys to `.env`. The pipeline
is unchanged; only the gateway implementation swaps. A non-test key fails startup
by design.

## Results

*Populated from a committed benchmark report at Phase 3. Numbers here will match
`bench/reports/` exactly.*

| Metric | Value |
|---|---|
| Incremental recovery vs control (95% CI) | *pending* |
| Incremental recovery vs naive baseline | *pending* |
| Cost per rupee recovered | *pending* |
| Mandate budget efficiency vs baseline | *pending* |
| Quiet-hour violations | *pending — must be 0* |
| Diagnosis top-1 accuracy vs ground truth | *pending* |
| Unresolved exceptions | *pending — reported in full* |

## Where AI is used — and where it is not

Eighteen pipeline stages. The model is used in **four**.

| Used | Not used |
|---|---|
| Ranking root-cause hypotheses over pre-computed statistics | Signal detection, slice aggregation, significance testing |
| Proposing playbooks for never-seen decline codes (as a PR, not a decision) | Retry timing — a contextual bandit, because it is a calibrated-probability question |
| Generating Hinglish and vernacular customer copy | **The policy gate** — structurally forbidden from importing a model |
| Narrating the benchmark report (display only, never parsed) | Message compliance validation, payment attribution, outcome classification |

The model **contributes quality; it never contributes correctness.** `make
test-no-llm` runs the entire suite with the LLM client raising on every call, and
it must pass. Every benchmark reports an **ablation** — if the LLM does not beat
pure statistical ranking by a margin justifying its cost, the honest conclusion is
to remove it, and the report says so.

Full reasoning: [`docs/02-technical/AI-DESIGN.md`](docs/02-technical/AI-DESIGN.md).

## What is real, and what is simulated

Stated plainly, because a reviewer who finds one overstatement discounts
everything else.

| | Simulator (default) | Razorpay test mode |
|---|---|---|
| Payment retries, links, subscriptions, mandates | Simulated | **Real test-mode API calls** |
| Webhooks | Injected | **Real deliveries** |
| SMS / WhatsApp / email / voice delivery | Simulated | **Simulated** |

**Recoup does not send real SMS.** The channel adapters, cost accounting, consent
enforcement, and compliance validation are real code doing real work — only the
final hop to a telecom provider is stubbed.

Benchmark data is **synthetic**, from a seeded generator whose every parameter is
published in the report. We claim the *internal* comparison between arms on the
same generator. We do not claim absolute real-world recovery rates.

## Architecture

```
services/core/src/recoup/
├── domain/       pure — entities, state machines, Money as integer paise
├── policy/       deterministic gate; cannot import a model (CI-enforced)
├── detection/    L1–L6 leak detectors, EWMA change detection
├── diagnosis/    SQL slicing → significance tests → LLM ranking → fallback
├── planning/     versioned playbooks, contextual-bandit retry timing
├── execution/    the only module permitted outbound side effects
├── attribution/  deterministic payment matching
├── gateway/      PaymentGateway protocol · simulator + Razorpay client
├── audit/        append-only, hash-chained, DB-trigger-enforced
└── bench/        cohort generator, three arms, report writer
```

Boundaries are enforced by `import-linter` contracts. The contract that matters:
`recoup.policy` may not reach `anthropic` in its transitive closure.

## Documentation

| | |
|---|---|
| [Vision](docs/00-overview/VISION.md) | The thesis, and why the naive version is wrong |
| [PRD](docs/01-product/PRD.md) | Scope, personas, 44 functional requirements |
| [Metrics doctrine](docs/01-product/METRICS-AND-KPIS.md) | Why recovery rate is a lie, and what we report instead |
| [Architecture](docs/02-technical/ARCHITECTURE.md) | C4 diagrams, flows, and where this could be wrong |
| [Domain model](docs/02-technical/DOMAIN-MODEL.md) | Entities, state machine, invariants I1–I7 |
| [Policy engine](docs/02-technical/POLICY-ENGINE.md) | Rules R1–R11, stopping rules, property invariants |
| [AI design](docs/02-technical/AI-DESIGN.md) | The 18-stage decision table |
| [Razorpay integration](docs/02-technical/RAZORPAY-INTEGRATION.md) | APIs, webhooks, decline taxonomy, the simulator |
| [Security](docs/02-technical/SECURITY.md) | Threat model, 10 threats with residual risk |
| [Roadmap](docs/03-delivery/ROADMAP.md) | Phases P0–P8 with gates |
| [ADRs](docs/04-adr/) | Seven decisions, with what was rejected and what it costs |
| [Failure log](docs/05-submission/FAILURE-LOG.md) | What broke, and how we got out |

## Development

```bash
make setup    # install deps, start services, migrate
make dev      # run API, worker, scheduler, console
make lint     # ruff
make types    # mypy --strict, tsc --strict
make test     # full suite
```

Standards: [`ENGINEERING-STANDARDS`](docs/03-delivery/ENGINEERING-STANDARDS.md) ·
Workflow: [`GIT-WORKFLOW`](docs/03-delivery/GIT-WORKFLOW.md)

Built with AI assistance; commits are co-authored accordingly. The judgment on
display is in the architecture, the decisions, and the things deliberately *not*
done — see [ADR-0005](docs/04-adr/ADR-0005-llm-off-critical-path.md) and
[ADR-0006](docs/04-adr/ADR-0006-mandatory-holdout.md).

## Licence

MIT — see [LICENSE](LICENSE).
