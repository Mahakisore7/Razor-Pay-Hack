# Phase 7 — Live Razorpay Integration

| Field | Value |
|---|---|
| Duration | 2 days |
| Depends on | P5 |
| Blocks | P8 |
| Tag | `v0.7.0` |
| Status | **Post-submission** |

**Goal:** the real Razorpay test-mode client behind the existing `PaymentGateway`
interface, so the same pipeline drives real sandbox objects.

The simulator remains the default. Live mode is opt-in, and the demo never
depends on network access — a reviewer with no Razorpay account must still get
the full experience.

---

## Tasks

### T7.1 — Client implementation
- [ ] `RazorpayClient` implementing `PaymentGateway`
- [ ] httpx async, explicit connect and read timeouts (TR-51)
- [ ] **Startup refuses any key not prefixed `rzp_test_`** (TR-49)
- [ ] Auth, error mapping to domain exceptions

### T7.2 — Resilience
- [ ] Retry with exponential backoff and jitter on 5xx and 429 **only**
- [ ] 4xx never retried
- [ ] Circuit breaker: opens after 5 consecutive failures, half-open after 30s
- [ ] **Open circuit surfaces as DEFER, not DENY** — a provider outage is a
      timing problem, and denying would drop recoverable cases
- [ ] Token-bucket rate limiting per endpoint class
- [ ] `X-Razorpay-Idempotency-Key` where supported

### T7.3 — Gateway conformance suite
- [ ] One test suite run against **both** implementations
- [ ] Asserts identical contract: return shapes, error taxonomy, idempotency
      semantics
- [ ] A simulator that diverges from the client would let us pass tests against
      fiction — this suite is what prevents that

### T7.4 — Webhook registration
- [ ] `make tunnel` wrapping ngrok
- [ ] Documented dashboard registration steps
- [ ] Webhook secret in `.env`
- [ ] Verified end to end with a real delivery

### T7.5 — Live scenarios
- [ ] Create a test-mode order, fail the payment, observe the L1 signal
- [ ] Create a subscription, fail a debit, observe L2
- [ ] Halt a subscription, observe L3
- [ ] Generate a payment link, pay it in a browser, observe attribution close the
      case as `RECOVERED`
- [ ] Record the flow for the pitch video

### T7.6 — Documentation
- [ ] Setup guide in the README
- [ ] Decline-code mapping verified against real sandbox responses; any
      divergence from the assumed taxonomy corrected and logged in FAILURE-LOG

---

## Acceptance criteria

| # | Criterion |
|---|---|
| A7.1 | With test keys set, the pipeline drives real Razorpay test-mode objects |
| A7.2 | Without keys, the simulator runs unchanged — no code path differs |
| A7.3 | A production-looking key (`rzp_live_*`) fails startup with a clear message |
| A7.4 | The conformance suite passes against both implementations |
| A7.5 | A real payment link, paid in a browser, closes a case as `RECOVERED` |
| A7.6 | Circuit breaker verified by pointing at an unreachable host |

A7.5 is the demo money shot: a real link, paid in a real browser, closing a real
case with a complete audit trail.

---

## PR breakdown

| PR | Branch | Contents |
|---|---|---|
| 1 | `feat/razorpay-client` | T7.1 |
| 2 | `feat/gateway-resilience` | T7.2 |
| 3 | `test/gateway-conformance` | T7.3 |
| 4 | `feat/webhook-tunnel-setup` | T7.4 |
| 5 | `docs/live-mode-setup` | T7.5, T7.6 |

---

## Risks

| Risk | Mitigation |
|---|---|
| Sandbox behaviour differs from assumptions | The conformance suite surfaces it. Divergences are corrected and recorded in FAILURE-LOG — this is exactly the "what broke" material the buildathon asks for. |
| Rate limits hit during testing | Token bucket sized conservatively; the simulator is used for volume. |
| Tunnel flakiness derails the demo | The recorded video is the fallback. The demo never depends on a live tunnel. |
| Accidentally using live keys | Startup refusal on the key prefix. Structural, not procedural. |
