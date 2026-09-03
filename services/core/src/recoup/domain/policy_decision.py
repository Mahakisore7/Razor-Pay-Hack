"""PolicyDecision -- the record that makes a denial auditable, not merely logged
(DOMAIN-MODEL SS8).

Storing `inputs` verbatim is the point: an operator can ask "why was this
blocked at 21:04 on the 3rd" and get the consent state, contact count, and
clock reading the engine actually saw, not a reconstruction.

Named `policy_decision`, not `policy`, to avoid colliding with the
top-level `recoup.policy` package -- this is the domain value object the
engine in that package produces, not the engine itself.
"""

from __future__ import annotations

from collections.abc import (
    Mapping,  # policy inputs are heterogeneous by nature; see the field comment
)
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from recoup.domain.identifiers import ActionId

__all__ = ["PolicyDecision", "Verdict"]


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action_id: ActionId
    attempt: int
    verdict: Verdict
    rule_id: str | None  # which rule decided
    inputs: Mapping[str, Any]  # exact inputs, for replay; Any because inputs vary per rule
    defer_until: datetime | None
    decided_at: datetime
