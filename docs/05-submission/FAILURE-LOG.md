# Failure Log

> **This is a living document.** Entries are written *when things break*, during
> the build — not reconstructed afterwards. The Razorpay buildathon asks "what
> broke, and how you got out," and says it is the answer they read first.
> Backfilling it would defeat the point, and would be obvious.

**Format for every entry:** what broke · how it was found · what was actually
wrong · the fix · **what it changed about the design**.

That last field is the one that matters. A bug that gets fixed teaches nothing. A
bug that changes how you build teaches something.

---

## Entry template

```markdown
### F-00n — <short title>
**Date:** YYYY-MM-DD · **Phase:** Pn · **Severity:** low | medium | high | critical

**What broke**
Symptom, as observed. Include the actual error output.

**How it was found**
Test, benchmark run, manual check, or accident.

**Root cause**
What was actually wrong. Not the symptom.

**Fix**
What changed. Link the commit or PR.

**What it changed about the design**
The generalisation. Which invariant, test, or rule now exists because of this.
```

---

## Open issues

Things currently known to be wrong or unresolved. Kept honest — an empty section
here at submission time would be a claim not to have any open problems, which is
never true of real software.

| ID | Issue | Impact | Status |
|---|---|---|---|
| *(none yet — build starts at P0)* | | | |

---

## Entries

<!-- Newest first. Add entries as they happen. -->

### F-002 — import-linter silently constrained nothing
**Date:** 2026-09-02 · **Phase:** P0 · **Severity:** medium

**What broke**

```
Could not find package 'r' in your Python path.
```

**How it was found**

Running `lint-imports` for the first time, immediately after writing the five
boundary contracts.

**Root cause**

`root_packages = recoup` was written as an inline value. import-linter treats
`root_packages` as a multi-value field and iterated the string character by
character, so it looked for a package named `r`.

**Fix**

The indented list form:

```ini
root_packages =
    recoup
```

A second, related error followed — `No matches for ignored import recoup.* ->
recoup.platform`. The ignore was redundant (`recoup.platform` is not in the
`layers` list, so the layering contract never constrained it) and import-linter
correctly errors on an ignore that matches nothing. Removed.

**What it changed about the design**

This is the important part. The contract that forbids `recoup.policy` from
reaching `anthropic` is the structural expression of the product's central
promise (ADR-0005). Had this shipped unnoticed, CI would have reported a
**passing** import-linter step while enforcing nothing at all — the worst
possible failure mode for a guarantee, because it looks like a guarantee.

So the phase-0 acceptance criteria that were already written (A0.4–A0.6) were
executed as a deliberate exercise rather than a formality: each gate was tripped
on purpose and observed to fail, then reverted. The policy/`anthropic` contract
was verified to report `BROKEN`, mypy to reject an untyped function, and ruff to
flag `datetime.now()` and `print`.

**Generalised rule, now in the phase docs:** *a gate that has never been seen to
fail is not known to work.* Every future quality gate gets the same treatment
before it is trusted.

---

### F-001 — Package build failed on a readme path outside the project
**Date:** 2026-09-02 · **Phase:** P0 · **Severity:** low

**What broke**

```
ValueError: Readme path must be within the project directory: ../../README.md
```

`uv sync` reported exit 0 while the editable install of `recoup` had actually
failed.

**How it was found**

`uv sync` output, on the first dependency install.

**Root cause**

`services/core/pyproject.toml` pointed `readme` at the monorepo root README.
Hatchling requires the readme to live inside the package directory.

**Fix**

Removed the `readme` field. The root README is the canonical one and does not
need to be duplicated into the package metadata.

**What it changed about the design**

Nothing structural — but it is a reminder that **a zero exit code is not proof
of success.** `uv sync` returned 0 with a failed build inside it. Verification
steps in the phase docs assert on the observable outcome (`import recoup`
succeeding) rather than on a command's exit status.

---

## Failures we expect, and are watching for

Written **before** the build, so the log can be checked against predictions
afterwards. Being wrong about these is itself a useful result, and predicting
them in advance is the difference between a failure log and a post-hoc
rationalisation.

| # | Predicted failure | Why we expect it | Where it would surface |
|---|---|---|---|
| 1 | Hidden non-determinism breaks benchmark reproducibility | Dict ordering, `set` iteration, float accumulation, and parallel completion order all leak non-determinism silently | P3, byte-identical test |
| 2 | Webhook HMAC fails because the framework re-serialised the body | The single most common Razorpay integration bug — parsed-then-redumped JSON has different bytes | P2 or P7 |
| 3 | The timing bandit loses to the fixed schedule | Contextual bandits need volume; 2,000 cases may not be enough to beat a well-chosen prior | P5, benchmark |
| 4 | LLM ranking does not beat z-score ordering | Statistical ranking may already be near-optimal on the slices we compute | P5, ablation table |
| 5 | Quiet-hours logic wrong outside IST | Timezone handling written and tested in one zone usually is | P4, property test P3 |
| 6 | Mandate budget race under concurrent workers | Check-then-act without atomic reservation is the classic form | P4, property test P5 |
| 7 | Prompt cache silently ineffective | One interpolated value in the system prompt destroys it, with no error | P5, `cache_read_input_tokens` |
| 8 | Frequency caps counted per case instead of per customer | The natural way to write it, and the way that turns recovery into harassment | P4, property test P7 |
| 9 | Real Razorpay decline codes do not match the assumed taxonomy | The mapping is built from documentation, not from observed sandbox responses | P7 |
| 10 | The 10-minute benchmark target is missed on first attempt | Per-case work that should have been per-cohort | P3 |

Predictions 3 and 4 are the interesting ones, because they are predictions that
**the AI components might not earn their place.** Both have an ablation or a
baseline arm built specifically to detect them, and both have a documented
response: report it, and consider removing the component.

If either comes true, that goes in this log and in the benchmark report, not in a
drawer.
