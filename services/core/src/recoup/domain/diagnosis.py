"""Diagnosis -- ranked hypotheses about why a case's payment failed (DOMAIN-MODEL SS5).

Structural only in this phase: the statistical slicing and LLM ranking that
populate these types are Phase 2+ work. `Case` needs a concrete `Diagnosis`
type to reference (DOMAIN-MODEL SS4), so the value objects are defined here
now, matching DOMAIN-MODEL SS5 exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

from recoup.domain.identifiers import CaseId

__all__ = ["Diagnosis", "DiagnosisMethod", "Evidence", "Hypothesis", "RootCause"]

# A playbook's `applies_to.root_cause` matches against this. Root causes are
# defined by playbook config, not a closed code-level enum
# (ENGINEERING-STANDARDS SS6: domain thresholds live in versioned YAML, not
# in code) -- new root causes can be introduced by a playbook without a code
# change.
RootCause = NewType("RootCause", str)


class DiagnosisMethod(StrEnum):
    STATISTICAL = "statistical"
    LLM_RANKED = "llm_ranked"
    ABSTAINED = "abstained"


@dataclass(frozen=True, slots=True)
class Evidence:
    slice_dimension: str  # "issuer" | "bin_range" | "psp_route" | ...
    slice_value: str
    failure_rate: float
    baseline_rate: float
    sample_size: int
    z_statistic: float
    p_value: float


@dataclass(frozen=True, slots=True)
class Hypothesis:
    root_cause: RootCause
    confidence: float  # [0, 1]
    evidence: tuple[Evidence, ...]
    narration: str | None  # LLM-written; display only, never parsed by code


@dataclass(frozen=True, slots=True)
class Diagnosis:
    case_id: CaseId
    hypotheses: tuple[Hypothesis, ...]  # ranked, descending confidence
    method: DiagnosisMethod
    computed_at: datetime
    llm_model: str | None  # recorded when a model participated
    fallback_reason: str | None  # why we fell back, if we did

    @property
    def root_cause(self) -> RootCause | None:
        return self.hypotheses[0].root_cause if self.hypotheses else None
