"""Scores diagnosis and recovery against the simulator's ground truth
(RAZORPAY-INTEGRATION SS6.1).

The one module allowed to read `GroundTruthLog` -- everything upstream of
it (detection, diagnosis, policy, planning, execution) must infer the
world from observable signals alone, never from the answer key. Enforced
by `.importlinter`'s `ground-truth-is-write-only` contract, not just this
docstring.

The actual scoring (diagnosis accuracy, arm comparison, the benchmark
report) is Phase 3 work (ROADMAP P3); this module exists now so the
import-linter contract above has something real to permit, and so T1.9's
determinism gate has an evaluator-side consumer to prove the boundary
against.
"""

from __future__ import annotations

from recoup.gateway.simulator.ground_truth import GroundTruthLog, GroundTruthRecord

__all__ = ["read_ground_truth"]


def read_ground_truth(log: GroundTruthLog) -> tuple[GroundTruthRecord, ...]:
    return log.all()
