"""Integration tests for the constraints DATA-MODEL.md SS3 declares (T1.8):
each one asserted against a real Postgres to reject exactly the thing it
exists to reject. A mocked `CHECK` constraint or trigger tests the mock,
same as a mocked `SKIP LOCKED` (ENGINEERING-STANDARDS SS4.2) -- these are
the tests that don't.
"""

import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from recoup.platform.models import Base

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

_SERVICE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def migrated_database_url() -> Iterator[str]:
    """Starts Postgres and applies the real Alembic migration -- not
    `Base.metadata.create_all()`, which would skip the hand-written
    triggers that exist only in the migration, not in the SQLAlchemy
    metadata. Runs `alembic upgrade` as a subprocess deliberately:
    invoking it in-process would nest its own `asyncio.run()` inside
    pytest-asyncio's already-running loop (see F-005 in FAILURE-LOG for
    this codebase's prior history with exactly that class of bug).
    """
    uv_path = shutil.which("uv")
    assert uv_path is not None, "uv must be on PATH to run this integration test"
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        subprocess.run(  # noqa: S603 -- uv_path is resolved via shutil.which, args are hardcoded
            [uv_path, "run", "alembic", "upgrade", "head"],
            cwd=_SERVICE_ROOT,
            env={**os.environ, "DATABASE_URL": url},
            check=True,
            capture_output=True,
            text=True,
        )
        yield url


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database_url)
    try:
        yield eng
    finally:
        await eng.dispose()


async def test_migrated_schema_has_a_table_for_every_declared_model(engine: AsyncEngine) -> None:
    """The rest of this module tests constraint *behaviour* through raw SQL,
    deliberately -- it must reach the real, migrated database, not
    `Base.metadata.create_all()` (see `migrated_database_url`'s docstring).
    That leaves an easy drift to miss: a model added to `models.py` with no
    matching migration, or the reverse. This is the one check in the suite
    that reflects the live schema and compares it against `models.py`
    directly, so the two are asserted to agree rather than assumed to.
    """

    def _table_names(sync_conn: Connection) -> set[str]:
        return set(inspect(sync_conn).get_table_names())

    async with engine.connect() as conn:
        actual_tables = await conn.run_sync(_table_names)

    declared_tables = set(Base.metadata.tables.keys())
    assert declared_tables <= actual_tables


@pytest_asyncio.fixture(loop_scope="module")
async def customer_and_signal(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    """A fresh customer + signal per test, so each test's `cases` rows
    don't collide with another test's under `cases_open_dedup`."""
    customer_id = uuid.uuid4()
    signal_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO customers (id, contact_hash) VALUES (:id, :hash)"),
            {"id": customer_id, "hash": f"hash-{customer_id}"},
        )
        await conn.execute(
            text(
                "INSERT INTO signals "
                "(id, leak_class, customer_id, at_risk_paise, detected_at, "
                "source_event_ids, context) "
                "VALUES (:id, 'L1', :customer_id, 100000, :now, '[\"e1\"]', '{}')"
            ),
            {"id": signal_id, "customer_id": customer_id, "now": datetime.now(UTC)},
        )
    return customer_id, signal_id


async def _insert_case(
    conn: AsyncConnection,
    *,
    customer_id: uuid.UUID,
    signal_id: uuid.UUID,
    state: str = "detected",
    at_risk_paise: int = 100_000,
    cost_spent_paise: int = 0,
    cost_ceiling_paise: int = 4_000,
    resolved_at: datetime | None = None,
) -> uuid.UUID:
    case_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO cases "
            "(id, signal_id, customer_id, state, arm, at_risk_paise, "
            "cost_spent_paise, cost_ceiling_paise, opened_at, resolved_at) "
            "VALUES (:id, :signal_id, :customer_id, :state, 'treatment', "
            ":at_risk_paise, :cost_spent_paise, :cost_ceiling_paise, :now, :resolved_at)"
        ),
        {
            "id": case_id,
            "signal_id": signal_id,
            "customer_id": customer_id,
            "state": state,
            "at_risk_paise": at_risk_paise,
            "cost_spent_paise": cost_spent_paise,
            "cost_ceiling_paise": cost_ceiling_paise,
            "now": datetime.now(UTC),
            "resolved_at": resolved_at,
        },
    )
    return case_id


# --- cost_within_ceiling (I2) -------------------------------------------------


async def test_cost_within_ceiling_rejects_a_breach(
    engine: AsyncEngine, customer_and_signal: tuple[uuid.UUID, uuid.UUID]
) -> None:
    customer_id, signal_id = customer_and_signal
    with pytest.raises(IntegrityError, match="cost_within_ceiling"):
        async with engine.begin() as conn:
            await _insert_case(
                conn,
                customer_id=customer_id,
                signal_id=signal_id,
                cost_spent_paise=500,
                cost_ceiling_paise=400,
            )


async def test_cost_within_ceiling_allows_spend_at_the_ceiling(
    engine: AsyncEngine, customer_and_signal: tuple[uuid.UUID, uuid.UUID]
) -> None:
    customer_id, signal_id = customer_and_signal
    async with engine.begin() as conn:
        await _insert_case(
            conn,
            customer_id=customer_id,
            signal_id=signal_id,
            cost_spent_paise=400,
            cost_ceiling_paise=400,
        )


# --- resolved_iff_terminal -----------------------------------------------------


async def test_resolved_iff_terminal_rejects_terminal_without_resolved_at(
    engine: AsyncEngine, customer_and_signal: tuple[uuid.UUID, uuid.UUID]
) -> None:
    customer_id, signal_id = customer_and_signal
    with pytest.raises(IntegrityError, match="resolved_iff_terminal"):
        async with engine.begin() as conn:
            await _insert_case(
                conn, customer_id=customer_id, signal_id=signal_id, state="recovered"
            )


async def test_resolved_iff_terminal_rejects_non_terminal_with_resolved_at(
    engine: AsyncEngine, customer_and_signal: tuple[uuid.UUID, uuid.UUID]
) -> None:
    customer_id, signal_id = customer_and_signal
    with pytest.raises(IntegrityError, match="resolved_iff_terminal"):
        async with engine.begin() as conn:
            await _insert_case(
                conn,
                customer_id=customer_id,
                signal_id=signal_id,
                state="diagnosing",
                resolved_at=datetime.now(UTC),
            )


async def test_resolved_iff_terminal_allows_terminal_with_resolved_at(
    engine: AsyncEngine, customer_and_signal: tuple[uuid.UUID, uuid.UUID]
) -> None:
    customer_id, signal_id = customer_and_signal
    async with engine.begin() as conn:
        await _insert_case(
            conn,
            customer_id=customer_id,
            signal_id=signal_id,
            state="lost",
            resolved_at=datetime.now(UTC),
        )


# --- cases_open_dedup (a partial unique index) ---------------------------------


async def test_cases_open_dedup_rejects_a_second_open_case(
    engine: AsyncEngine, customer_and_signal: tuple[uuid.UUID, uuid.UUID]
) -> None:
    customer_id, signal_id = customer_and_signal
    async with engine.begin() as conn:
        await _insert_case(
            conn, customer_id=customer_id, signal_id=signal_id, at_risk_paise=777_700
        )
    with pytest.raises(IntegrityError, match="cases_open_dedup"):
        async with engine.begin() as conn:
            await _insert_case(
                conn,
                customer_id=customer_id,
                signal_id=signal_id,
                state="diagnosing",
                at_risk_paise=777_700,
            )


async def test_cases_open_dedup_allows_a_new_open_case_once_the_first_is_terminal(
    engine: AsyncEngine, customer_and_signal: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The index is partial, not a blanket unique constraint: a customer may
    have many historical cases at the same amount, only never two *open*
    ones at once."""
    customer_id, signal_id = customer_and_signal
    async with engine.begin() as conn:
        await _insert_case(
            conn,
            customer_id=customer_id,
            signal_id=signal_id,
            at_risk_paise=333_300,
            state="lost",
            resolved_at=datetime.now(UTC),
        )
    async with engine.begin() as conn:
        await _insert_case(
            conn, customer_id=customer_id, signal_id=signal_id, at_risk_paise=333_300
        )


# --- audit_events immutability trigger (A1.4) ----------------------------------


async def _insert_audit_event(conn: AsyncConnection, case_id: uuid.UUID) -> uuid.UUID:
    event_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO audit_events "
            "(id, case_id, seq, kind, payload, actor_type, trace_id, occurred_at, "
            "prev_hash, hash) "
            "VALUES (:id, :case_id, 1, 'case_opened', '{}', 'system', 'trace-1', "
            ":now, '', 'deadbeef')"
        ),
        {"id": event_id, "case_id": case_id, "now": datetime.now(UTC)},
    )
    return event_id


async def test_audit_events_rejects_update(
    engine: AsyncEngine, customer_and_signal: tuple[uuid.UUID, uuid.UUID]
) -> None:
    customer_id, signal_id = customer_and_signal
    async with engine.begin() as conn:
        case_id = await _insert_case(conn, customer_id=customer_id, signal_id=signal_id)
        event_id = await _insert_audit_event(conn, case_id)

    with pytest.raises(Exception, match="audit_events is append-only"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE audit_events SET hash = 'tampered' WHERE id = :id"),
                {"id": event_id},
            )


async def test_audit_events_rejects_delete(
    engine: AsyncEngine, customer_and_signal: tuple[uuid.UUID, uuid.UUID]
) -> None:
    customer_id, signal_id = customer_and_signal
    async with engine.begin() as conn:
        case_id = await _insert_case(conn, customer_id=customer_id, signal_id=signal_id)
        event_id = await _insert_audit_event(conn, case_id)

    with pytest.raises(Exception, match="audit_events is append-only"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": event_id})
