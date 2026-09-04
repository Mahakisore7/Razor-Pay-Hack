"""Persists a `PlanningResult` (T2.10): the one caller `planner.py`
itself never had, since `build_plan` is pure (no session, ARCHITECTURE's
`planning` layer). Detection (T2.2) and execution (T2.6/T2.7) already
had a real, session-taking write path from their own phase; planning
did not, because nothing wired planning to execution until now.

Advances `case` `DETECTED -> DIAGNOSING -> PLANNED -> EXECUTING` -- the
only legal path once a plan exists (`domain.case`'s transition table).
Diagnosis itself writes nothing durable this phase (T2.3's own scope:
`stub_diagnose` is pure, no `DiagnosisRow`), so there is no separately
observable moment to split `DIAGNOSING` out into its own audit event --
this collapses the three hops into the one moment a plan actually lands,
audited as a single `plan_created`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from recoup.audit.events import Actor, AuditKind
from recoup.audit.repository import record_event
from recoup.domain.action import Action, ActionPayload
from recoup.domain.case import Case, CaseState
from recoup.domain.identifiers import ActionId
from recoup.domain.money import Money
from recoup.planning.planner import PlanningResult
from recoup.planning.playbooks.schema import Playbook
from recoup.platform.clock import Clock
from recoup.platform.logging import current_trace_id
from recoup.platform.models import ActionRow, CaseRow, PlannedStepRow, PlanRow, ScheduledActionRow

__all__ = ["persist_plan"]


async def persist_plan(
    session: AsyncSession,
    clock: Clock,
    *,
    case: Case,
    playbook: Playbook,
    result: PlanningResult,
) -> list[tuple[Action, uuid.UUID]]:
    """Writes the `Plan`/`PlannedStep`s, realises each as an attempt-1
    `Action` (with an outbox row due at the step's own `due_at`), and
    advances the case to `EXECUTING`. Returns the `(Action,
    scheduled_action_id)` pairs in `due_at` order, so a caller can hand
    the first one straight to the scheduler/executor.

    `channel` comes from the *playbook*'s own step declaration --
    `PlannedStep` (the pure planning result) carries only `step_id`,
    `due_at`, and `expected_cost`, not a channel, so the playbook is
    still needed here to resolve one.
    """
    case.transition_to(CaseState.DIAGNOSING)
    case.transition_to(CaseState.PLANNED)
    case.transition_to(CaseState.EXECUTING)

    plan_id = uuid.uuid4()
    session.add(
        PlanRow(
            id=plan_id,
            case_id=case.id,
            playbook_id=result.plan.playbook_id,
            playbook_version=result.plan.playbook_version,
            total_expected_cost_paise=result.plan.total_expected_cost.paise,
        )
    )

    steps_by_id = {step.id: step for step in playbook.steps}
    realised: list[tuple[Action, uuid.UUID]] = []
    for planned in result.plan.steps:
        session.add(
            PlannedStepRow(
                id=uuid.uuid4(),
                plan_id=plan_id,
                step_id=planned.step_id,
                due_at=planned.due_at,
                expected_cost_paise=planned.expected_cost.paise,
            )
        )
        playbook_step = steps_by_id[planned.step_id]
        action_id = uuid.uuid4()
        action = Action(
            id=ActionId(action_id),
            case_id=case.id,
            step_id=planned.step_id,
            attempt=1,
            channel=playbook_step.channel,
            payload=ActionPayload(),
            cost=Money(0, case.at_risk.currency),
            due_at=planned.due_at,
        )
        session.add(
            ActionRow(
                id=action_id,
                case_id=case.id,
                step_id=planned.step_id,
                attempt=1,
                channel=playbook_step.channel.value,
                idempotency_key=action.idempotency_key,
                payload={},
                cost_paise=0,
                due_at=planned.due_at,
            )
        )
        scheduled_id = uuid.uuid4()
        session.add(
            ScheduledActionRow(
                id=scheduled_id, action_id=action_id, case_id=case.id, due_at=planned.due_at
            )
        )
        realised.append((action, scheduled_id))

    case_row = await session.get(CaseRow, case.id)
    assert case_row is not None
    case_row.state = case.state.value
    case_row.cost_ceiling_paise = max(
        case_row.cost_ceiling_paise, result.plan.total_expected_cost.paise
    )

    trace_id = current_trace_id()
    occurred_at: datetime = clock.now()
    await record_event(
        session,
        case_id=case.id,
        kind=AuditKind.PLAN_CREATED,
        payload={
            "playbook_id": result.plan.playbook_id,
            "playbook_version": result.plan.playbook_version,
            "step_ids": [step.step_id for step in result.plan.steps],
            "total_expected_cost_paise": result.plan.total_expected_cost.paise,
        },
        actor=Actor.system(),
        trace_id=trace_id,
        occurred_at=occurred_at,
    )
    for dropped in result.dropped_steps:
        await record_event(
            session,
            case_id=case.id,
            kind=AuditKind.PLAN_STEP_DROPPED,
            payload={
                "step_id": dropped.step_id,
                "expected_cost_paise": dropped.expected_cost.paise,
                "reason": dropped.reason,
            },
            actor=Actor.system(),
            trace_id=trace_id,
            occurred_at=occurred_at,
        )

    return sorted(realised, key=lambda pair: pair[0].due_at)
