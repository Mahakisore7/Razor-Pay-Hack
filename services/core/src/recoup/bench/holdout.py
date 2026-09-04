"""Persists the control arm's mechanical counterpart to `planning.
repository.persist_plan` (T3.4; PHASE-03-measurement.md, DOMAIN-MODEL
I3/I7): advances a `Case` to `HOLDOUT` and writes exactly zero `PlanRow`,
`ActionRow`, or `ScheduledActionRow` -- not merely zero *done* rows,
zero rows at all, so I3 ("a case in HOLDOUT has zero executed actions")
holds by the simplest possible construction rather than by an
after-the-fact check.

`Case.transition_to` already forbids `arm == control` from ever reaching
`EXECUTING` (I7); this module is what a control-arm case reaches
*instead*, once diagnosis and planning would otherwise have produced a
plan. Diagnosis itself still runs -- `Diagnosis` is passed in, computed
the same way (`diagnosis.engine.stub_diagnose`, or any A2.9-swappable
replacement) as it would be for any other arm -- so "cases detected,
diagnosed, and recorded" (T3.4's own checklist) holds for control too;
only the *execution* half is withheld, and even the diagnosis is never
turned into a plan a caller could execute.

Attribution's `TR-30` handling of `HOLDOUT` (anchoring the match window
to `opened_at` rather than to an executed action) already existed before
this module did -- `attribution.engine._load_candidates` has carried it
since T2.8, on the expectation that something would eventually put a
case into `HOLDOUT` for it to apply to. This is that something.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from recoup.audit.events import Actor, AuditKind
from recoup.audit.repository import record_event
from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.diagnosis import Diagnosis
from recoup.platform.clock import Clock
from recoup.platform.logging import current_trace_id
from recoup.platform.models import CaseRow

__all__ = ["persist_holdout"]


async def persist_holdout(
    session: AsyncSession, clock: Clock, *, case: Case, diagnosis: Diagnosis
) -> None:
    """Advances `case` `DETECTED -> DIAGNOSING -> PLANNED -> HOLDOUT` --
    the only legal path once diagnosis has run and produced no plan to
    execute (the same three-hop collapse `persist_plan` uses, ending one
    stop earlier). Raises `ValueError` for any arm but `control`: nothing
    but the control arm should ever be held out, and a caller passing the
    wrong case here is exactly the kind of mistake this function exists
    to make loud rather than silently corrupt I3's guarantee.
    """
    if case.arm is not Arm.CONTROL:
        raise ValueError(
            f"case {case.id}: persist_holdout is for arm=={Arm.CONTROL.value} cases only, "
            f"got arm=={case.arm.value}"
        )

    case.transition_to(CaseState.DIAGNOSING)
    case.transition_to(CaseState.PLANNED)
    case.transition_to(CaseState.HOLDOUT)

    case_row = await session.get(CaseRow, case.id)
    assert case_row is not None
    case_row.state = case.state.value

    trace_id = current_trace_id()
    now = clock.now()
    await record_event(
        session,
        case_id=case.id,
        kind=AuditKind.DIAGNOSIS_COMPLETED,
        payload={
            "root_cause": diagnosis.root_cause,
            "method": diagnosis.method.value,
        },
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=now,
    )
    await record_event(
        session,
        case_id=case.id,
        kind=AuditKind.CASE_HELD_OUT,
        payload={"reason": "arm_is_control"},
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=now,
    )
