# Phase 0 — Foundations

| Field | Value |
|---|---|
| Duration | 1 day |
| Depends on | — |
| Blocks | Everything |
| Tag on completion | `v0.1.0-alpha` |

**Goal:** every quality gate exists and fails the build correctly, before any
product code is written. It is far cheaper to add `mypy --strict` to an empty
repo than to a working one.

No product code ships in this phase. That is the point.

---

## Tasks

### T0.1 — Repository scaffold
- [ ] Monorepo layout: `services/core/`, `apps/console/`, `docs/`, `infra/`, `bench/`
- [ ] `.gitattributes` — normalise line endings (`* text=auto eol=lf`)
- [ ] `.gitignore` — Python, Node, `.env`, `.venv`, build artefacts, `bench/reports/`
- [ ] `LICENSE` (MIT), `CODE_OF_CONDUCT.md`
- [ ] `.editorconfig`

### T0.2 — Python toolchain
- [ ] `uv init`, `pyproject.toml` with pinned deps and `uv.lock` committed
- [ ] ruff configured (lint + format, line length 100)
- [ ] mypy strict configured for `recoup.*`
- [ ] pytest, pytest-asyncio, pytest-cov, Hypothesis, testcontainers
- [ ] Package skeleton with the module structure from [ARCHITECTURE §5](../02-technical/ARCHITECTURE.md)

### T0.3 — Module boundary contracts
- [ ] `.importlinter` with all four contracts from ARCHITECTURE §5.1
- [ ] **Verify each contract fails when violated** — write a deliberately bad
      import, confirm CI rejects it, revert. A contract never seen to fail is a
      contract that might not work.

### T0.4 — Console scaffold
- [ ] Next.js 15 App Router, TypeScript strict, `noUncheckedIndexedAccess`
- [ ] Tailwind 4 + shadcn/ui initialised
- [ ] Vitest + Playwright configured
- [ ] One placeholder page and one passing test

### T0.5 — Compose stack
- [ ] `infra/docker-compose.yml`: Postgres 16, Redis 7, API, worker, scheduler, console
- [ ] Jaeger + Prometheus for local traces and metrics
- [ ] Healthchecks on every service; `depends_on` with `condition: service_healthy`
- [ ] Multi-stage Dockerfiles, **non-root user** (TR-54)
- [ ] `.env.example`, complete and commented

### T0.6 — Makefile
- [ ] `make setup` · `make dev` · `make test` · `make test-no-llm` · `make lint`
      · `make types` · `make migrate` · `make demo` · `make bench` · `make clean`
- [ ] Self-documenting `make help`

### T0.7 — CI
- [ ] GitHub Actions: lint → types → boundaries → unit → integration → coverage
      → security → build
- [ ] Postgres and Redis service containers
- [ ] uv and pnpm caching
- [ ] gitleaks, pip-audit, npm audit, trivy
- [ ] Coverage gate with regression detection
- [ ] Branch protection on `main`

### T0.8 — Pre-commit and commit hygiene
- [ ] `.pre-commit-config.yaml` per [ENGINEERING-STANDARDS §10](ENGINEERING-STANDARDS.md)
- [ ] commitlint with the Conventional Commits scope list
- [ ] PR template
- [ ] Issue templates (bug, task)

### T0.9 — Health endpoints
- [ ] FastAPI app skeleton
- [ ] `/health/live` (process) and `/health/ready` (DB + Redis) — distinct (TR-71)
- [ ] `/metrics` Prometheus endpoint
- [ ] structlog JSON configured with a trace-ID processor
- [ ] Typed settings object; missing required var fails startup (TR-75)
- [ ] Settings `__repr__` masks secrets (T2 mitigation)

---

## Acceptance criteria

| # | Criterion | How to verify |
|---|---|---|
| A0.1 | CI is green on an empty test suite | Push and observe |
| A0.2 | `docker compose up` yields a healthy stack | `curl /health/ready` → 200 |
| A0.3 | `make demo` runs (does nothing yet, exits 0) | Run it |
| A0.4 | A deliberate `mypy` error fails CI | Introduce, observe, revert |
| A0.5 | A boundary violation fails CI | Import `anthropic` in `recoup.policy`, observe, revert |
| A0.6 | A committed fake secret fails CI | Add, observe gitleaks, revert |
| A0.7 | A non-conventional commit message is rejected | `git commit -m "stuff"` |
| A0.8 | Cold start under 5 minutes | Time it on a clean clone (TR-47) |

A0.4 through A0.7 are the real deliverable of this phase. **A gate that has never
been seen to fail is not known to work.** Each one is deliberately tripped once
and the result recorded in [FAILURE-LOG](../05-submission/FAILURE-LOG.md) if
anything did not behave as expected.

---

## PR breakdown

| PR | Branch | Contents |
|---|---|---|
| 1 | `chore/repo-scaffold` | T0.1 |
| 2 | `chore/python-toolchain` | T0.2, T0.3 |
| 3 | `chore/console-scaffold` | T0.4 |
| 4 | `chore/compose-stack` | T0.5, T0.6 |
| 5 | `ci/pipeline` | T0.7, T0.8 |
| 6 | `feat(api): health and telemetry` | T0.9 |

---

## Risks

| Risk | Mitigation |
|---|---|
| Tooling rabbit hole consumes the day | Timebox to one day. Anything unresolved gets a TODO issue and moves on — a perfect CI on an empty repo is not progress. |
| testcontainers is slow or flaky on Windows | Verify early. Fall back to compose-provided services in CI if needed. |
| Over-configuring before knowing the shape | Configure only what the standards document requires. Resist adding tools "we might want". |
