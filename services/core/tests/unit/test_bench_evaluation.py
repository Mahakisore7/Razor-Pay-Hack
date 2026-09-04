"""bench.evaluation is the one module allowed to read ground truth
(RAZORPAY-INTEGRATION SS6.1) -- the actual scoring is Phase 3 work, but the
read path this phase adds is exercised here."""

from datetime import UTC, datetime

from recoup.bench.evaluation import read_ground_truth
from recoup.domain.decline import DeclineCategory
from recoup.gateway.simulator.ground_truth import GroundTruthLog, GroundTruthRecord


def test_read_ground_truth_returns_the_logs_records() -> None:
    log = GroundTruthLog()
    record = GroundTruthRecord(
        payment_id="pay_1",
        customer_id="cust_1",
        true_cause="success",
        decline_category=None,
        would_have_recovered_unaided=False,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    log.record(record)
    assert read_ground_truth(log) == (record,)


def test_read_ground_truth_on_an_empty_log() -> None:
    assert read_ground_truth(GroundTruthLog()) == ()


def test_read_ground_truth_preserves_decline_category() -> None:
    log = GroundTruthLog()
    log.record(
        GroundTruthRecord(
            payment_id="pay_1",
            customer_id="cust_1",
            true_cause="issuer_outage:HDFC",
            decline_category=DeclineCategory.ISSUER_DOWN,
            would_have_recovered_unaided=True,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    assert read_ground_truth(log)[0].decline_category == DeclineCategory.ISSUER_DOWN
