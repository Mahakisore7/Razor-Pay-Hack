"""A1.7 -- the phase gate. Every number this project will ever report
depends on this holding: same seed in, byte-identical results out
(RAZORPAY-INTEGRATION SS6.2). A benchmark built on a non-deterministic
simulator is unfalsifiable, which is the exact failure mode this project
exists to avoid, so this is verified twice, independently, not once.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from recoup.domain.decline import DeclineCategory
from recoup.domain.money import Money
from recoup.gateway.interface import MandateDebitRequest, PaymentStatus, RetryRequest
from recoup.gateway.simulator.simulator import RazorpaySimulator

_AT = datetime(2026, 2, 27, 9, 0, tzinfo=UTC)  # pre-payday, so salary-cycle logic is exercised


async def _run_scenario(seed: int) -> list[dict[str, Any]]:
    """Drives one simulator instance through issuer outages, salary-cycle
    days, retries, and mandate debits -- exercising every phenomenon at
    once, since A1.7 must hold for the whole simulator, not just its
    simplest path."""
    sim = RazorpaySimulator(seed=seed)
    events: list[dict[str, Any]] = []

    for i in range(40):
        instrument = ["upi", "card", "netbanking", "wallet"][i % 4]
        issuer = f"issuer_{i % 5}"
        at = _AT + timedelta(hours=i)
        payment = sim.seed_payment(
            customer_id=f"cust_{i % 10}",
            amount=Money(100_00 * (i + 1)),
            method=instrument,
            issuer=issuer,
            at=at,
        )
        events.append(
            {
                "kind": "seed",
                "payment_id": payment.id,
                "status": payment.status.value,
                "error_reason": payment.error_reason,
            }
        )

        is_retryable_failure = (
            payment.status == PaymentStatus.FAILED
            and payment.error_reason is not None
            and DeclineCategory(payment.error_reason).retryable
        )
        if is_retryable_failure:
            result = await sim.retry_payment(
                RetryRequest(payment_id=payment.id, attempt=2, at=at + timedelta(hours=1))
            )
            events.append(
                {
                    "kind": "retry",
                    "payment_id": result.payment.id,
                    "status": result.payment.status.value,
                    "error_reason": result.payment.error_reason,
                }
            )

    for i in range(5):
        debit = await sim.present_mandate(
            MandateDebitRequest(
                mandate_id=f"mandate_{i}", amount=Money(50_000), due_at=_AT + timedelta(days=i)
            )
        )
        events.append(
            {
                "kind": "mandate",
                "payment_id": debit.payment.id,
                "status": debit.payment.status.value,
                "error_reason": debit.payment.error_reason,
            }
        )

    for record in sim.ground_truth.all():
        events.append(
            {
                "kind": "ground_truth",
                "payment_id": record.payment_id,
                "customer_id": record.customer_id,
                "true_cause": record.true_cause,
                "decline_category": (
                    record.decline_category.value if record.decline_category else None
                ),
                "would_have_recovered_unaided": record.would_have_recovered_unaided,
                "occurred_at": record.occurred_at.isoformat(),
            }
        )

    return events


def _canonical(events: list[dict[str, Any]]) -> str:
    return json.dumps(events, sort_keys=True, separators=(",", ":"))


async def test_two_independent_runs_at_the_same_seed_are_byte_identical() -> None:
    first = _canonical(await _run_scenario(seed=2026))
    second = _canonical(await _run_scenario(seed=2026))
    assert first == second


async def test_two_independent_runs_at_the_same_seed_are_byte_identical_verified_again() -> None:
    """A second, independent pair -- a different seed, a fresh pair of
    simulator instances -- so this gate isn't resting on one lucky seed."""
    first = _canonical(await _run_scenario(seed=99001))
    second = _canonical(await _run_scenario(seed=99001))
    assert first == second


async def test_different_seeds_are_not_trivially_identical() -> None:
    """Guards against a determinism check that would pass even if the
    simulator ignored its seed entirely and always did the same thing."""
    a = _canonical(await _run_scenario(seed=1))
    b = _canonical(await _run_scenario(seed=2))
    assert a != b
