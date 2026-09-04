"""GroundTruthLog is the answer key (RAZORPAY-INTEGRATION SS6.1): append-only
from the simulator's side, read back only by whoever is scoring against it."""

from datetime import UTC, datetime

from recoup.domain.decline import DeclineCategory
from recoup.gateway.simulator.ground_truth import GroundTruthLog, GroundTruthRecord


def _record(payment_id: str) -> GroundTruthRecord:
    return GroundTruthRecord(
        payment_id=payment_id,
        customer_id="cust_1",
        true_cause="issuer_outage:HDFC",
        decline_category=DeclineCategory.ISSUER_DOWN,
        would_have_recovered_unaided=False,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_empty_log_returns_no_records() -> None:
    assert GroundTruthLog().all() == ()


def test_records_are_returned_in_append_order() -> None:
    log = GroundTruthLog()
    log.record(_record("pay_1"))
    log.record(_record("pay_2"))
    assert [r.payment_id for r in log.all()] == ["pay_1", "pay_2"]


def test_all_returns_an_immutable_snapshot() -> None:
    log = GroundTruthLog()
    log.record(_record("pay_1"))
    snapshot = log.all()
    log.record(_record("pay_2"))
    assert len(snapshot) == 1  # the earlier snapshot is unaffected by the later write
