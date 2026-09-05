"""Persists a `PlanningResult` (T2.10): the one caller `planner.py`
itself never had, since `build_plan` is pure (no session, ARCHITECTURE's
`planning` layer). Detection (T2.2) and execution (T2.6/T2.7) already
had a real, session-taking write path from their own phase; planning
did not, because nothing wired planning to execution until now.

Advances `case` `DETECTED -> DIAGNOSING -> PLANNED -> EXECUTING`, unless
R10's approval threshold (POLICY-ENGINE SS3, T4.1) says otherwise -- a
case whose `at_risk` exceeds it goes to `AWAITING_APPROVAL` instead,
`domain.case`'s own transition table only ever admitting that state from
`PLANNED`, never from `EXECUTING`, is what makes this the one and only
place that decision can be made. `policy.rules.approval_threshold.
requires_approval` is `policy`'s own reusable predicate, not a rule
re-implemented here -- `planning` sits above `policy` in the layering
contract, so importing it is allowed the same way domain_guards.py
already reads `Mandate.authorize_debit` instead of duplicating it.

Diagnosis itself writes nothing durable this phase (T2.3's own scope:
`stub_diagnose` is pure, no `DiagnosisRow`), so there is no separately
observable moment to split `DIAGNOSING` out into its own audit event --
this collapses the three hops into the one moment a plan actually lands,
audited as a single `plan_created` (plus `approval_requested` when it
routes to `AWAITING_APPROVAL`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from recoup.audit.events import Actor, AuditKind
from recoup.audit.repository import record_event
from recoup.domain.action import Action, ActionPayload, Channel
from recoup.domain.case import Case, CaseState
from recoup.domain.identifiers import ActionId
from recoup.domain.money import Money
from recoup.planning.planner import PlanningResult
from recoup.planning.playbooks.schema import Playbook
from recoup.platform.clock import Clock
from recoup.platform.logging import current_trace_id
from recoup.platform.models import ActionRow, CaseRow, PlannedStepRow, PlanRow, ScheduledActionRow
from recoup.policy.rules.approval_threshold import APPROVAL_THRESHOLD, requires_approval

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
    advances the case to `EXECUTING` -- or to `AWAITING_APPROVAL` instead,
    if R10 (POLICY-ENGINE SS3) says this case's `at_risk` needs a human
    first. Returns the `(Action, scheduled_action_id)` pairs in `due_at`
    order, so a caller can hand the first one straight to the
    scheduler/executor; that caller's own policy gate is what actually
    keeps those actions from executing while the case sits in
    `AWAITING_APPROVAL` (`policy.rules.approval_threshold.evaluate`), so
    every step is still planned, scheduled, and claimable exactly as
    normal here.

    `channel`, `category`, and `consumes_mandate_budget` come from the
    *playbook*'s own step declaration -- `PlannedStep` (the pure planning
    result) carries only `step_id`, `due_at`, and `expected_cost`, not any
    of the three, so the playbook is still needed here to resolve them.
    """
    case.transition_to(CaseState.DIAGNOSING)
    case.transition_to(CaseState.PLANNED)
    needs_approval = requires_approval(case.at_risk)
    case.transition_to(CaseState.AWAITING_APPROVAL if needs_approval else CaseState.EXECUTING)

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
        # T3.5: a payment_retry step needs the id of the payment it is
        # re-presenting -- the only fact `Case` carries for that. Every
        # other channel's payload stays empty; nothing else this phase
        # reads a variable from it.
        payload = (
            ActionPayload(variables={"payment_id": case.source_payment_id})
            if playbook_step.channel == Channel.PAYMENT_RETRY and case.source_payment_id is not None
            else ActionPayload()
        )
        action = Action(
            id=ActionId(action_id),
            case_id=case.id,
            step_id=planned.step_id,
            attempt=1,
            channel=playbook_step.channel,
            category=playbook_step.category,
            payload=payload,
            cost=planned.expected_cost,
            due_at=planned.due_at,
            consumes_mandate_budget=playbook_step.consumes_mandate_budget,
        )
        session.add(
            ActionRow(
                id=action_id,
                case_id=case.id,
                step_id=planned.step_id,
                attempt=1,
                channel=playbook_step.channel.value,
                idempotency_key=action.idempotency_key,
                payload={"template": payload.template, "variables": dict(payload.variables)},
                cost_paise=planned.expected_cost.paise,
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
    # Mirrored onto the domain object too, not just the row: a caller
    # holding this same `case` past this call (the benchmark runner's
    # `_ActionRecord`, again) needs R8/cost_ceiling's next gate check to
    # see the budget a plan actually funded, not the zero `Case` starts
    # detection with (detection/pipeline.py's own docstring on why it's
    # zero there).
    case.cost_ceiling = Money(case_row.cost_ceiling_paise, case.cost_ceiling.currency)

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
    if needs_approval:
        await record_event(
            session,
            case_id=case.id,
            kind=AuditKind.APPROVAL_REQUESTED,
            payload={
                "at_risk_paise": case.at_risk.paise,
                "threshold_paise": APPROVAL_THRESHOLD.paise,
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
