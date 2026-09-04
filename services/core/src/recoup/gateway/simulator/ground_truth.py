"""Ground truth: the true cause of every simulated failure (RAZORPAY-
INTEGRATION SS6.1).

Write-only from the simulator's side, read-only from `bench.evaluation`'s --
enforced by `.importlinter`'s `ground-truth-is-write-only` contract, not
just this docstring. Diagnosis accuracy is measured against this; if any
pipeline component could read it while running, it could be "graded" on
information it never actually had access to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from recoup.domain.decline import DeclineCategory

__all__ = ["GroundTruthLog", "GroundTruthRecord"]


@dataclass(frozen=True, slots=True)
class GroundTruthRecord:
    payment_id: str
    customer_id: str
    true_cause: str  # e.g. "issuer_outage:HDFC", "salary_cycle", "network_fault", "success"
    decline_category: DeclineCategory | None  # None when the attempt succeeded
    would_have_recovered_unaided: bool  # the control-arm counterfactual
    occurred_at: datetime


class GroundTruthLog:
    """An append-only, in-memory log for one simulator instance / benchmark run."""

    def __init__(self) -> None:
        self._records: list[GroundTruthRecord] = []

    def record(self, entry: GroundTruthRecord) -> None:
        self._records.append(entry)

    def all(self) -> tuple[GroundTruthRecord, ...]:
        return tuple(self._records)
