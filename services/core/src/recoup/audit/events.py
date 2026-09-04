"""AuditEvent -- the system of record (DOMAIN-MODEL SS10).

Append-only. Application-level immutability is a promise; a Postgres
trigger rejecting UPDATE/DELETE (T1.8) is the guarantee. Hash-chained, so
tampering with a past event is detectable rather than merely disallowed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any  # payload/actor content is heterogeneous per event kind

from recoup.domain.identifiers import AuditEventId, CaseId

__all__ = [
    "Actor",
    "ActorKind",
    "AuditEvent",
    "AuditKind",
    "append_event",
    "canonical_json",
    "compute_hash",
]


class ActorKind(StrEnum):
    SYSTEM = "system"
    USER = "user"
    SCHEDULER = "scheduler"


@dataclass(frozen=True, slots=True, init=False)
class Actor:
    """SYSTEM | USER(id) | SCHEDULER (DOMAIN-MODEL SS10) as one dataclass
    rather than three, so canonical JSON serialisation doesn't need a
    per-variant case. `user_id` is set if and only if `kind` is USER."""

    kind: ActorKind
    user_id: str | None

    def __init__(self, kind: ActorKind, user_id: str | None = None) -> None:
        if kind == ActorKind.USER and user_id is None:
            raise ValueError("Actor.user_id is required when kind is USER")
        if kind != ActorKind.USER and user_id is not None:
            raise ValueError(f"Actor.user_id must be None when kind is {kind}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "user_id", user_id)

    @classmethod
    def system(cls) -> Actor:
        return cls(ActorKind.SYSTEM)

    @classmethod
    def scheduler(cls) -> Actor:
        return cls(ActorKind.SCHEDULER)

    @classmethod
    def user(cls, user_id: str) -> Actor:
        return cls(ActorKind.USER, user_id)


class AuditKind(StrEnum):
    SIGNAL_DETECTED = "signal_detected"
    CASE_OPENED = "case_opened"
    ARM_ASSIGNED = "arm_assigned"
    DIAGNOSIS_STARTED = "diagnosis_started"
    DIAGNOSIS_COMPLETED = "diagnosis_completed"
    DIAGNOSIS_ABSTAINED = "diagnosis_abstained"
    LLM_CALLED = "llm_called"
    LLM_FALLBACK = "llm_fallback"
    PLAN_CREATED = "plan_created"
    PLAN_STEP_DROPPED = "plan_step_dropped"
    CASE_HELD_OUT = "case_held_out"
    POLICY_EVALUATED = "policy_evaluated"
    POLICY_DENIED = "policy_denied"
    POLICY_DEFERRED = "policy_deferred"
    ACTION_SCHEDULED = "action_scheduled"
    ACTION_CLAIMED = "action_claimed"
    ACTION_EXECUTED = "action_executed"
    ACTION_FAILED = "action_failed"
    MESSAGE_VALIDATED = "message_validated"
    MESSAGE_REJECTED = "message_rejected"
    CONSENT_CHANGED = "consent_changed"
    STOPPING_RULE_FIRED = "stopping_rule_fired"
    KILL_SWITCH_TRIPPED = "kill_switch_tripped"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    PAYMENT_ATTRIBUTED = "payment_attributed"
    ATTRIBUTION_AMBIGUOUS = "attribution_ambiguous"
    CASE_RESOLVED = "case_resolved"
    CASE_EXPIRED = "case_expired"


@dataclass(frozen=True, slots=True, init=False)
class AuditEvent:
    id: AuditEventId
    case_id: CaseId
    seq: int  # per-case, gapless, starts at 1
    kind: AuditKind
    payload: Mapping[str, Any]  # PII masked before it ever reaches here
    actor: Actor
    trace_id: str  # OpenTelemetry correlation
    occurred_at: datetime
    prev_hash: str  # hash of event seq-1, "" for seq 1
    hash: str  # sha256(canonical_json(this) minus hash) -- derived, never supplied

    def __init__(
        self,
        *,
        id: AuditEventId,  # noqa: A002 -- matches DOMAIN-MODEL's id field name on every entity
        case_id: CaseId,
        seq: int,
        kind: AuditKind,
        payload: Mapping[str, Any],
        actor: Actor,
        trace_id: str,
        occurred_at: datetime,
        prev_hash: str,
    ) -> None:
        if seq < 1:
            raise ValueError(f"AuditEvent.seq must be >= 1, got {seq}")
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "case_id", case_id)
        object.__setattr__(self, "seq", seq)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "prev_hash", prev_hash)
        object.__setattr__(self, "hash", compute_hash(self))


def canonical_json(event: AuditEvent) -> str:
    """Deterministic JSON for every field except `hash` -- sorted keys, no
    whitespace, so the same event always encodes to the same bytes."""
    body = {
        "id": str(event.id),
        "case_id": str(event.case_id),
        "seq": event.seq,
        "kind": event.kind.value,
        "payload": event.payload,
        "actor": {"kind": event.actor.kind.value, "user_id": event.actor.user_id},
        "trace_id": event.trace_id,
        "occurred_at": event.occurred_at.isoformat(),
        "prev_hash": event.prev_hash,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def compute_hash(event: AuditEvent) -> str:
    return hashlib.sha256(canonical_json(event).encode("utf-8")).hexdigest()


def append_event(
    prior: AuditEvent | None,
    *,
    id: AuditEventId,  # noqa: A002 -- matches DOMAIN-MODEL's id field name on every entity
    case_id: CaseId,
    kind: AuditKind,
    payload: Mapping[str, Any],
    actor: Actor,
    trace_id: str,
    occurred_at: datetime,
) -> AuditEvent:
    """Build the next event in a case's chain from the previous one.

    `seq` and `prev_hash` are derived from `prior`, not supplied -- the one
    place a chain can break is a caller getting these two fields out of
    sync with the actual previous event, so this removes the chance to
    make that mistake.
    """
    if prior is not None and prior.case_id != case_id:
        raise ValueError("append_event: prior event belongs to a different case")
    return AuditEvent(
        id=id,
        case_id=case_id,
        seq=(prior.seq + 1) if prior is not None else 1,
        kind=kind,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
        occurred_at=occurred_at,
        prev_hash=prior.hash if prior is not None else "",
    )
