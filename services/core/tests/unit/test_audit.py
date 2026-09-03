"""AuditEvent's hash chain (DOMAIN-MODEL SS10): tampering with a payload,
reordering events, and deleting an event must each be detectable by
`verify_chain`, since application-level immutability is only a promise --
the chain is what makes a violation of that promise provable."""

from datetime import UTC, datetime, timedelta

import pytest

from recoup.audit.events import Actor, ActorKind, AuditEvent, AuditKind, append_event, compute_hash
from recoup.audit.verify import verify_chain
from recoup.domain.identifiers import AuditEventId, CaseId, uuid7

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _case_id() -> CaseId:
    return CaseId(uuid7())


def _chain(case_id: CaseId, length: int) -> list[AuditEvent]:
    events: list[AuditEvent] = []
    prior: AuditEvent | None = None
    for i in range(length):
        prior = append_event(
            prior,
            id=AuditEventId(uuid7()),
            case_id=case_id,
            kind=AuditKind.CASE_OPENED,
            payload={"index": i},
            actor=Actor.system(),
            trace_id="trace-1",
            occurred_at=_T0 + timedelta(minutes=i),
        )
        events.append(prior)
    return events


# --- Actor -------------------------------------------------------------------


def test_actor_user_requires_a_user_id() -> None:
    with pytest.raises(ValueError, match="user_id is required"):
        Actor(ActorKind.USER)


def test_actor_system_rejects_a_user_id() -> None:
    with pytest.raises(ValueError, match="must be None"):
        Actor(ActorKind.SYSTEM, "u_123")


def test_actor_user_factory_sets_user_id() -> None:
    actor = Actor.user("u_123")
    assert actor.kind == ActorKind.USER
    assert actor.user_id == "u_123"


def test_actor_scheduler_factory() -> None:
    actor = Actor.scheduler()
    assert actor.kind == ActorKind.SCHEDULER
    assert actor.user_id is None


def test_audit_event_rejects_a_seq_below_one() -> None:
    with pytest.raises(ValueError, match="seq must be"):
        AuditEvent(
            id=AuditEventId(uuid7()),
            case_id=_case_id(),
            seq=0,
            kind=AuditKind.CASE_OPENED,
            payload={},
            actor=Actor.system(),
            trace_id="trace-1",
            occurred_at=_T0,
            prev_hash="",
        )


# --- append_event / hash chain construction -----------------------------------


def test_first_event_has_seq_one_and_empty_prev_hash() -> None:
    first = append_event(
        None,
        id=AuditEventId(uuid7()),
        case_id=_case_id(),
        kind=AuditKind.CASE_OPENED,
        payload={},
        actor=Actor.system(),
        trace_id="trace-1",
        occurred_at=_T0,
    )
    assert first.seq == 1
    assert first.prev_hash == ""
    assert first.hash == compute_hash(first)


def test_second_event_chains_to_the_first() -> None:
    case_id = _case_id()
    events = _chain(case_id, 2)
    assert events[1].seq == 2
    assert events[1].prev_hash == events[0].hash


def test_append_event_rejects_a_prior_from_a_different_case() -> None:
    events = _chain(_case_id(), 1)
    with pytest.raises(ValueError, match="different case"):
        append_event(
            events[0],
            id=AuditEventId(uuid7()),
            case_id=_case_id(),
            kind=AuditKind.CASE_OPENED,
            payload={},
            actor=Actor.system(),
            trace_id="trace-1",
            occurred_at=_T0,
        )


def test_canonical_json_hash_is_deterministic_regardless_of_payload_key_order() -> None:
    event_id = AuditEventId(uuid7())
    case_id = _case_id()
    event_a = AuditEvent(
        id=event_id,
        case_id=case_id,
        seq=1,
        kind=AuditKind.CASE_OPENED,
        payload={"a": 1, "b": 2},
        actor=Actor.system(),
        trace_id="trace-1",
        occurred_at=_T0,
        prev_hash="",
    )
    event_b = AuditEvent(
        id=event_id,
        case_id=case_id,
        seq=1,
        kind=AuditKind.CASE_OPENED,
        payload={"b": 2, "a": 1},
        actor=Actor.system(),
        trace_id="trace-1",
        occurred_at=_T0,
        prev_hash="",
    )
    assert event_a.hash == event_b.hash


# --- verify_chain --------------------------------------------------------------


def test_verify_chain_accepts_an_intact_chain() -> None:
    assert verify_chain(_chain(_case_id(), 5)) is None


def test_verify_chain_accepts_an_empty_chain() -> None:
    assert verify_chain([]) is None


def test_verify_chain_detects_a_tampered_payload() -> None:
    events = _chain(_case_id(), 3)
    # Simulate the stored bytes being rewritten out-of-band, without
    # recomputing the hash -- exactly what the trigger in T1.8 exists to
    # prevent at the database layer; this proves the chain would notice.
    object.__setattr__(events[1], "payload", {"index": "tampered"})
    assert verify_chain(events) == 2


def test_verify_chain_detects_reordering() -> None:
    events = _chain(_case_id(), 3)
    reordered = [events[0], events[2], events[1]]
    assert verify_chain(reordered) == 2


def test_verify_chain_detects_a_deleted_event() -> None:
    events = _chain(_case_id(), 3)
    with_gap = [events[0], events[2]]  # seq 2 removed
    assert verify_chain(with_gap) == 2
