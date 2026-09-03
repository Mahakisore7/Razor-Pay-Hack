"""Diagnosis is structural in this phase (DOMAIN-MODEL SS5) -- the statistical
slicing and LLM ranking that populate it are Phase 2+ work. These tests only
cover the one behaviour the value objects have on their own: `root_cause`
delegating to the top-ranked hypothesis.
"""

from recoup.domain.diagnosis import Diagnosis, DiagnosisMethod, Evidence, Hypothesis, RootCause
from recoup.domain.identifiers import CaseId, uuid7
from tests.factories import EPOCH


def _make_hypothesis(root_cause: str, confidence: float) -> Hypothesis:
    return Hypothesis(
        root_cause=RootCause(root_cause),
        confidence=confidence,
        evidence=(
            Evidence(
                slice_dimension="issuer",
                slice_value="HDFC",
                failure_rate=0.42,
                baseline_rate=0.05,
                sample_size=1200,
                z_statistic=8.1,
                p_value=0.0001,
            ),
        ),
        narration="HDFC failures spiked overnight.",
    )


def test_root_cause_is_the_top_ranked_hypothesis() -> None:
    diagnosis = Diagnosis(
        case_id=CaseId(uuid7()),
        hypotheses=(
            _make_hypothesis("issuer_outage", 0.9),
            _make_hypothesis("insufficient_funds", 0.3),
        ),
        method=DiagnosisMethod.STATISTICAL,
        computed_at=EPOCH,
        llm_model=None,
        fallback_reason=None,
    )
    assert diagnosis.root_cause == RootCause("issuer_outage")


def test_abstained_diagnosis_has_no_root_cause() -> None:
    diagnosis = Diagnosis(
        case_id=CaseId(uuid7()),
        hypotheses=(),
        method=DiagnosisMethod.ABSTAINED,
        computed_at=EPOCH,
        llm_model=None,
        fallback_reason="no significant slice",
    )
    assert diagnosis.root_cause is None
