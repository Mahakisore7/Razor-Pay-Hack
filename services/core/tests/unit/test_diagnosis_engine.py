"""Unit tests for `recoup.diagnosis.engine` (T2.3) -- pure, in-memory: a
`CaseId`, a `DeclineCategory | None`, and a clock in, a `Diagnosis` out.
"""

from datetime import UTC, datetime

from recoup.diagnosis.engine import DiagnosisEngine, stub_diagnose
from recoup.domain.decline import DeclineCategory
from recoup.domain.diagnosis import DiagnosisMethod
from recoup.domain.identifiers import CaseId, uuid7
from recoup.platform.clock import FrozenClock

_CLOCK = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))


def test_stub_diagnose_reports_the_decline_category_as_the_sole_hypothesis() -> None:
    case_id = CaseId(uuid7())
    diagnosis = stub_diagnose(case_id, DeclineCategory.INSUFFICIENT_FUNDS, _CLOCK)

    assert diagnosis.case_id == case_id
    assert diagnosis.method == DiagnosisMethod.STATISTICAL
    assert diagnosis.computed_at == _CLOCK.now()
    assert diagnosis.llm_model is None
    assert diagnosis.fallback_reason is None
    assert len(diagnosis.hypotheses) == 1

    hypothesis = diagnosis.hypotheses[0]
    assert hypothesis.root_cause == "insufficient_funds"
    assert hypothesis.confidence == 1.0
    assert hypothesis.evidence == ()
    assert hypothesis.narration is None
    assert diagnosis.root_cause == "insufficient_funds"


def test_stub_diagnose_abstains_when_there_is_no_decline_category() -> None:
    case_id = CaseId(uuid7())
    diagnosis = stub_diagnose(case_id, None, _CLOCK)

    assert diagnosis.method == DiagnosisMethod.ABSTAINED
    assert diagnosis.hypotheses == ()
    assert diagnosis.root_cause is None
    assert diagnosis.fallback_reason == "signal carried no decline category"


def test_stub_diagnose_uses_the_injected_clock_not_wall_time() -> None:
    at = datetime(2030, 5, 17, 3, 30, tzinfo=UTC)
    clock = FrozenClock(at)
    diagnosis = stub_diagnose(CaseId(uuid7()), DeclineCategory.MANDATE_REVOKED, clock)
    assert diagnosis.computed_at == at


def test_stub_diagnose_satisfies_the_diagnosis_engine_protocol() -> None:
    """A2.9: the stub is swappable without touching any caller precisely
    because callers depend on this Protocol's call signature, not on
    `stub_diagnose` itself."""
    engine: DiagnosisEngine = stub_diagnose
    diagnosis = engine(CaseId(uuid7()), DeclineCategory.ISSUER_DOWN, _CLOCK)
    assert diagnosis.root_cause == "issuer_down"
