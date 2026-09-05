"""The three-arm benchmark runner (T3.5; PHASE-03-measurement.md):
`recoup bench run --seed N --size M`. All three arms, one cohort, one
run -- the first thing in this codebase to wire the cohort generator
(T3.1), arm assignment (T3.2), the baseline and control arms (T3.3/
T3.4), and Phase 2's own closed loop (detection -> planning -> policy
gate -> execution -> attribution) into a single process.

Simulated time, not wall-clock: one shared `FrozenClock`, advanced to
each event's own timestamp as the run's own priority queue pops it, so a
21-day case horizon runs in however long the DB round trips actually
take, not 21 real days. Every event -- a cohort case becoming detected,
a scheduled action becoming due -- carries its own timestamp; a `heapq`
processes them in that order regardless of which case or arm they
belong to, which is what "simulated time advancement" (T3.5's checklist)
actually means here.

A single `RazorpaySimulator` instance is shared across the whole run
(one seed, one `World`), so cases interact with the identical simulated
world a live worker pool would share. `seed_failed_payment`, not
`seed_payment`: a cohort case is an *at-risk* case by construction
(T3.1), and letting `World.attempt_outcome` re-roll the initial attempt
could just as easily land on `success`, silently evaporating most of a
requested cohort. Every *retry* against that seed still rolls through
`World.attempt_outcome` normally -- only the initial failure is forced.

Known, deliberate limitation of this cut (documented, not hidden, per
this project's own practice -- see the payment_id gap this same PR
fixes for precedent): recovery this round comes only from
`payment_retry` outcomes. `link`/`email` steps still execute (sent,
cost-accounted, audited) but do not themselves capture a payment --
`World.click_through`/`converts_given_click` exist for exactly this and
are not yet wired to the runner. This under-measures both the baseline
arm (1 of 4 steps drives recovery) and the treatment arm (1 of 2), and a
report built from this run's data must say so plainly rather than
imply full realism. Wiring click-through/conversion modeling in is
follow-up work, not a defect in this run's own correctness.

Also known: nothing in this phase cancels a case's remaining scheduled
steps once it resolves early (that is POLICY-ENGINE R2's stopping
rules, PHASE-04 T4.2) -- a case recovered by its first retry still has
its later steps claimed, gated, and executed for real. Wasteful, not
incorrect: `attribute_payment` simply finds no eligible case for a
payment against an already-terminal one, exactly as TR-8/idempotency
already needs it to for a replayed payment.

Consent (R4, `policy.rules.consent`): this runner is the only caller in
the whole codebase that ever builds a `PolicyContext` (no live worker
exists yet to do it for real traffic), so it is also the only place
that can seed the consent ledger a benchmark customer needs. A cohort
customer is given blanket `checkout`-sourced consent on every channel
the moment their case is opened -- the common real-world default for a
merchant's checkout flow, and the same spirit as `seed_failed_payment`:
a synthetic ground truth explicit enough to make the arms' own
messaging steps actually executable and measurable, rather than
silently denied by R4 for a ledger that was never populated (which is
what a hardcoded empty `consent_events` produced before this fix, on
every run to date -- every non-exempt channel action was denied
`no_consent`, so email/SMS/etc. never reached `done` and T3.6's cost
and guardrail statistics read as zero, not because nothing happened but
because R4 was gating against a ledger this runner never wrote to).
"""

from __future__ import annotations

import heapq
import itertools
import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from recoup.attribution.engine import attribute_payment
from recoup.bench.baseline import load_baseline_playbook
from recoup.bench.cohort import (
    Cohort,
    CohortCase,
    CohortConfig,
    generate_cohort,
    load_default_cohort_config,
)
from recoup.bench.holdout import persist_holdout
from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.diagnosis.engine import stub_diagnose
from recoup.domain.action import Action, Channel
from recoup.domain.case import Arm, Case
from recoup.domain.consent import ConsentSource
from recoup.domain.identifiers import CustomerRef, SignalId, uuid7
from recoup.domain.policy_decision import Verdict
from recoup.domain.signal import Signal, SignalContext
from recoup.execution.executor import ExecutionStatus, execute
from recoup.execution.outbox import claim_due_batch
from recoup.gateway.simulator.simulator import RazorpaySimulator
from recoup.planning.planner import build_plan, select_playbook
from recoup.planning.playbooks.loader import load_playbooks
from recoup.planning.playbooks.schema import Playbook
from recoup.planning.repository import persist_plan
from recoup.platform.clock import FrozenClock
from recoup.platform.models import BenchRun
from recoup.policy.context import DndStatus, KillSwitchState, PolicyContext
from recoup.policy.engine import evaluate
from recoup.policy.repository import load_consent_events, persist_decision, record_consent

__all__ = ["BenchmarkRunSummary", "run_benchmark"]

_WORKER_ID = "bench-runner"
_CLAIM_BATCH_SIZE = 200
# R6 (quiet hours): no per-customer timezone data exists yet -- see
# `_gate_and_execute`'s own comment for why IST is a safe, provably-inert
# default against every playbook step shipped so far.
_DEFAULT_CUSTOMER_TIMEZONE = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class BenchmarkRunSummary:
    run_id: uuid.UUID
    seed: int
    size: int
    started_at: datetime
    finished_at: datetime
    cases_opened: int
    cases_by_arm: dict[str, int]


@dataclass(frozen=True, slots=True)
class _ActionRecord:
    action: Action
    case: Case
    playbook_id: str


def _signal_from_cohort_case(
    cohort_case: CohortCase, customer: CustomerRef, payment_id: str
) -> Signal:
    return Signal(
        id=SignalId(uuid7()),
        leak_class=cohort_case.ground_truth.leak_class,
        customer=customer,
        at_risk=cohort_case.amount,
        detected_at=cohort_case.detected_at,
        source_event_ids=(f"bench-cohort:{cohort_case.index}",),
        decline=cohort_case.ground_truth.decline_category,
        context=SignalContext(issuer=cohort_case.issuer, method=cohort_case.instrument),
        source_payment_id=payment_id,
    )


async def run_benchmark(
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    *,
    seed: int,
    size: int,
    start_at: datetime,
    cohort_config: CohortConfig | None = None,
) -> BenchmarkRunSummary:
    """Runs `size` cohort cases through all three arms, in simulated
    time, and returns a summary. The `bench_runs` row this writes is the
    permanent, reproducible record (DATA-MODEL SS7) -- `seed` is what a
    reader needs to reproduce it (A3.2, T3.8's own phase gate, is a
    property of the cohort/world/policy pipeline this wires together,
    not of this function's control flow alone).
    """
    config = cohort_config if cohort_config is not None else load_default_cohort_config()
    cohort: Cohort = generate_cohort(config, seed=seed, size=size, start_at=start_at)

    sim = RazorpaySimulator(seed=seed)
    clock = FrozenClock(start_at)
    playbooks = load_playbooks()
    baseline_playbook = load_baseline_playbook()

    run_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(BenchRun(id=run_id, seed=seed, config={"size": size}, started_at=start_at))
        await session.commit()

    seq = itertools.count()
    heap: list[tuple[datetime, int, str, object]] = []
    for cohort_case in cohort.cases:
        heapq.heappush(heap, (cohort_case.detected_at, next(seq), "case", cohort_case))

    actions_by_scheduled_id: dict[uuid.UUID, _ActionRecord] = {}
    claimed_ids: set[uuid.UUID] = set()
    cases_by_arm: dict[str, int] = {arm.value: 0 for arm in Arm}
    cases_opened = 0

    while heap:
        when, _, kind, item = heapq.heappop(heap)
        clock.set(when)

        if kind == "case":
            case = await _handle_case_arrival(
                sessionmaker,
                sim,
                clock,
                seed=seed,
                run_id=run_id,
                cohort_case=item,  # type: ignore[arg-type]
                playbooks=playbooks,
                baseline_playbook=baseline_playbook,
                heap=heap,
                seq=seq,
                actions_by_scheduled_id=actions_by_scheduled_id,
            )
            if case is not None:
                cases_opened += 1
                cases_by_arm[case.arm.value] += 1
            continue

        scheduled_action_id: uuid.UUID = item  # type: ignore[assignment]
        if scheduled_action_id in claimed_ids:
            # Defensive, not load-bearing: two different cases' actions
            # landing on the exact same due_at is possible in principle
            # (continuous hashed floats make it vanishingly unlikely in
            # practice, which is also why this line is untested rather
            # than contrived) -- claim_due_batch below already claims
            # everything due at once, so a second pop at the identical
            # instant would just find nothing left to claim if this
            # short-circuit were removed.
            continue  # pragma: no cover

        async with sessionmaker() as session:
            claimed = await claim_due_batch(
                session, clock, worker_id=_WORKER_ID, batch_size=_CLAIM_BATCH_SIZE
            )
        for row in claimed:
            claimed_ids.add(row.id)
        for row in claimed:
            record = actions_by_scheduled_id.get(row.id)
            if record is None:
                continue  # not one of this run's own actions -- ignore
            async with sessionmaker() as session:
                await _gate_and_execute(session, redis, sim, clock, row_id=row.id, record=record)

    finished_at = clock.now()
    async with sessionmaker() as session:
        run_row = await session.get(BenchRun, run_id)
        assert run_row is not None
        run_row.completed_at = finished_at
        await session.commit()

    return BenchmarkRunSummary(
        run_id=run_id,
        seed=seed,
        size=size,
        started_at=start_at,
        finished_at=finished_at,
        cases_opened=cases_opened,
        cases_by_arm=cases_by_arm,
    )


async def _seed_consent_if_new(
    session: AsyncSession, *, customer: CustomerRef, at: datetime
) -> None:
    """Blanket checkout consent, once per customer -- see this module's
    own docstring for why the benchmark runner is the one seeding it.
    Guarded by an existence check rather than relying on `resolve_customer`
    reporting find-vs-create, since a cohort can (rarely, TR-8-style)
    reference the same synthetic customer from more than one case.
    """
    existing = await load_consent_events(session, customer)
    if existing:
        return
    for channel in Channel:
        await record_consent(
            session,
            customer=customer,
            channel=channel,
            granted=True,
            source=ConsentSource.CHECKOUT,
            occurred_at=at,
        )


async def _handle_case_arrival(
    sessionmaker: async_sessionmaker[AsyncSession],
    sim: RazorpaySimulator,
    clock: FrozenClock,
    *,
    seed: int,
    run_id: uuid.UUID,
    cohort_case: CohortCase,
    playbooks: dict[str, Playbook],
    baseline_playbook: Playbook,
    heap: list[tuple[datetime, int, str, object]],
    seq: itertools.count[int],
    actions_by_scheduled_id: dict[uuid.UUID, _ActionRecord],
) -> Case | None:
    payment = sim.seed_failed_payment(
        customer_id=cohort_case.customer_id,
        amount=cohort_case.amount,
        method=cohort_case.instrument,
        issuer=cohort_case.issuer,
        at=clock.now(),
        decline_category=cohort_case.ground_truth.decline_category,
    )

    async with sessionmaker() as session:
        customer = await resolve_customer(session, cohort_case.razorpay_customer_id)
        await _seed_consent_if_new(session, customer=customer, at=cohort_case.detected_at)
        signal = _signal_from_cohort_case(cohort_case, customer, payment.id)
        case = await open_case_for_signal(session, clock, seed, signal, bench_run_id=run_id)
    if case is None:
        # TR-8 dedup no-op -- another cohort case landed on the identical (customer, amount).
        return None

    if case.arm == Arm.CONTROL:
        diagnosis = stub_diagnose(case.id, cohort_case.ground_truth.decline_category, clock)
        async with sessionmaker() as session:
            await persist_holdout(session, clock, case=case, diagnosis=diagnosis)
            await session.commit()
        return case

    if case.arm == Arm.BASELINE:
        playbook = baseline_playbook
    else:
        diagnosis = stub_diagnose(case.id, cohort_case.ground_truth.decline_category, clock)
        matched = select_playbook(playbooks, diagnosis, cohort_case.ground_truth.leak_class)
        if matched is None:
            return case  # no matching playbook -- the same "abstain" shape T2.4 already has
        playbook = matched

    result = build_plan(case, playbook, clock)
    async with sessionmaker() as session:
        realised = await persist_plan(session, clock, case=case, playbook=playbook, result=result)
        await session.commit()

    for action, scheduled_id in realised:
        actions_by_scheduled_id[scheduled_id] = _ActionRecord(
            action=action, case=case, playbook_id=playbook.id
        )
        heapq.heappush(heap, (action.due_at, next(seq), "action", scheduled_id))
    return case


async def _gate_and_execute(
    session: AsyncSession,
    redis: Redis,
    sim: RazorpaySimulator,
    clock: FrozenClock,
    *,
    row_id: uuid.UUID,
    record: _ActionRecord,
) -> None:
    consent_events = await load_consent_events(session, record.case.customer)
    ctx = PolicyContext(
        now=clock.now(),
        case=record.case,
        playbook_id=record.playbook_id,
        consent_events=consent_events,
        # No DND registry sync exists yet (out of T4.1's scope, same as
        # the still-permissive kill switch and mandate below) -- every
        # cohort customer is treated as not registered, so R5 stays a
        # no-op here until a real sync source lands. Every playbook step
        # shipped so far is `transactional` anyway (see the playbook
        # YAMLs' own `category` comments), so R5 would not fire either way.
        dnd_status=DndStatus(registered=False),
        # R6 (quiet hours): no per-customer geo/phone-based timezone
        # inference exists yet, so every cohort customer defaults to IST,
        # Razorpay's primary market. Every playbook step shipped so far is
        # `payment_retry`, `link`, or `email` -- all three exempt from R6
        # (quiet_hours.py's own docstring) -- so this default is
        # provably a no-op today, the same shape as `dnd_status` above.
        customer_timezone=_DEFAULT_CUSTOMER_TIMEZONE,
        # R7 (frequency cap): `execution.executor.execute` now writes real
        # contact history (this PR's own T4.1 addition), but reading it
        # back here would let quiet-hours-shaped DEFER verdicts reach a
        # runner with no re-queue mechanism for one yet -- a deferred
        # action would simply vanish (see the `pragma: no cover` comment
        # below), not be rescheduled. `()` keeps this rule provably
        # permissive until that re-queue exists; wiring a live read is
        # follow-up work, not this PR's own scope.
        contact_history=(),
        mandate=None,
        kill_switch=KillSwitchState(global_tripped=False, tripped_playbooks=frozenset()),
        # R11 (rate limits): no live token bucket exists yet (this rule's
        # own docstring) -- an empty mapping treats every channel as
        # unconstrained, matching kill_switch/dnd_status's own "no real
        # backing source yet" default.
        rate_limit_tokens={},
    )
    decision = evaluate(record.action, ctx)
    await persist_decision(session, clock, case_id=record.case.id, decision=decision)
    await session.commit()
    if decision.verdict is not Verdict.ALLOW:
        # Genuinely unreachable with this runner's current inputs, not
        # merely untested: kill_switch and the mandate check are always
        # permissive here (`KillSwitchState(global_tripped=False, ...)`,
        # `mandate=None`), consent is now seeded for every channel
        # (T3.6's own fix), dnd_status is always unregistered above (and
        # every shipped playbook step is `transactional` regardless),
        # quiet_hours is exempt for every shipped step's channel
        # regardless of the IST default above, frequency_cap can never
        # breach against an empty `contact_history`, rate_limit can never
        # breach against an empty `rate_limit_tokens`, domain_guards'
        # non-retryable check is already filtered out at planning time by
        # the playbook's own `decline_retryable` guard, and cost_ceiling
        # is now kept in sync with the plan that funded it. Kept, not
        # deleted -- a future kill-switch/mandate/DEFER-requeue wiring
        # (PHASE-04) makes this a real path again, and that requeue is
        # that same future work's own scope.
        return  # pragma: no cover

    result = await execute(
        session,
        redis,
        sim,
        clock,
        action=record.action,
        case=record.case,
        scheduled_action_id=row_id,
        dry_run=False,
    )
    if (
        result.status is ExecutionStatus.EXECUTED
        and result.channel_success
        and record.action.channel == Channel.PAYMENT_RETRY
        and result.reference is not None
    ):
        payment = await sim.fetch_payment(result.reference)
        await attribute_payment(session, clock, payment=payment)
