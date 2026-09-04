"""Playbook YAML schema (DOMAIN-MODEL SS6.1, TR-16).

Pydantic, not a hand-rolled dict walk: a malformed playbook must fail with a
precise, field-level error at load time, not a `KeyError` the first time a
case reaches planning. `extra="forbid"` everywhere -- a typo'd field name in
a playbook is exactly the kind of mistake schema validation exists to catch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recoup.domain.action import Channel
from recoup.domain.signal import LeakClass

__all__ = [
    "AppliesTo",
    "Playbook",
    "PlaybookStep",
    "SkipIf",
    "StepGuard",
    "TimingSpec",
]


class AppliesTo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A free-form string, not `RootCause` -- new root causes can be
    # introduced by a playbook without a code change (domain/diagnosis.py).
    root_cause: str
    leak_classes: tuple[LeakClass, ...] = Field(min_length=1)


class TimingSpec(BaseModel):
    """Fixed-schedule timing only (TR-19's bandit is P5): `fixed` anchors to
    the plan's own creation, `relative` to an earlier step in the same
    playbook. A `bandit` policy is not yet a member of this schema -- there
    is nothing in this phase that would interpret one, and a playbook field
    no code reads is worse than no field at all.
    """

    model_config = ConfigDict(extra="forbid")

    policy: Literal["fixed", "relative"]
    offset_hours: float = Field(ge=0)
    after_step: str | None = None

    @model_validator(mode="after")
    def _after_step_matches_policy(self) -> TimingSpec:
        if self.policy == "relative" and self.after_step is None:
            raise ValueError("timing.policy 'relative' requires 'after_step'")
        if self.policy == "fixed" and self.after_step is not None:
            raise ValueError("timing.policy 'fixed' does not take 'after_step'")
        return self


class SkipIf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at_risk_below_paise: int | None = Field(default=None, gt=0)


class StepGuard(BaseModel):
    """Read by the policy engine (T2.5), not the planner -- ARCHITECTURE's
    "domain guards" policy rule, not a planning-time filter."""

    model_config = ConfigDict(extra="forbid")

    decline_retryable: bool | None = None


class PlaybookStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    channel: Channel
    timing: TimingSpec
    # Not in DOMAIN-MODEL SS6.1's illustrative YAML, which shows no per-step
    # cost field even though Plan.total_expected_cost and TR-18's
    # cost-ceiling fit both need one. Declaring it directly on the step is
    # the simplest source of truth available this phase, without a
    # channel-cost lookup table this phase has no other use for. This is
    # the planner's *estimate* for fitting a plan under the ceiling before
    # execution -- the *actual* per-send cost (T2.7's channel registry,
    # priced against a real gateway/provider) is what lands in
    # case.cost_spent.
    expected_cost_paise: int = Field(ge=0)
    required: bool = False
    consumes_mandate_budget: bool = False
    skip_if: SkipIf | None = None
    guard: StepGuard | None = None


class Playbook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = Field(gt=0)
    applies_to: AppliesTo
    cost_ceiling_pct: float = Field(gt=0, le=10)
    max_attempts: int = Field(gt=0)
    max_case_age_days: int = Field(gt=0)
    steps: tuple[PlaybookStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _steps_are_well_formed(self) -> Playbook:
        seen_ids: set[str] = set()
        for step in self.steps:
            if step.id in seen_ids:
                raise ValueError(f"duplicate step id {step.id!r} in playbook {self.id!r}")
            if step.timing.policy == "relative" and step.timing.after_step not in seen_ids:
                raise ValueError(
                    f"step {step.id!r} timing.after_step={step.timing.after_step!r} "
                    "must name an earlier step in the same playbook"
                )
            seen_ids.add(step.id)
        return self
