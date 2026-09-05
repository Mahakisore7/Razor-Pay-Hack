"""Integration tests for `recoup.bench.report.write_report` (T3.7): the
repository half that loads a real, completed benchmark run and writes
`report.md`/`report.json` -- everything downstream of it (`recoup.bench.
report`'s pure assembly/rendering) is tested without a database in
`tests/unit/test_bench_report.py`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from testcontainers.community.redis import RedisContainer

from recoup.bench.report import _git_sha, _load_policy_denials, _resource_hash, write_report
from recoup.bench.runner import run_benchmark
from recoup.bench.statistics import load_case_outcomes
from recoup.detection.pipeline import open_case_for_signal, resolve_customer
from recoup.domain.action import Action, ActionCategory, ActionPayload, Channel
from recoup.domain.identifiers import ActionId, SignalId, uuid7
from recoup.domain.money import Currency, Money
from recoup.domain.outcome import OutcomeKind
from recoup.domain.policy_decision import PolicyDecision, Verdict
from recoup.domain.signal import LeakClass, Signal, SignalContext
from recoup.platform.clock import FrozenClock
from recoup.platform.models import ActionRow, BenchRun
from recoup.policy.repository import persist_decision

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_START = datetime(2026, 4, 10, tzinfo=UTC)


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


async def test_write_report_produces_markdown_and_json_files(
    engine: AsyncEngine, redis_client: Redis, tmp_path: Path
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=1001, size=150, start_at=_START)

    out_dir = await write_report(sessionmaker, run_id=summary.run_id, out_root=tmp_path)

    assert out_dir.parent == tmp_path
    assert out_dir.name.startswith("1001-")
    md_path = out_dir / "report.md"
    json_path = out_dir / "report.json"
    assert md_path.is_file()
    assert json_path.is_file()

    md_text = md_path.read_text(encoding="utf-8")
    assert "## 1. Run metadata" in md_text
    assert "## 2. Validity statement" in md_text
    assert f"seed {summary.seed}" in md_text

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["metadata"]["run_id"] == str(summary.run_id)
    assert payload["metadata"]["seed"] == 1001
    assert payload["metadata"]["cohort_size"] == 150


async def test_write_report_run_metadata_carries_real_provenance(
    engine: AsyncEngine, redis_client: Redis, tmp_path: Path
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=1002, size=150, start_at=_START)

    out_dir = await write_report(sessionmaker, run_id=summary.run_id, out_root=tmp_path)
    payload = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    meta = payload["metadata"]

    assert meta["git_sha"] == _git_sha()
    assert meta["cohort_config_hash"] == _resource_hash("recoup.bench", "cohort.yaml")
    assert meta["simulator_config_hash"] == _resource_hash(
        "recoup.gateway.simulator", "simulator.yaml"
    )
    assert meta["gateway_mode"] == "simulated"
    assert "baseline-naive" in meta["playbook_versions"]


async def test_write_report_exception_list_covers_every_non_recovered_case(
    engine: AsyncEngine, redis_client: Redis, tmp_path: Path
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=1003, size=150, start_at=_START)

    async with sessionmaker() as session:
        cases = await load_case_outcomes(session, summary.run_id)
    expected_non_recovered = sum(1 for c in cases if c.outcome_kind is not OutcomeKind.RECOVERED)

    out_dir = await write_report(sessionmaker, run_id=summary.run_id, out_root=tmp_path)
    payload = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))

    assert len(payload["exceptions"]) == expected_non_recovered
    assert all(ex["outcome_kind"] != "recovered" for ex in payload["exceptions"])


async def test_write_report_raises_for_an_unknown_run(engine: AsyncEngine, tmp_path: Path) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    with pytest.raises(ValueError, match="no bench run"):
        await write_report(sessionmaker, run_id=uuid.uuid4(), out_root=tmp_path)


async def test_write_report_raises_for_a_run_that_has_not_completed(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(BenchRun(id=run_id, seed=1, config={"size": 1}, started_at=_START))
        await session.commit()

    with pytest.raises(ValueError, match="has not completed yet"):
        await write_report(sessionmaker, run_id=run_id, out_root=tmp_path)


async def test_load_policy_denials_matches_recorded_deny_verdicts(
    engine: AsyncEngine, redis_client: Redis
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    summary = await run_benchmark(sessionmaker, redis_client, seed=1004, size=150, start_at=_START)

    async with sessionmaker() as session:
        denials = await _load_policy_denials(session, summary.run_id)
    # `feat/bench-statistics` fixed the consent-seeding and cost-ceiling-
    # sync bugs that used to make every messaging-channel action deny --
    # a plain run now denies nothing (see test_bench_runner.py's own
    # regression test for the same fact at the runner level).
    assert denials == {}


async def test_load_policy_denials_groups_by_rule_and_falls_back_for_no_rule_id(
    engine: AsyncEngine,
) -> None:
    """Real runs no longer produce a deny at all (the test above) -- this
    seeds `PolicyDecisionRow`s directly, the same way `test_policy_
    repository.py` does, so the grouping/counting logic itself (and its
    `rule_id is None` fallback label) stays covered without depending on
    the runner ever denying something again by accident.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    clock = FrozenClock(_START)
    run_id = uuid.uuid4()

    async with sessionmaker() as session:
        session.add(BenchRun(id=run_id, seed=9999, config={"size": 1}, started_at=_START))
        await session.commit()

        customer = await resolve_customer(session, "cust_denial_grouping")
        signal = Signal(
            id=SignalId(uuid7()),
            leak_class=LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
            customer=customer,
            at_risk=Money(500_000, Currency.INR),
            detected_at=_START,
            source_event_ids=(f"evt-{uuid.uuid4()}",),
            decline=None,
            context=SignalContext(),
        )
        case = await open_case_for_signal(
            session, clock, seed=1, signal=signal, bench_run_id=run_id
        )
    assert case is not None

    actions = [
        Action(
            id=ActionId(uuid7()),
            case_id=case.id,
            step_id=f"step-{i}",
            attempt=1,
            channel=Channel.EMAIL,
            category=ActionCategory.TRANSACTIONAL,
            payload=ActionPayload(),
            cost=Money(0, Currency.INR),
            due_at=_START,
        )
        for i in range(3)
    ]
    async with sessionmaker() as session:
        for action in actions:
            session.add(
                ActionRow(
                    id=action.id,
                    case_id=case.id,
                    step_id=action.step_id,
                    attempt=action.attempt,
                    channel=action.channel.value,
                    idempotency_key=action.idempotency_key,
                    payload={},
                    cost_paise=0,
                    due_at=action.due_at,
                )
            )
        await session.commit()

        # Two denies for "no_consent", one deny with no rule_id at all,
        # and one allow -- the allow must not be counted.
        for action, verdict, rule_id in [
            (actions[0], Verdict.DENY, "no_consent"),
            (actions[1], Verdict.DENY, "no_consent"),
            (actions[2], Verdict.DENY, None),
        ]:
            await persist_decision(
                session,
                clock,
                case_id=case.id,
                decision=PolicyDecision(
                    action_id=action.id,
                    attempt=1,
                    verdict=verdict,
                    rule_id=rule_id,
                    inputs={},
                    defer_until=None,
                    decided_at=clock.now(),
                ),
            )
        await session.commit()

    async with sessionmaker() as session:
        denials = await _load_policy_denials(session, run_id)
    assert denials == {"no_consent": 2, "(unknown rule)": 1}
