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
| O-1 | `gh` CLI is unauthenticated in the dev environment | PRs and branch protection on `main` (T0.7) can't be set up from the CLI; branches are pushed and left for manual PR creation | Open — needs `gh auth login` or manual setup via the GitHub web UI |

---

## Entries

<!-- Newest first. Add entries as they happen. -->

### F-007 — a "safe" git revert took uncommitted work with it
**Date:** 2026-09-03 · **Phase:** P0 · **Severity:** low

**What broke**

While deliberately tripping the gitleaks gate (the same "verify it actually
fails" discipline as F-002), a scratch commit with a fake secret was
undone with `git reset --hard <old-sha>`. That SHA was correct for undoing
the scratch commit, but the working tree at the time also held unrelated,
legitimate uncommitted edits — a dependency security patch and CI-prep
config changes made moments earlier. `reset --hard` discards *all*
uncommitted changes to tracked files, not just the ones related to what
you're trying to undo, and none of that was pushed anywhere to fall back on.

**How it was found**

Immediately — Edit/Write tool results on the next file touched reported
the file's on-disk content no longer matched what had just been written,
which was the tell that something upstream had reverted it.

**Root cause**

Treated "undo my last commit" as interchangeable with "reset to a known-good
SHA," when the two are only equivalent if the working tree is otherwise
clean. It wasn't. `reset --hard HEAD~1` (relative, and only ever safe on a
tree with nothing else uncommitted) is a different operation from
`reset --hard <sha>` (absolute, and blind to everything that changed since).

**Fix**

Recreated the three lost edits from memory (their content was known
precisely, having just been written) and regenerated the two lockfiles
from the restored source files rather than by hand. No data was
unrecoverable — the fix was re-deriving, not un-losing.

**What it changed about the design**

Not the codebase — the procedure. **A destructive git operation used to
undo a scratch commit must run on a working tree with nothing else
uncommitted first.** In this session, that now means: commit everything
legitimate, confirm `git status` is clean, *then* make the scratch commit
being tested, so the revert afterward is an unambiguous `reset --hard
HEAD~1` with nothing else at stake. Applied for real two sections later in
this same log, when the same gate needed re-tripping with a better fake
secret.

---

### F-006 — a config key that used to work stopped silently, one major version later
**Date:** 2026-09-02 · **Phase:** P0 · **Severity:** medium

**What broke**

```
[ERR_PNPM_IGNORED_BUILDS] Ignored build scripts: @tailwindcss/oxide@4.1.14, esbuild@0.28.2, sharp@0.34.5, unrs-resolver@1.12.2
```

`pnpm install --frozen-lockfile` inside the console's Docker build (T0.5)
failed outright on a fresh container, even with
`package.json#pnpm.onlyBuiltDependencies` listing exactly those four
packages.

**How it was found**

First `docker compose build console`. Confusingly, the *identical* install
succeeded cleanly on the host machine every time, frozen lockfile and all —
which pointed at environment state, not the config, and cost real time
investigating the wrong layer (BuildKit cache mounts, a stale global pnpm
store) before the actual cause surfaced.

**Root cause**

`pnpm config get onlyBuiltDependencies` echoed the array back correctly,
and `pnpm approve-builds --all` reported nothing pending — both signals
that looked like the config was live. It wasn't: pnpm v11 replaced
`package.json#pnpm.onlyBuiltDependencies` (an array, pnpm v10) with
`pnpm-workspace.yaml`'s `allowBuilds` (a `name -> boolean` map). The old
key is silently ignored rather than rejected, so nothing in the tool's own
output said the config was stale. The host machine only ever "worked" by
coincidence, unrelated to this file: pnpm v11 also tracks per-user, global
build approvals outside any project, and this host had already approved
these exact common packages once, in some earlier, unrelated project.
A brand-new container has no such history, so it was the only environment
telling the truth.

**Fix**

`pnpm-workspace.yaml`'s `allowBuilds` map, with the four packages listed
explicitly. Verified in an actual fresh Linux container (not the host) with
a warm-but-otherwise-clean pnpm store, so the fix was confirmed against the
environment that had been failing, not the one that had been accidentally
passing.

**What it changed about the design**

**A config value that "looks read" is not the same as a config value that
is applied** — `pnpm config get` will happily echo a key it does no longer
act on. And more generally: when local and CI/Docker disagree on something
that reads the same files, the local machine's *history* — not just its
current config — is a live suspect, not just the config itself.

---

### F-005 — a deterministic Redis protocol error that took an afternoon to trace to two unrelated bugs
**Date:** 2026-09-02 · **Phase:** P0 · **Severity:** medium

**What broke**

Adding a real integration test for `db.ping()`/`cache.ping()` against
testcontainers-backed Postgres and Redis (rather than the mocked version in
the unit suite — ENGINEERING-STANDARDS 4.2 is explicit that a mocked ping
tests the mock), the Redis half failed 100% of the time, on every run:

```
redis.exceptions.ResponseError: unknown command 'HELLO'
```

**How it was found**

First run of `tests/integration/test_platform_connectivity.py`. Reproducible
on every attempt, in isolation and in the full suite, which ruled out a flake
immediately and justified the time spent bisecting it.

**Investigation**

Two genuinely separate bugs were hiding behind one symptom.

**Bug 1 — event-loop-scoped socket teardown.** Bisection (running each
combination of {get_settings, raw asyncpg, SQLAlchemy async engine} before a
Redis ping, in isolation) showed the failure needed *both* a real Postgres
connection through SQLAlchemy's async engine *and* `get_settings()` having
been called, in the same event loop, immediately before a Redis connection.
`engine.dispose()` only *schedules* the underlying OS socket's close — it does
not complete synchronously — and a Redis connection opened immediately
afterward could be handed a file descriptor the OS had just freed before
asyncio's selector finished unregistering the old callback for it, corrupting
the next read. A bare `await asyncio.sleep(0)` reproducibly fixed a minimal
standalone repro. It did not fix the actual pytest run, which pointed at a
second issue.

**Bug 2 — mismatched fixture and event-loop scope.** Restructuring the test
to start both containers once and create the cached engine/client once,
disposing both only at the very end (matching how `db.py`/`cache.py` are
actually used in production — process-lifetime singletons, never disposed
between requests) fixed the original symptom entirely. But it introduced a
new one at teardown: `AttributeError: 'NoneType' object has no attribute
'send'`, deep in asyncpg's write path. pytest-asyncio's *default* event-loop
scope is per-test-function, while my fixture was module-scoped — so the
engine's connections were established on one event loop and torn down on a
different, already-closed one. Setting `loop_scope="module"` on both the
fixture and the module's `pytest.mark.asyncio` mark fixed it.

**Bug 3 (the one that broke the full suite specifically) — a second,
uncleared `lru_cache`.** With both of the above fixed, the test still failed,
but *only* when run after the rest of the suite, never in isolation — with a
completely different, much more mundane error:
`asyncpg.exceptions.InvalidPasswordError`. `recoup.platform.config.get_settings()`
is its own `@lru_cache`, separate from `get_engine()`'s and `get_redis()`'s.
`test_health.py`'s fixture builds `create_app()`, which calls `get_settings()`
once, caching a `Settings` object built from whatever environment existed at
that point — the defaults, since no `DATABASE_URL` was set yet. My
integration test's fixture set `DATABASE_URL` via `monkeypatch` and cleared
`get_engine`'s and `get_redis`'s caches, but never `get_settings`'s — so
`get_engine()` kept calling the *already-cached* `Settings` from the earlier
test, silently ignoring the env vars I had just set.

**Fix**

- Test containers and the cached clients are now created once per module and
  disposed once, matching the real usage pattern rather than fighting it.
- `loop_scope="module"` on the fixture and the module's asyncio mark.
- The fixture now clears `config.get_settings`, `db.get_engine`, and
  `cache.get_redis` together, every time, in both directions (setup and
  teardown) — see `tests/integration/test_platform_connectivity.py`.

**What it changed about the design**

Three lessons, at three different levels:

1. **A symptom can have more than one cause.** The first fix (event-loop
   yield) genuinely worked on a minimal repro and was real — it just was not
   the whole story. Confirming a fix by re-running the *actual* failing case
   (not a simplified stand-in for it) is what caught that it wasn't sufficient.
2. **Any module-level cache that config-carrying code depends on must be
   cleared as a set, not individually.** `get_settings`, `get_engine`, and
   `get_redis` form a dependency chain; clearing two of three silently leaves
   the chain pointing at stale state. If a fourth cached resource is ever
   added to `recoup.platform`, this is the reminder to audit the full chain,
   not just the new piece.
3. **Test resources should mirror production lifecycle, not fight it.**
   `db.py` and `cache.py` are deliberately process-lifetime singletons
   (ARCHITECTURE). A test that disposes and recreates them per test case is
   testing a usage pattern the code was never designed for, and paid for that
   mismatch in debugging time. The fix that actually stuck was the one that
   made the test's resource lifecycle match the real one.

---

### F-004 — mypy cannot see a real pydantic-settings constructor kwarg
**Date:** 2026-09-02 · **Phase:** P0 · **Severity:** low

**What broke**

```
error: Unexpected keyword argument "_env_file" for "Settings"  [call-arg]
```

**How it was found**

Writing settings tests that pass `_env_file=None` to force `Settings` to ignore
any `.env` file on disk and read only what the test set up.

**Root cause**

`_env_file` is a real `pydantic-settings` constructor parameter — it overrides
`SettingsConfigDict.env_file` per call. mypy's synthesized `__init__` for a
`BaseModel` subclass is built from declared *fields* only, and `_env_file` is
not a field, so it does not exist in the synthesized signature.

Tried enabling the `pydantic.mypy` plugin first, on the theory it would widen
the synthesized `__init__` to accept the validator-compatible input types
(a plain `str` where a field is typed `SecretStr`, hit in the same file). It
did not help — that gap is unrelated to what the plugin actually does, and
enabling it fixed nothing while adding a dependency. Reverted.

**Fix**

Two fixes, for two different problems that looked like one:

1. For `_env_file`, a single wrapper function
   (`_settings_ignoring_any_local_dotenv`) carries one commented
   `type: ignore[call-arg]`, rather than repeating the comment at every call site.
2. For the `SecretStr`/`Literal` mismatch, stopped constructing `Settings` via
   keyword arguments entirely and drove every test through
   `monkeypatch.setenv` instead — which is how `Settings` is actually built in
   production, sidesteps the typing mismatch, and is a more honest test in
   its own right.

**What it changed about the design**

Nothing structural. The lesson is procedural: when a type error looks like it
might be a systemic gap (worth a plugin, a config change), verify that
hypothesis against the actual error *before* changing shared configuration.
The pydantic-mypy plugin change was reverted within the same commit rather than
left in as unexplained scaffolding.

---

### F-003 — a settings test passed or failed depending on who ran it
**Date:** 2026-09-02 · **Phase:** P0 · **Severity:** medium

**What broke**

```
AssertionError: assert SecretStr('**********') is None
```

`test_settings_load_from_defaults` asserted `settings.anthropic_api_key is None`
on a supposedly clean `Settings()` instance. It failed on this machine and
would pass on another.

**How it was found**

First run of the new settings test suite (T0.9), immediately after adding
`anthropic_api_key: SecretStr | None` to `Settings`.

**Root cause**

`Settings(_env_file=None)` disables reading a `.env` *file* — it does not
touch `os.environ`. This machine has `ANTHROPIC_API_KEY` set ambiently in the
shell (Claude Code itself resolves credentials from it, per this project's own
tooling), so `pydantic-settings` picked it up from the real environment and the
"clean" instance was not clean at all.

**Fix**

An autouse fixture (`_clean_environment`) that `monkeypatch.delenv`s every key
`Settings` reads, before each test. The test no longer depends on the shell
it happens to run in.

**What it changed about the design**

This is the generalisable one. **A test that reads real environment variables
is not a unit test — it is a unit test on this machine, on this day.** Every
settings test in this project now explicitly clears its environment first
rather than assuming a blank slate. Recorded here because the same class of
bug will recur the moment a second settings-reading module is added (the
Razorpay client in Phase 7 is the obvious future instance), and this entry is
the reminder to clear its env vars too, not just this file's.

---

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
