"""Plan -- a playbook instantiated for one case (DOMAIN-MODEL SS6.2).

The playbook itself (versioned YAML, loaded and validated at startup) is
Phase 2+ work (ARCHITECTURE's `planning/playbooks/`); this is the pure
result of instantiating one, which is what `Case.plan` references now.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from recoup.domain.identifiers import CaseId
from recoup.domain.money import Money

__all__ = ["Plan", "PlannedStep"]


@dataclass(frozen=True, slots=True)
class PlannedStep:
    """One step of a `Plan`, due at a specific time.

    `step_id` must exist in the referenced playbook version -- enforced by
    whatever builds the `Plan` (the planner), which has the playbook at
    hand; a bare `PlannedStep` does not.
    """

    step_id: str
    due_at: datetime
    expected_cost: Money


@dataclass(frozen=True, slots=True)
class Plan:
    case_id: CaseId
    playbook_id: str
    playbook_version: int  # pinned, so a playbook edit never retroactively changes a running case
    steps: tuple[PlannedStep, ...]  # ordered
    total_expected_cost: Money
    created_at: datetime

    def __post_init__(self) -> None:
        for earlier, later in zip(self.steps, self.steps[1:], strict=False):
            if later.due_at < earlier.due_at:
                raise ValueError(
                    "Plan.steps due_at must be monotonically non-decreasing, "
                    f"got {earlier.due_at} before {later.due_at}"
                )
