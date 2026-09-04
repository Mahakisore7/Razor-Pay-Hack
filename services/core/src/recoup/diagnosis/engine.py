"""Stub diagnosis (PHASE-02 T2.3): a `Diagnosis` produced directly from the
decline category already on the signal that opened the case -- no slicing,
no significance testing, no LLM. Those are TR-10..TR-15, explicitly P5 work
(PHASE-02-closed-loop.md: "diagnosis returns the decline category directly
... intelligence is P5").

`DiagnosisEngine` is the seam: every future caller depends on that call
signature, not on `stub_diagnose` itself, so P5's real statistical/LLM
engine drops in as a straight substitution (A2.9) -- the same shape
`detection.detectors.base.Detector` already uses for L1-L3.
"""

from __future__ import annotations

from typing import Protocol

from recoup.domain.decline import DeclineCategory
from recoup.domain.diagnosis import Diagnosis, DiagnosisMethod, Hypothesis, RootCause
from recoup.domain.identifiers import CaseId
from recoup.platform.clock import Clock

__all__ = ["DiagnosisEngine", "stub_diagnose"]


class DiagnosisEngine(Protocol):
    def __call__(
        self, case_id: CaseId, decline_category: DeclineCategory | None, clock: Clock
    ) -> Diagnosis: ...


def stub_diagnose(
    case_id: CaseId, decline_category: DeclineCategory | None, clock: Clock
) -> Diagnosis:
    """The decline category *is* the diagnosis: one hypothesis, full
    confidence, no evidence to show for it since none was computed.

    A signal with no decline category (T2.2's L3 halted-subscription
    detector never sets one -- a halt isn't itself a declined payment) has
    nothing to name a `root_cause` from, so the diagnosis abstains.
    `ABSTAINED` is a first-class outcome (DOMAIN-MODEL SS5), not an error:
    it is what routes a case to a generic playbook rather than a
    root-cause-specific one, once one exists.
    """
    now = clock.now()
    if decline_category is None:
        return Diagnosis(
            case_id=case_id,
            hypotheses=(),
            method=DiagnosisMethod.ABSTAINED,
            computed_at=now,
            llm_model=None,
            fallback_reason="signal carried no decline category",
        )
    hypothesis = Hypothesis(
        root_cause=RootCause(decline_category.value),
        confidence=1.0,
        evidence=(),
        narration=None,
    )
    return Diagnosis(
        case_id=case_id,
        hypotheses=(hypothesis,),
        method=DiagnosisMethod.STATISTICAL,
        computed_at=now,
        llm_model=None,
        fallback_reason=None,
    )
