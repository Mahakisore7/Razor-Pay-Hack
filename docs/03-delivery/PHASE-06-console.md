# Phase 6 — Ops Console

| Field | Value |
|---|---|
| Duration | 3 days |
| Depends on | P5 |
| Blocks | P8 |
| Tag | `v0.6.0` |
| Status | **Post-submission** |

**Goal:** a Next.js operations console that lets a reviewer drive the entire demo
from a browser.

Scope discipline: shadcn defaults, no custom design system. The console exists to
make the pipeline legible, not to be a design showcase. Time spent here does not
move any judging criterion that the backend does not already move.

---

## Tasks

### T6.1 — Foundation
- [ ] App Router layout, server components by default
- [ ] API types generated from OpenAPI, never hand-written
- [ ] Zod validation at the boundary
- [ ] Auth: session cookie, role-gated routes
- [ ] Error boundary and loading states

### T6.2 — Dashboard (Priya)
- [ ] At-risk value by leak class, over a date range
- [ ] Case pipeline funnel by state
- [ ] Recovery rate trend, treatment vs control
- [ ] Approval queue depth
- [ ] Kill-switch status, prominently

### T6.3 — Case list and timeline
- [ ] Filterable list: state, arm, leak class, amount, date
- [ ] Case detail: at-risk, diagnosis with evidence, plan, outcome
- [ ] **Full timeline including policy denials**, visually distinct from actions
- [ ] `chain_valid` badge, computed on read
- [ ] SSE live updates

### T6.4 — Approvals
- [ ] Pending queue with case context and proposed actions
- [ ] Approve / reject; reason mandatory on reject
- [ ] Optimistic update with rollback on failure

### T6.5 — Compliance view (Meera)
- [ ] Denials by rule over time
- [ ] Contact fatigue distribution
- [ ] Opt-out rate trend
- [ ] Quiet-hour violations (expected: a flat zero line)
- [ ] Consent coverage by channel

### T6.6 — Economics view (Arjun)
- [ ] Spend by channel
- [ ] Cost per rupee recovered, trend
- [ ] Daily cap headroom
- [ ] Per-playbook profitability

### T6.7 — Benchmark viewer
- [ ] Run list; report rendered in-app
- [ ] Arm comparison chart with confidence intervals
- [ ] Exception list, filterable, not truncated

### T6.8 — Kill switch control
- [ ] Trip and clear, reason mandatory
- [ ] Confirmation dialog stating the blast radius
- [ ] Current state and actor visible on every page

---

## Acceptance criteria

| # | Criterion |
|---|---|
| A6.1 | A reviewer drives the full demo with no terminal |
| A6.2 | The case timeline shows denials as prominently as actions |
| A6.3 | Kill switch is trippable from the UI and visibly takes effect |
| A6.4 | `tsc --strict` clean; Playwright suite green |
| A6.5 | Dashboard p95 load under 1.5s (TR-46) |
| A6.6 | Charts follow the project palette and are legible in light and dark |

A6.2 is the one that matters. Showing what the system refused to do, at equal
weight to what it did, is the product's central claim rendered as UI.

---

## PR breakdown

| PR | Branch | Contents |
|---|---|---|
| 1 | `feat(console): foundation and auth` | T6.1 |
| 2 | `feat(console): dashboard` | T6.2 |
| 3 | `feat(console): case timeline` | T6.3 |
| 4 | `feat(console): approvals` | T6.4 |
| 5 | `feat(console): compliance view` | T6.5 |
| 6 | `feat(console): economics view` | T6.6 |
| 7 | `feat(console): benchmark viewer` | T6.7, T6.8 |

---

## Risks

| Risk | Mitigation |
|---|---|
| UI polish consumes time the backend needs | Hard timebox: 3 days. shadcn defaults only. If it slips, ship a static HTML report instead (roadmap cut order item 1). |
| Hand-written API types drift from the backend | Generated from OpenAPI. A backend change that breaks the console then fails at compile time. |
| Charts become a design project | Follow the dataviz guidance once, apply consistently, stop. |
