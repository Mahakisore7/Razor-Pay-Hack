# Definition of Done

| Field | Value |
|---|---|
| Document version | 1.0 |
| Related | [ENGINEERING-STANDARDS](ENGINEERING-STANDARDS.md) · [GIT-WORKFLOW](GIT-WORKFLOW.md) |

"Done" is not "it works on my machine." Three levels, each stricter.

---

## Level 1 — A task is done

- [ ] Acceptance criteria in the phase doc are met
- [ ] Tests written **and observed to fail before the fix**
- [ ] `mypy --strict` / `tsc --strict` clean
- [ ] ruff clean, no new per-file ignores
- [ ] import-linter contracts pass
- [ ] No secret, no PII in logs
- [ ] Docstrings on new public functions
- [ ] Docs updated **in the same commit** if behaviour changed

The "observed to fail" clause matters. A test written after the code, that passes
immediately, has not been shown to test anything.

## Level 2 — A PR is done

Everything in Level 1, plus:

- [ ] PR template filled, including the **Risk** section
- [ ] Under ~400 changed lines, or split with a reason given
- [ ] CI fully green
- [ ] Coverage did not regress
- [ ] Self-review completed against the [code review checklist](ENGINEERING-STANDARDS.md#9-code-review)
- [ ] ADR added if an architectural decision was made
- [ ] Benchmark re-run if the change could move the numbers
- [ ] Commits conventional; branch rebased on `main`
- [ ] Exactly one issue closed

## Level 3 — A phase is done

Everything in Level 2, for every PR, plus:

- [ ] Every acceptance criterion in the phase doc is verified, individually
- [ ] The **phase gate** criterion is verified twice, independently
- [ ] `make demo` works on a clean clone
- [ ] `make test-no-llm` passes (from P5 onward)
- [ ] Full benchmark re-run; report committed
- [ ] Property tests pass at full depth, not the CI-capped example count
- [ ] FAILURE-LOG updated with anything that broke
- [ ] Tagged and CHANGELOG regenerated

---

## The gates that are never waived

Under time pressure, some things get cut. These do not. They are listed here so
the decision is made now, calmly, rather than at 2 AM on 4 September.

| Gate | Why it is absolute |
|---|---|
| **No action executes without an `ALLOW`** | The product's central claim. Without it, this is a spam engine with good documentation. |
| **The audit chain verifies** | The audit log is the system of record. An unverifiable log proves nothing. |
| **The control arm executes zero actions** | A contaminated holdout invalidates every number in the report. |
| **Quiet-hour violations are zero** | An invariant, not a target. One violation is a failing build. |
| **No secret in any commit** | Irreversible once pushed to a public repo. |
| **Money arithmetic is integer** | A rounding error makes the headline number indefensible. |
| **The benchmark is reproducible from its seed** | A result nobody can regenerate is not a result. |
| **Reported numbers match the committed report** | Any drift here reads as fabrication, whatever the cause. |

Everything else — the console, live Razorpay, L4/L5 leak classes, the bandit — is
scope that can be cut and stated plainly as cut. The eight rows above are what
makes the thing worth submitting at all.
