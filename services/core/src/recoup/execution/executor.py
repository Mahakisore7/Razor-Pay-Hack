"""The executor (PHASE-02 T2.7): the only place a claimed scheduled action
becomes a real side effect. This is A2.3, the phase gate this entire
product's claims rest on -- "An action with no ALLOW raises rather than
executing."

TR-22: asserts a persisted `PolicyDecision(action_id, attempt)` with
`verdict == ALLOW` exists before touching a channel, and raises
otherwise. That decision is looked up, not computed here -- `policy.
evaluate()` runs upstream (layering also forbids `policy` from importing
`execution`, so the reverse could not work even if it were desirable) and
its verdict is recorded before an action ever reaches this function.

TR-23: idempotency is enforced with a Redis `SET NX` on a key derived
from `Action.idempotency_key` (itself `sha256(case_id | step_id |
attempt)`), checked *before* the channel is ever called. That ordering
is deliberate: it guarantees the channel -- and therefore the gateway --
is never called twice for the same `(case_id, step_id, attempt)`, which
is the guarantee TR-56/57 actually ask for. Its cost is a known, accepted
gap: a crash between a successful `SET` and this function's transaction
commit can leave that one action's cost unrecorded on the reclaim that
follows, since the reclaimed attempt sees the key already set and
correctly refuses to re-run the channel. Recording cost is bookkeeping;
never double-charging or double-sending is the safety property this
module exists for, and the two are not both achievable across an
arbitrary crash point without a heavier protocol than TR-23 asks for.

I4 (T2.9): a suppressed duplicate writes no `action_executed` -- the
channel did not run again, so there is no new execution to record; the
original run already accounted for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from recoup.audit.events import Actor, AuditKind
from recoup.audit.repository import record_event
from recoup.domain.action import NON_CONTACT_CHANNELS, Action
from recoup.domain.case import Case
from recoup.domain.errors import RecoupError
from recoup.domain.policy_decision import Verdict
from recoup.execution.channels.base import ChannelResult
from recoup.execution.channels.registry import get_channel_handler
from recoup.execution.outbox import mark_done, mark_failed
from recoup.gateway.interface import PaymentGateway
from recoup.platform.clock import Clock
from recoup.platform.logging import current_trace_id
from recoup.platform.models import CaseRow, PolicyDecisionRow
from recoup.policy.repository import record_contact

__all__ = [
    "ExecutionResult",
    "ExecutionStatus",
    "NoAllowDecisionError",
    "execute",
    "idempotency_key",
]

_IDEMPOTENCY_TTL = timedelta(hours=24)


class NoAllowDecisionError(RecoupError):
    """TR-22, A2.3 -- the phase gate. Raised, never silently skipped: an
    action reaching the executor with no recorded ALLOW for its
    `(action_id, attempt)` is a bug upstream, not a normal outcome to
    recover from.
    """

    def __init__(self, action_id: UUID, attempt: int) -> None:
        self.action_id = action_id
        self.attempt = attempt
        super().__init__(
            f"no ALLOW policy decision recorded for action {action_id} attempt {attempt}"
        )


class ExecutionStatus(StrEnum):
    EXECUTED = "executed"
    SUPPRESSED = "suppressed"  # idempotency key already existed -- a duplicate, not re-run
    FAILED = "failed"  # the channel raised; the scheduled action is marked failed


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: ExecutionStatus
    channel_success: bool | None  # None when suppressed or failed before the channel ran
    # The channel's own `ChannelResult.reference` -- e.g. the new payment id
    # a successful payment_retry produced (T3.5: a benchmark runner needs
    # this to fetch and attribute that payment; nothing before T3.5 needed
    # to know it). `None` for every status but EXECUTED.
    reference: str | None = None


def idempotency_key(action: Action) -> str:
    """A namespaced Redis key over `Action.idempotency_key` -- the
    `recoup:idempotency:` prefix is this module's concern, not the
    domain object's.
    """
    return f"recoup:idempotency:{action.idempotency_key}"


async def _assert_allowed(session: AsyncSession, action_id: UUID, attempt: int) -> None:
    result = await session.execute(
        select(PolicyDecisionRow.verdict).where(
            PolicyDecisionRow.action_id == action_id, PolicyDecisionRow.attempt == attempt
        )
    )
    verdict = result.scalar_one_or_none()
    if verdict != Verdict.ALLOW.value:
        raise NoAllowDecisionError(action_id, attempt)


async def execute(
    session: AsyncSession,
    redis: Redis,
    gateway: PaymentGateway,
    clock: Clock,
    *,
    action: Action,
    case: Case,
    scheduled_action_id: UUID,
    dry_run: bool = False,
) -> ExecutionResult:
    """TR-26: every stage up to the channel call runs identically in
    dry-run -- the ALLOW assertion, the idempotency check, and (per
    TR-27) the cost postgres write -- only the channel invocation itself,
    the actual side effect, is substituted.
    """
    await _assert_allowed(session, action.id, action.attempt)

    key = idempotency_key(action)
    acquired = await redis.set(key, "1", nx=True, ex=int(_IDEMPOTENCY_TTL.total_seconds()))
    if not acquired:
        await mark_done(session, clock, scheduled_action_id)
        return ExecutionResult(status=ExecutionStatus.SUPPRESSED, channel_success=None)

    if dry_run:
        result = ChannelResult(success=True, reference=None)
    else:
        handler = get_channel_handler(action.channel)
        try:
            result = await handler(gateway, action, case, clock)
        except Exception as exc:
            # I4: the audit event and `mark_failed`'s status write share
            # this session, and `mark_failed` is what actually commits --
            # so the two land together or, on a crash between them, not
            # at all.
            await record_event(
                session,
                case_id=case.id,
                kind=AuditKind.ACTION_FAILED,
                payload={
                    "action_id": str(action.id),
                    "channel": action.channel.value,
                    "step_id": action.step_id,
                    "attempt": action.attempt,
                    "error": str(exc),
                },
                actor=Actor.scheduler(),
                trace_id=current_trace_id(),
                occurred_at=clock.now(),
            )
            await mark_failed(session, scheduled_action_id, error=str(exc))
            return ExecutionResult(status=ExecutionStatus.FAILED, channel_success=None)

    # TR-27: cost and the outbox's terminal status commit together -- both
    # statements on this session, one commit, no partial state where the
    # cost landed but the row is still `claimed` (or vice versa). The
    # audit event (I4) joins the same commit for the same reason.
    await record_event(
        session,
        case_id=case.id,
        kind=AuditKind.ACTION_EXECUTED,
        payload={
            "action_id": str(action.id),
            "channel": action.channel.value,
            "step_id": action.step_id,
            "attempt": action.attempt,
            "cost_paise": action.cost.paise,
            "channel_success": result.success,
            "dry_run": dry_run,
        },
        actor=Actor.scheduler(),
        trace_id=current_trace_id(),
        occurred_at=clock.now(),
    )
    if action.channel not in NON_CONTACT_CHANNELS:
        # R7 (POLICY-ENGINE SS3): counted regardless of `result.success` --
        # an attempted send already reached the customer's phone/inbox,
        # whatever the channel later reports about delivery. Not written
        # for a suppressed duplicate (TR-23's own idempotency guard, above)
        # since the channel never ran a second time for it.
        await record_contact(
            session, customer=case.customer, channel=action.channel, occurred_at=clock.now()
        )
    await session.execute(
        update(CaseRow)
        .where(CaseRow.id == case.id)
        .values(cost_spent_paise=CaseRow.cost_spent_paise + action.cost.paise)
    )
    # Mirrors the row update onto the caller's own `Case` object: a
    # long-lived caller holding this `case` across several actions (the
    # benchmark runner's `_ActionRecord`, most notably) needs its
    # `cost_spent` to reflect reality, or R8/cost_ceiling's next gate
    # check re-evaluates against a permanently-stale zero.
    case.record_cost(action.cost)
    await mark_done(session, clock, scheduled_action_id)
    await session.commit()
    return ExecutionResult(
        status=ExecutionStatus.EXECUTED, channel_success=result.success, reference=result.reference
    )
