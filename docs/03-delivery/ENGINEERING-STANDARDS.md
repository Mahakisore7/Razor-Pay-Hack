# Engineering Standards

| Field | Value |
|---|---|
| Document version | 1.0 |
| Related | [GIT-WORKFLOW](GIT-WORKFLOW.md) · [DEFINITION-OF-DONE](DEFINITION-OF-DONE.md) · [TRD](../02-technical/TRD.md) |

Standards that are enforced by tooling, not by good intentions. Anything in this
document that a machine can check, a machine does check.

---

## 1. Non-negotiables

Ten rules. Each one is enforced by a linter, a type checker, a test, or a
database constraint — because a standard that relies on remembering is a standard
that decays.

| # | Rule | Enforced by |
|---|---|---|
| 1 | No floats in money arithmetic | `Money` has no float constructor; lint bans `float(` in `domain` |
| 2 | No `datetime.now()` in domain, detection, or policy | ruff custom rule; the clock is injected |
| 3 | No global RNG | ruff bans `random.*` / `np.random.*`; generators are passed |
| 4 | The policy engine cannot import a model | import-linter contract |
| 5 | No unbounded HTTP call | ruff rule requiring explicit `timeout=` |
| 6 | No PII in logs or LLM payloads | structlog processor + redactor assertion |
| 7 | No action without a matching `ALLOW` | Executor assertion + property test P6 |
| 8 | No mutable audit | Postgres trigger |
| 9 | `mypy --strict` clean | CI |
| 10 | No secret in the repo | gitleaks, pre-commit + CI |

## 2. Python

### 2.1 Style

`ruff` for lint and format. Line length 100. Configuration in `pyproject.toml`,
no per-file ignores without an inline comment explaining why.

### 2.2 Typing

`mypy --strict` on `recoup.*`. Specifically:

- No implicit `Any`. `Any` requires a comment justifying it.
- No untyped function definitions.
- Domain identifiers are `NewType`, not bare `str`:

  ```python
  CaseId = NewType("CaseId", UUID)
  SignalId = NewType("SignalId", UUID)
  ```

  This costs nothing and makes `execute(signal_id, case_id)` called with swapped
  arguments a type error rather than a runtime mystery.
- `Protocol` for interfaces, not ABCs — structural typing keeps the simulator
  independent of the client's inheritance.

### 2.3 Structure

- Domain objects are frozen dataclasses with `slots=True`.
- Pydantic at boundaries (API, config, LLM schemas); dataclasses in the domain.
  Pydantic in the domain would drag validation machinery into pure logic.
- One public class or function per module where reasonable.
- `__all__` on public modules.

### 2.4 Errors

- Domain errors subclass `RecoupError`.
- Never catch bare `Exception` except at the top-level worker boundary, where it
  logs and marks the action failed.
- Errors carry context: `CaseNotFound(case_id)`, not `ValueError("not found")`.
- **Never silently swallow.** A caught exception is logged or re-raised, always.

### 2.5 Async

- `async` all the way down for I/O. No `asyncio.run` inside library code.
- No blocking calls in async paths — `asyncio.sleep`, never `time.sleep`.
- Every `await` on external I/O has a timeout.

## 3. TypeScript

- `strict: true`, plus `noUncheckedIndexedAccess`.
- No `any`. `unknown` plus narrowing.
- Server Components by default; `"use client"` only where interactivity requires.
- API types generated from the OpenAPI schema — never hand-written, so a backend
  change that breaks the console fails at compile time.
- Zod validation at the boundary; a trusted API is still a boundary.

## 4. Testing

### 4.1 The pyramid, weighted for this system

| Layer | Share | Notes |
|---|---|---|
| Unit | ~60% | Fast, isolated, no I/O |
| Property | ~10% | Policy, attribution, domain invariants |
| Integration | ~25% | Real Postgres and Redis via testcontainers |
| E2E | ~5% | Full pipeline, dry-run |

The property share is unusually high, and deliberately. For a compliance gate,
"it works on these 40 examples" is much weaker than "no input sequence can
produce a quiet-hour violation."

### 4.2 Rules

- Test names state the behaviour: `test_quiet_hours_defers_not_denies`, not
  `test_policy_2`.
- **Arrange–Act–Assert**, visually separated.
- One behaviour per test.
- No test depends on another's state or on execution order.
- No sleeps. Inject the clock and advance it.
- No network. The simulator is the network.
- **No mocked database.** A mocked `SKIP LOCKED` tests the mock.
- Fixtures build valid objects by default; tests override only what they exercise.

### 4.3 Coverage

| Module | Requirement |
|---|---|
| `policy` | **100% branch** |
| `attribution` | **100% branch** |
| `domain` | ≥ 95% line |
| Everything else | ≥ 85% line |

Coverage is a floor, not a goal. A module at 100% with no property tests is less
trustworthy than one at 85% with them, which is why the two requirements are
listed separately rather than as one number.

## 5. Documentation

### 5.1 Code

- Docstrings on public functions: what, why, and any non-obvious constraint. Not
  a restatement of the signature.
- Comments explain **why**. The code already says what.
- A comment explaining a workaround links the issue or the upstream bug.
- Non-obvious domain rules cite the requirement:

  ```python
  # Consent is evaluated at due_at, not now: compliance asks whether the
  # customer was opted in *when contacted*, not whether they are now. (FR-20)
  ```

### 5.2 Repo

- Every architectural decision gets an [ADR](../04-adr/).
- Behaviour changes update the relevant doc **in the same PR**. Documentation
  that lags the code is worse than none, because it is confidently wrong.
- The README's `make demo` path is verified on a clean clone before every tag.

## 6. Configuration

- All configuration via environment variables, parsed into a typed settings
  object at startup.
- **Fail fast**: a missing or invalid required setting stops the process with a
  clear message. Never a silent default for something that matters.
- Secrets never have defaults.
- Domain thresholds (quiet hours, caps, ceilings) live in versioned YAML, not in
  code, and their hash is recorded on every policy decision.
- `.env.example` is complete and current; every variable has a comment.

## 7. Dependencies

| Rule | Reason |
|---|---|
| Pinned by hash in the lockfile | Reproducible builds |
| A new dependency needs a one-line justification in the PR | Every dependency is a permanent liability |
| Prefer the standard library | `hashlib` over a hashing package |
| No dependency for something trivial | The leftpad lesson |
| `pip-audit` / `npm audit` clean of high and critical | CI gate |

## 8. Performance

- Measure before optimising. A benchmark or a profile, in the PR.
- No N+1 queries — integration tests assert query counts on hot paths.
- Index every foreign key and every filter column; verify with `EXPLAIN`.
- Batch external calls where the API allows.
- Performance-sensitive paths (policy evaluation, attribution) have
  micro-benchmarks with regression thresholds.

## 9. Code review

Even self-review on a solo project. The checklist:

- [ ] Does it do what the PR says, and only that?
- [ ] Are the tests meaningful, or do they assert the implementation back at itself?
- [ ] What happens on failure? Is the error path tested?
- [ ] Any way this executes an action without an `ALLOW`?
- [ ] Any way PII reaches a log or a model?
- [ ] Is money arithmetic integer throughout?
- [ ] Does it degrade gracefully if the LLM is unavailable?
- [ ] Would this be understandable in six months without the author?

The fourth and fifth items are on the list because they are the two failures that
would matter most and are the easiest to introduce accidentally.

## 10. Pre-commit hooks

```yaml
- ruff (lint + format)
- mypy
- import-linter
- gitleaks
- commitlint
- trailing whitespace, EOF newline, large file check
```

Never bypassed with `--no-verify`. Skipping a hook is a decision that you did not
forget anything, made at exactly the moment you most likely did.
