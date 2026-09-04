"""`Rule` (POLICY-ENGINE SS3): a pure function of `(action, ctx)` that
returns the `PolicyDecision` that fires it, or `None` to let evaluation
fall through to the next rule -- the same "return `None` to pass" shape
`detection.detectors.base.Detector` already uses.
"""

from __future__ import annotations

from typing import Protocol

from recoup.domain.action import Action
from recoup.domain.policy_decision import PolicyDecision
from recoup.policy.context import PolicyContext

__all__ = ["Rule"]


class Rule(Protocol):
    def __call__(self, action: Action, ctx: PolicyContext) -> PolicyDecision | None: ...
