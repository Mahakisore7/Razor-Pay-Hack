# ADR-0005 — LLM confined to the non-critical path

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

Recoup is an AI product for an AI buildathon. The obvious design is an agent that
reasons about each case and decides what to do — LLM-first, tools underneath.
That design is wrong for a system that moves money, and the reasons are worth
recording.

## Decision

The LLM is used at exactly **four** of eighteen pipeline stages: hypothesis
ranking, playbook proposal for unseen decline codes, customer copy generation, and
report narration. It is structurally excluded from detection, statistics, timing,
the policy gate, compliance validation, attribution, and outcome classification.

**The model contributes quality. It never contributes correctness.**

## Rationale

Three properties are required of everything on the money path, and a language
model provides none of them:

- **Determinism.** A regulator asking "would this have been blocked on 3
  September" needs one answer, not a distribution.
- **Reviewability.** Meera can read fourteen rule files and know what they do. She
  cannot read a prompt and know what it will do on the fifteen-thousandth case.
- **Non-persuadability.** Customer-supplied text flows through this system. A model
  gating actions is a model that can be argued with by an attacker.

Against that, the model earns its place where genuine ambiguity exists: deciding
that a BIN-range slice is downstream of an issuer slice rather than an independent
cause is real domain reasoning over correlated evidence, and Hinglish
personalisation across thousands of variants is exactly what language models are
for.

The split is enforced structurally, not by convention. `import-linter` forbids
`recoup.policy` from reaching `anthropic` in its transitive closure, so the central
promise cannot decay through a well-meaning refactor.

## Alternatives rejected

**Agent-first with tool calls.** The demo-friendly design, and the one most
submissions will use. Rejected: an agent that can call `send_sms` is an agent that
can be prompt-injected into calling `send_sms`. Putting a deterministic gate
downstream of the agent gives the same safety with none of the non-determinism, so
the agent framing buys only the appearance of sophistication.

**LLM-as-judge for compliance.** Rejected outright. "Is this legal to send" must
not have a temperature parameter.

**No LLM at all.** Considered seriously. Rejected because hypothesis ranking over
correlated slices and vernacular copy generation are genuinely better with one —
but the architecture supports running without it, and the ablation measures whether
that is still true.

## Consequences

**Positive.** The system is correct without the model. `make test-no-llm` runs the
full suite with an LLM client that raises on every call, and it must pass. Prompt
injection has no privileged channel to attack, because the model's output is a
closed enum consumed by a dictionary lookup.

**Negative.** Less headline-grabbing than an autonomous agent. Some reviewers
equate "agentic" with "sophisticated" and may read this as less ambitious.

**Accepted.** The buildathon explicitly asks where you chose *not* to use AI. This
ADR is the answer, and the ablation table in every benchmark report keeps the
question open — if the LLM stops earning its place, the honest response is to
remove it.
