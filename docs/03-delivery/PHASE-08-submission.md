# Phase 8 — Submission

| Field | Value |
|---|---|
| Duration | 2 days |
| Depends on | P6, P7 |
| Tag | `v1.0.0` |

**Goal:** package the work so a reviewer with no context reaches the interesting
part in under five minutes.

The buildathon reads: a repo that runs, a 5-minute video, and "what broke and how
you got out." That last one they read first.

---

## Tasks

### T8.1 — README
- [ ] What it is, in two sentences, above the fold
- [ ] The headline benchmark number with its confidence interval
- [ ] Architecture diagram
- [ ] Quickstart: `make demo`, no credentials
- [ ] Where AI is used and where it is deliberately not
- [ ] **Explicit real-vs-simulated table** (RAZORPAY-INTEGRATION §9)
- [ ] Links into the doc set

### T8.2 — Failure log
- [ ] [FAILURE-LOG](../05-submission/FAILURE-LOG.md) finalised
- [ ] Written **during** the build, not reconstructed after
- [ ] Each entry: what broke, how it was found, the diagnosis, the fix, what it
      changed about the design

### T8.3 — Clean-clone verification
- [ ] Fresh clone, fresh machine, no cached images
- [ ] `make demo` end to end, timed (< 5 min, TR-47)
- [ ] **A third party who has not seen the repo does this unaided**
- [ ] Every friction point either fixed or documented

### T8.4 — Pitch video (5 min)
- [ ] Script per [PITCH-SCRIPT](../05-submission/PITCH-SCRIPT.md)
- [ ] Show: the problem, one case end to end including a policy denial, the
      benchmark with its control arm, the kill switch
- [ ] Unlisted upload

### T8.5 — Final benchmark
- [ ] Full run at a fixed published seed
- [ ] Report committed to the repo
- [ ] Numbers in README and video match the committed report exactly

### T8.6 — Submission form
- [ ] The 12 fields per [SUBMISSION-CHECKLIST](../05-submission/SUBMISSION-CHECKLIST.md)
- [ ] Repo public
- [ ] `v1.0.0` tagged, CHANGELOG generated

---

## Acceptance criteria

| # | Criterion |
|---|---|
| A8.1 | A stranger clones and reaches a benchmark report unaided |
| A8.2 | README numbers match the committed report exactly |
| A8.3 | The video is under 5 minutes and shows working software, not slides |
| A8.4 | FAILURE-LOG contains real, specific, dated failures |
| A8.5 | The repo is public, CI green on `main`, `v1.0.0` tagged |
| A8.6 | No secret in any commit, ever — verified across full history |

---

## The honesty pass

Before submitting, re-read every claim in the README, the video script, and the
docs against what the code actually does. Specifically:

- [ ] Does the README claim any capability that is simulated? Is that stated?
- [ ] Does the benchmark report state its limitations before its headline?
- [ ] Is the exception list complete?
- [ ] Are negative results (a losing bandit, an LLM that did not beat z-score
      ranking) reported rather than omitted?
- [ ] Does anything imply real SMS is sent? (It is not.)
- [ ] Does anything imply real merchant data? (It does not.)

A reviewer who finds one overstatement discounts everything else. The measurement
doctrine in [METRICS](../01-product/METRICS-AND-KPIS.md) only means something if
it is applied to our own claims about the project, not only to the numbers inside it.

---

## Risks

| Risk | Mitigation |
|---|---|
| Clean-clone works locally, fails elsewhere | Verified by a third party on a different machine. Non-negotiable. |
| Video runs long | Script and rehearse. Cut the architecture explanation before cutting the demo. |
| Numbers drift between README, video, and report | One published seed; all three regenerated from the same run. |
| Failure log reads as backfilled | Written continuously from P0. Dated entries with real error output. |
