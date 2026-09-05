"""T2.10 -- the end-to-end test: one case actually flows webhook ->
`RECOVERED` through every stage this phase built, wired together for
the first time (each stage's own PR tested it in isolation; this is the
first place they run in sequence). Also covers the acceptance criteria
that specifically need the whole pipeline assembled to demonstrate:

- A2.1/A2.2: webhook -> RECOVERED in dry-run, with a gapless, verifying
  audit chain covering every stage.
- A2.3: an action a realistically-planned pipeline produced still raises
  rather than executing if nothing ever recorded an ALLOW for it.
- A2.4: a replayed webhook creates no second signal (T2.1's raw_events
  dedup and T2.2's already-detected dedup, exercised together).
- A2.8: opt-out mid-plan cancels the remaining steps and closes the
  case SUPPRESSED with a reason code.
- A2.9: the stub diagnosis engine is swappable -- a second function
  matching the same `DiagnosisEngine` Protocol slots into the identical
  call site with no change.

A2.5 (no double-claim), A2.6 (a killed worker never duplicates a side
effect), and A2.7 (attribution's 100% branch coverage) are proven on
their own dedicated tests (`test_outbox.py`, `test_executor.py`,
`test_attribution_matcher.py`/`test_attribution_properties.py`) and are
not re-derived here.
"""

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.redis import RedisContainer

from recoup.api.app import create_app
from recoup.attribution.engine import attribute_payment
from recoup.audit.events import Actor, ActorKind, AuditEvent, AuditKind
from recoup.audit.verify import verify_chain
from recoup.detection.pipeline import already_detected, run_detection
from recoup.diagnosis.engine import stub_diagnose
from recoup.domain.action import Action
from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.decline import DeclineCategory
from recoup.domain.diagnosis import Diagnosis, DiagnosisMethod, Hypothesis, RootCause
from recoup.domain.identifiers import AuditEventId, CaseId
from recoup.domain.money import Currency, Money
from recoup.domain.policy_decision import Verdict
from recoup.domain.signal import LeakClass
from recoup.execution.executor import ExecutionStatus, NoAllowDecisionError, execute
from recoup.execution.outbox import claim_due_batch
from recoup.execution.suppression import suppress_case
from recoup.gateway.ingestion import RAZORPAY_SOURCE, store_raw_event
from recoup.gateway.interface import Payment as GatewayPayment
from recoup.gateway.interface import PaymentStatus
from recoup.gateway.simulator.simulator import RazorpaySimulator
from recoup.planning.planner import build_plan, select_playbook
from recoup.planning.playbooks.loader import load_playbooks
from recoup.planning.repository import persist_plan
from recoup.platform.clock import Clock, FrozenClock, get_clock
from recoup.platform.config import Settings
from recoup.platform.db import get_session
from recoup.platform.models import AuditEventRow, CaseRow, RawEvent, ScheduledActionRow
from recoup.policy.context import DndStatus, KillSwitchState, PolicyContext
from recoup.policy.engine import evaluate
from recoup.policy.repository import persist_decision

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_SECRET = "whsec_e2e_test_only"
_T0 = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
_PLAYBOOKS = load_playbooks()


def _signature(body: bytes) -> str:
    return hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _webhook_body(
    *, provider_payment_id: str, razorpay_customer_id: str, amount_paise: int
) -> bytes:
    return json.dumps(
        {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": provider_payment_id,
                        "customer_id": razorpay_customer_id,
                        "amount": amount_paise,
                        "method": "upi",
                        "error_reason": "payment_failed_due_to_insufficient_funds",
                    }
                }
            },
        }
    ).encode()


@pytest_asyncio.fixture(loop_scope="module")
async def client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """Same shape as `test_webhook_ingestion.py`'s `client` fixture --
    `httpx.AsyncClient` over `ASGITransport`, not `TestClient`, for the
    cross-loop reason that fixture's own docstring gives."""
    app = create_app()
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_clock] = lambda: FrozenClock(_T0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def redis_client() -> AsyncIterator[Redis]:
    with RedisContainer("redis:7-alpine") as r:
        client: Redis = Redis(
            host=r.get_container_host_ip(),
            port=int(r.get_exposed_port(6379)),
            decode_responses=True,
        )
        try:
            yield client
        finally:
            await client.aclose()


def _alternate_diagnose(
    case_id: CaseId, decline_category: DeclineCategory | None, clock: Clock
) -> Diagnosis:
    """A2.9: a stand-in for a hypothetical non-stub engine (P5), matching
    `DiagnosisEngine`'s call signature exactly -- proof that swapping the
    implementation needs no change to `select_playbook`'s call site,
    which only ever depends on the Protocol, never on `stub_diagnose`
    itself.
    """
    assert decline_category is not None
    return Diagnosis(
        case_id=case_id,
        hypotheses=(
            Hypothesis(
                root_cause=RootCause(decline_category.value),
                confidence=0.87,
                evidence=(),
                narration="a different engine reached the same root cause",
            ),
        ),
        method=DiagnosisMethod.LLM_RANKED,
        computed_at=clock.now(),
        llm_model="fake-alternate-v1",
        fallback_reason=None,
    )


async def _gate_and_execute(
    session: AsyncSession,
    redis_client: Redis,
    gateway: RazorpaySimulator,
    clock: Clock,
    *,
    playbook_id: str,
    claimed: list[ScheduledActionRow],
    actions_by_scheduled_id: dict[uuid.UUID, Action],
    case: Case,
) -> None:
    """The "gate then execute" half of a worker tick: evaluate policy for
    each claimed row, persist the decision, then execute -- exactly what
    T2.10 wires that no earlier phase's PR had a caller for.
    """
    ctx = PolicyContext(
        now=clock.now(),
        case=case,
        playbook_id=playbook_id,
        consent_events=(),
        dnd_status=DndStatus(registered=False),
        customer_timezone=ZoneInfo("Asia/Kolkata"),
        contact_history=(),
        mandate=None,
        kill_switch=KillSwitchState(global_tripped=False, tripped_playbooks=frozenset()),
        rate_limit_tokens={},
        daily_spend=Money(0),
    )
    for row in claimed:
        action = actions_by_scheduled_id[row.id]
        decision = evaluate(action, ctx)
        assert decision.verdict is Verdict.ALLOW
        await persist_decision(session, clock, case_id=case.id, decision=decision)
        await session.commit()

        result = await execute(
            session,
            redis_client,
            gateway,
            clock,
            action=action,
            case=case,
            scheduled_action_id=row.id,
            dry_run=True,
        )
        assert result.status is ExecutionStatus.EXECUTED


async def _reconstruct_events(session: AsyncSession, case_id: uuid.UUID) -> list[AuditEvent]:
    rows = (
        (
            await session.execute(
                select(AuditEventRow)
                .where(AuditEventRow.case_id == case_id)
                .order_by(AuditEventRow.seq)
            )
        )
        .scalars()
        .all()
    )
    return [
        AuditEvent(
            id=AuditEventId(row.id),
            case_id=CaseId(row.case_id),
            seq=row.seq,
            kind=AuditKind(row.kind),
            payload=row.payload,
            actor=Actor(ActorKind(row.actor_type), row.actor_id),
            trace_id=row.trace_id,
            occurred_at=row.occurred_at,
            prev_hash=row.prev_hash,
        )
        for row in rows
    ]


async def test_a_case_flows_webhook_to_recovered_in_dry_run(
    client: AsyncClient, engine: AsyncEngine, redis_client: Redis
) -> None:
    razorpay_customer_id = "cust_e2e_recovered"
    body = _webhook_body(
        provider_payment_id="pay_e2e_original",
        razorpay_customer_id=razorpay_customer_id,
        amount_paise=500_000,
    )
    settings = Settings(razorpay_webhook_secret=SecretStr(_SECRET))
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    # --- ingestion, twice (A2.4: raw_events dedup, TR-3) -----------------------------
    with patch("recoup.api.routes.webhooks.get_settings", return_value=settings):
        first_response = await client.post(
            "/webhooks/razorpay", content=body, headers={"x-razorpay-signature": _signature(body)}
        )
        second_response = await client.post(
            "/webhooks/razorpay", content=body, headers={"x-razorpay-signature": _signature(body)}
        )
    assert first_response.status_code == 200
    assert second_response.status_code == 200

    provider_event_id = hashlib.sha256(body).hexdigest()
    async with sessionmaker() as session:
        raw_event = (
            await session.execute(
                select(RawEvent).where(RawEvent.provider_event_id == provider_event_id)
            )
        ).scalar_one()

    # --- detection, twice (A2.4: signal dedup, TR-4) -----------------------------------
    clock_t0 = FrozenClock(_T0)
    async with sessionmaker() as session:
        case = await run_detection(session, clock_t0, seed=1, raw_event=raw_event)
    assert case is not None
    async with sessionmaker() as session:
        replay_case = await run_detection(session, clock_t0, seed=1, raw_event=raw_event)
        assert replay_case is None  # no second signal, no second case
        assert await already_detected(session, provider_event_id) is True

    # Force TREATMENT: `assign_arm` is seed-derived, and only a
    # non-control arm can ever reach EXECUTING (I7).
    case.arm = Arm.TREATMENT
    async with sessionmaker() as session:
        await session.execute(
            update(CaseRow).where(CaseRow.id == case.id).values(arm=Arm.TREATMENT.value)
        )
        await session.commit()

    # --- diagnosis (A2.9: the stub, then a swap-in that needs no caller change) -------
    stub_result = stub_diagnose(case.id, DeclineCategory.INSUFFICIENT_FUNDS, clock_t0)
    assert stub_result.method is DiagnosisMethod.STATISTICAL
    assert stub_result.root_cause == "insufficient_funds"

    diagnosis = _alternate_diagnose(case.id, DeclineCategory.INSUFFICIENT_FUNDS, clock_t0)
    case.diagnosis = diagnosis

    # --- planning -----------------------------------------------------------------------
    playbook = select_playbook(_PLAYBOOKS, diagnosis, LeakClass.L1_FAILED_ONE_TIME_PAYMENT)
    assert playbook is not None
    planning_result = build_plan(case, playbook, clock_t0)

    async with sessionmaker() as session:
        realised = await persist_plan(
            session, clock_t0, case=case, playbook=playbook, result=planning_result
        )
        await session.commit()
    assert case.state is CaseState.EXECUTING
    (retry_action, retry_scheduled_id), (link_action, link_scheduled_id) = realised

    # --- scheduler + policy gate + executor, per due step (A2.3 proven in test_executor.py;
    #     the phase gate is exercised for real here, not re-derived) --------------------
    sim = RazorpaySimulator(seed=1)

    retry_due_clock = FrozenClock(retry_action.due_at)
    async with sessionmaker() as session:
        claimed = await claim_due_batch(session, retry_due_clock, worker_id="w1", batch_size=10)
    assert [row.id for row in claimed] == [retry_scheduled_id]
    async with sessionmaker() as session:
        await _gate_and_execute(
            session,
            redis_client,
            sim,
            retry_due_clock,
            playbook_id=playbook.id,
            claimed=claimed,
            actions_by_scheduled_id={retry_scheduled_id: retry_action},
            case=case,
        )

    link_due_clock = FrozenClock(link_action.due_at)
    async with sessionmaker() as session:
        claimed = await claim_due_batch(session, link_due_clock, worker_id="w1", batch_size=10)
    assert [row.id for row in claimed] == [link_scheduled_id]
    async with sessionmaker() as session:
        await _gate_and_execute(
            session,
            redis_client,
            sim,
            link_due_clock,
            playbook_id=playbook.id,
            claimed=claimed,
            actions_by_scheduled_id={link_scheduled_id: link_action},
            case=case,
        )

    # --- the customer pays, and attribution closes the loop -----------------------------
    payment_captured_at = link_action.due_at + timedelta(hours=1)
    payment = GatewayPayment(
        id="pay_e2e_settlement",
        order_id="order_e2e",
        customer_id=razorpay_customer_id,
        amount=Money(500_000, Currency.INR),
        status=PaymentStatus.CAPTURED,
        method="upi",
        issuer="HDFC",
        error_reason=None,
        created_at=payment_captured_at,
    )
    async with sessionmaker() as session:
        attribution_result = await attribute_payment(
            session, FrozenClock(payment_captured_at), payment=payment
        )
    assert attribution_result.matched_case_id == case.id

    # --- A2.1: the case is RECOVERED --------------------------------------------------
    async with sessionmaker() as session:
        case_row = (
            await session.execute(select(CaseRow).where(CaseRow.id == case.id))
        ).scalar_one()
    assert case_row.state == CaseState.RECOVERED.value
    assert case_row.resolved_at is not None

    # --- A2.2: the audit chain verifies, gapless, every stage represented -----------
    async with sessionmaker() as session:
        events = await _reconstruct_events(session, case.id)
    assert verify_chain(events) is None
    kinds = {event.kind for event in events}
    assert kinds == {
        AuditKind.SIGNAL_DETECTED,
        AuditKind.CASE_OPENED,
        AuditKind.ARM_ASSIGNED,
        AuditKind.PLAN_CREATED,
        AuditKind.ACTION_CLAIMED,
        AuditKind.POLICY_EVALUATED,
        AuditKind.ACTION_EXECUTED,
        AuditKind.PAYMENT_ATTRIBUTED,
        AuditKind.CASE_RESOLVED,
    }


async def test_an_action_with_no_persisted_decision_raises_rather_than_executing(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    """A2.3, in the context of a plan the pipeline actually produced --
    `test_executor.py` proves this against a hand-seeded action; this
    proves it holds for one `persist_plan` itself realised."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    clock_t0 = FrozenClock(_T0)

    body = _webhook_body(
        provider_payment_id="pay_e2e_no_decision",
        razorpay_customer_id="cust_e2e_no_decision",
        amount_paise=500_000,
    )
    async with sessionmaker() as session:
        await store_raw_event(
            session,
            clock_t0,
            source=RAZORPAY_SOURCE,
            event_type="payment.failed",
            provider_event_id=hashlib.sha256(body).hexdigest(),
            payload=json.loads(body),
        )
        raw_event = (
            await session.execute(
                select(RawEvent).where(
                    RawEvent.provider_event_id == hashlib.sha256(body).hexdigest()
                )
            )
        ).scalar_one()
        case = await run_detection(session, clock_t0, seed=1, raw_event=raw_event)
    assert case is not None
    case.arm = Arm.TREATMENT
    async with sessionmaker() as session:
        await session.execute(
            update(CaseRow).where(CaseRow.id == case.id).values(arm=Arm.TREATMENT.value)
        )
        await session.commit()

    diagnosis = stub_diagnose(case.id, DeclineCategory.INSUFFICIENT_FUNDS, clock_t0)
    case.diagnosis = diagnosis
    playbook = select_playbook(_PLAYBOOKS, diagnosis, LeakClass.L1_FAILED_ONE_TIME_PAYMENT)
    assert playbook is not None
    planning_result = build_plan(case, playbook, clock_t0)

    async with sessionmaker() as session:
        realised = await persist_plan(
            session, clock_t0, case=case, playbook=playbook, result=planning_result
        )
        await session.commit()
    (retry_action, retry_scheduled_id), _ = realised

    sim = RazorpaySimulator(seed=1)
    retry_due_clock = FrozenClock(retry_action.due_at)
    async with sessionmaker() as session:
        claimed = await claim_due_batch(session, retry_due_clock, worker_id="w1", batch_size=10)
    assert [row.id for row in claimed] == [retry_scheduled_id]

    # No policy decision was ever persisted for this action.
    async with sessionmaker() as session:
        with pytest.raises(NoAllowDecisionError):
            await execute(
                session,
                redis_client,
                sim,
                retry_due_clock,
                action=retry_action,
                case=case,
                scheduled_action_id=retry_scheduled_id,
                dry_run=True,
            )


async def test_opt_out_mid_plan_cancels_remaining_steps_and_closes_suppressed(
    engine: AsyncEngine,
) -> None:
    """A2.8. `suppress_case` is called directly here, standing in for the
    consent-ledger-driven stopping rule (`customer_opt_out -> SUPPRESSED`,
    POLICY-ENGINE R2, PHASE-04 T4.2) that will trigger it automatically
    once that phase builds it."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    clock_t0 = FrozenClock(_T0)

    body = _webhook_body(
        provider_payment_id="pay_e2e_opt_out",
        razorpay_customer_id="cust_e2e_opt_out",
        amount_paise=500_000,
    )
    async with sessionmaker() as session:
        await store_raw_event(
            session,
            clock_t0,
            source=RAZORPAY_SOURCE,
            event_type="payment.failed",
            provider_event_id=hashlib.sha256(body).hexdigest(),
            payload=json.loads(body),
        )
        raw_event = (
            await session.execute(
                select(RawEvent).where(
                    RawEvent.provider_event_id == hashlib.sha256(body).hexdigest()
                )
            )
        ).scalar_one()
        case = await run_detection(session, clock_t0, seed=1, raw_event=raw_event)
    assert case is not None
    case.arm = Arm.TREATMENT
    async with sessionmaker() as session:
        await session.execute(
            update(CaseRow).where(CaseRow.id == case.id).values(arm=Arm.TREATMENT.value)
        )
        await session.commit()

    diagnosis = stub_diagnose(case.id, DeclineCategory.INSUFFICIENT_FUNDS, clock_t0)
    case.diagnosis = diagnosis
    playbook = select_playbook(_PLAYBOOKS, diagnosis, LeakClass.L1_FAILED_ONE_TIME_PAYMENT)
    assert playbook is not None
    planning_result = build_plan(case, playbook, clock_t0)

    async with sessionmaker() as session:
        await persist_plan(session, clock_t0, case=case, playbook=playbook, result=planning_result)
        await session.commit()
    assert case.state is CaseState.EXECUTING

    async with sessionmaker() as session:
        cancelled = await suppress_case(
            session, clock_t0, case_id=case.id, reason_code="customer_opt_out"
        )
        await session.commit()
    assert cancelled == 2  # both retry and payment_link, still pending

    async with sessionmaker() as session:
        case_row = (
            await session.execute(select(CaseRow).where(CaseRow.id == case.id))
        ).scalar_one()
        events = await _reconstruct_events(session, case.id)

    assert case_row.state == CaseState.SUPPRESSED.value
    assert verify_chain(events) is None
    assert events[-1].kind is AuditKind.CASE_RESOLVED
    assert events[-1].payload["reason_code"] == "customer_opt_out"
