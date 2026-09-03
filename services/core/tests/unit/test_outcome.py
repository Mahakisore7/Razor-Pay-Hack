"""Outcome.reason_code is mandatory on every non-recovery kind (DOMAIN-MODEL
SS9): a case that ends without a reason is a case we cannot explain."""

import pytest

from recoup.domain.identifiers import CaseId, uuid7
from recoup.domain.money import Money
from recoup.domain.outcome import Outcome, OutcomeKind
from tests.factories import EPOCH


def test_recovered_outcome_needs_no_reason_code() -> None:
    outcome = Outcome(
        case_id=CaseId(uuid7()),
        kind=OutcomeKind.RECOVERED,
        recovered=Money(2_499_00),
        resolved_at=EPOCH,
        attributed_payment_id="pay_abc123",
        attributed_step_id="timed_retry",
    )
    assert outcome.reason_code is None


def test_partially_recovered_outcome_needs_no_reason_code() -> None:
    outcome = Outcome(
        case_id=CaseId(uuid7()),
        kind=OutcomeKind.PARTIALLY_RECOVERED,
        recovered=Money(1_000_00),
        resolved_at=EPOCH,
    )
    assert outcome.reason_code is None


@pytest.mark.parametrize(
    "kind",
    [OutcomeKind.LOST, OutcomeKind.EXPIRED, OutcomeKind.SUPPRESSED, OutcomeKind.ESCALATED],
)
def test_non_recovery_outcome_requires_a_reason_code(kind: OutcomeKind) -> None:
    with pytest.raises(ValueError, match="reason_code"):
        Outcome(case_id=CaseId(uuid7()), kind=kind, recovered=Money(0), resolved_at=EPOCH)


def test_non_recovery_outcome_with_a_reason_code_constructs() -> None:
    outcome = Outcome(
        case_id=CaseId(uuid7()),
        kind=OutcomeKind.EXPIRED,
        recovered=Money(0),
        resolved_at=EPOCH,
        reason_code="max_case_age_exceeded",
    )
    assert outcome.reason_code == "max_case_age_exceeded"
