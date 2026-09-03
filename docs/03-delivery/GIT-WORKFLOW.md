# Git Workflow

| Field | Value |
|---|---|
| Document version | 1.0 |
| Model | Trunk-based, short-lived branches, squash merge |
| Related | [ENGINEERING-STANDARDS](ENGINEERING-STANDARDS.md) · [DEFINITION-OF-DONE](DEFINITION-OF-DONE.md) |

The buildathon scores "build quality: does it run, is it structured, **would you
trust it**." Commit history is the most honest available record of how something
was built, so it is treated as a deliverable.

---

## 1. Branching

**Trunk-based.** `main` is always releasable. Branches are short-lived — hours to
a day — and merge via PR.

Long-lived feature branches are avoided deliberately: they hide integration risk
until the end, and on a solo build they produce exactly the "big bang merge, then
everything broke" story that a reviewer reads as inexperience.

### 1.1 Branch naming

```
<type>/<short-kebab-description>
```

| Type | For |
|---|---|
| `feat/` | New capability |
| `fix/` | Bug fix |
| `docs/` | Documentation only |
| `refactor/` | Behaviour-preserving restructure |
| `test/` | Tests only |
| `chore/` | Tooling, deps, CI |
| `perf/` | Performance |

Examples:

```
feat/policy-engine-quiet-hours
feat/razorpay-webhook-verification
fix/attribution-window-boundary
docs/product-technical-foundation
chore/ci-add-import-linter
```

### 1.2 Protection on `main`

- No direct pushes.
- PR required; CI must be green.
- Branches must be current with `main` before merge.
- Force push and deletion disabled.

Enforced on a solo project too. The rules exist to make the *process* legible,
and a self-approved PR with green CI still demonstrates the discipline.

## 2. Commits — Conventional Commits

```
<type>(<scope>): <subject>

<body>

<footer>
```

Enforced by commitlint in pre-commit and CI.

### 2.1 Types

`feat` · `fix` · `docs` · `refactor` · `test` · `chore` · `perf` · `ci` · `build` · `revert`

### 2.2 Scopes

Code scopes match the module structure, so `git log --grep` is useful:

`domain` · `policy` · `detection` · `diagnosis` · `planning` · `execution` ·
`attribution` · `gateway` · `audit` · `bench` · `api` · `console` · `infra`

`docs` commits scope to the `docs/` section touched instead, `docs/NN-<name>/`
minus the number: `overview` · `product` · `technical` · `delivery` · `adr` ·
`submission`. Both lists are enforced together by `commitlint.config.js`.

### 2.3 Rules

| Rule | Reason |
|---|---|
| Subject in imperative mood, ≤ 72 chars, no trailing period | Reads as "apply this commit and it will…" |
| Body explains **why**, not what | The diff already says what |
| One logical change per commit | A commit that does two things cannot be reverted cleanly |
| Never commit broken code to `main` | Bisect is only useful if every commit builds |
| Breaking changes get `!` and a `BREAKING CHANGE:` footer | Semantic versioning depends on it |

### 2.4 Example

```
feat(policy): enforce quiet hours in customer timezone

Quiet hours were evaluated against server time, which would have
sent SMS at 03:00 to a customer in a different timezone than the
deployment. The rule now reads customer.timezone and evaluates
action.due_at in that zone.

Verdict is DEFER rather than DENY: the message is legitimate, only
the timing is wrong, so it is rescheduled to the window opening
rather than dropped.

Property test P3 extended to generate timezones across the full
UTC-12..UTC+14 range.

Refs: FR-20, TR-20
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

That body is worth the thirty seconds. Six months later it answers "why DEFER and
not DENY" without anyone re-deriving it.

## 3. Pull requests

### 3.1 Size

Target **under 400 changed lines**. Above roughly 500, review quality collapses —
reviewers start skimming, and a skimmed PR is an unreviewed PR. Split by seam:
schema, then logic, then wiring.

### 3.2 Template

```markdown
## What
One paragraph: what this changes.

## Why
The problem being solved. Link the requirement (FR-n / TR-n) or issue.

## How
Notable implementation decisions. Anything a reviewer would otherwise
have to reverse-engineer from the diff.

## Testing
- [ ] Unit tests added/updated
- [ ] Property tests updated (if policy/attribution/domain)
- [ ] Integration tests pass
- [ ] `make test-no-llm` passes (if diagnosis touched)
- [ ] Benchmark re-run (if it could move the numbers)

## Risk
What could break. What is not covered.

## Checklist
- [ ] CI green
- [ ] Types clean (mypy strict / tsc strict)
- [ ] No secrets, no PII in logs
- [ ] Docs updated if behaviour changed
- [ ] ADR added if an architectural decision was made
```

The **Risk** section is not optional. A PR whose author cannot say what might
break has not been thought through.

### 3.3 Merge strategy

**Squash merge.** One commit per PR on `main`, with the PR number appended.

The rationale: local commits are a work log — "wip", "fix typo", "actually fix
it". `main`'s history should be a sequence of complete, reviewable, revertible
changes. Squashing gives both: messy local iteration, clean permanent history.

```
feat(policy): enforce quiet hours in customer timezone (#23)
```

## 4. Releases

Semantic versioning, tagged on `main`.

```
v0.1.0   P0-P1 foundations
v0.2.0   P2 closed loop
v0.3.0   P3 measurement
v0.4.0   P4 governance
v0.5.0   P5 intelligence
v1.0.0   P8 submission
```

Pre-1.0, minor versions may break — appropriate for a system with no external
consumers. `CHANGELOG.md` is generated from Conventional Commits, which is the
practical payoff for the commit discipline.

## 5. Issues

One issue per unit of work, labelled `phase-0`…`phase-8`, plus an area label
matching the commit scope. Each issue states its acceptance criteria before work
starts.

Every PR closes exactly one issue (`Closes #N`).

## 6. CI gates

Every PR runs, and every one must pass:

```mermaid
flowchart LR
    A["Push"] --> B["Lint<br/>ruff"]
    B --> C["Types<br/>mypy --strict"]
    C --> D["Boundaries<br/>import-linter"]
    D --> E["Unit +<br/>property"]
    E --> F["Integration<br/>testcontainers"]
    F --> G["Coverage<br/>gate"]
    G --> H["Security<br/>gitleaks, pip-audit"]
    H --> I["Build<br/>images"]
    I --> J["✅ Mergeable"]

    classDef pass fill:#143d14,stroke:#4ad94a,color:#fff
    class J pass
```

The `import-linter` step is early and deliberately so: it enforces that the policy
engine cannot import a model ([AI-DESIGN §6.1](../02-technical/AI-DESIGN.md)).
That is a product guarantee, and it fails the build like any other.

## 7. Commit hygiene in practice

| Practice | Reason |
|---|---|
| Commit early, commit often, locally | Small commits are easy to reorder and rewrite |
| Rebase local work before opening a PR | Linear history, no merge noise |
| Never rewrite pushed history on `main` | Someone may have pulled it |
| Never `--no-verify` | Hooks exist to catch what you forgot. Skipping them is deciding you did not forget, which is exactly when you did |
| Never commit `.env`, keys, or dumps | gitleaks catches most; discipline catches the rest |
| Delete branches after merge | Keeps the branch list meaningful |

## 8. Co-authorship

This project is built with AI assistance. Commits made with Claude carry:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Stated openly. The buildathon is explicitly looking for people who **build with
AI**, and pretending otherwise in a repo where the commit cadence makes it
obvious would be both dishonest and transparent. The judgment being demonstrated
is in the architecture, the decisions, and the things deliberately *not* done —
not in whether the keystrokes were typed by hand.
