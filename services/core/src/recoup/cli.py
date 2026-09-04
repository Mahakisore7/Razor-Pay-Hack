"""The `recoup` CLI.

Commands are added as the phases that need them land -- `audit verify` in
Phase 2, `bench run` in Phase 3. Phase 0 ships the entry point itself and a
`serve` command, so the API is runnable without reaching into uvicorn directly.
"""

import time
import uuid
from pathlib import Path

import typer
import uvicorn

app = typer.Typer(
    name="recoup", help="Recoup -- revenue recovery control plane.", no_args_is_help=True
)
audit_app = typer.Typer(name="audit", help="Audit chain tools.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")


@app.command()
def version() -> None:
    """Print the installed version."""
    from importlib.metadata import version as pkg_version

    typer.echo(pkg_version("recoup"))


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind address."),  # noqa: S104 -- container-bound by design
    port: int = typer.Option(8000, help="Bind port."),
    reload: bool = typer.Option(False, help="Autoreload on code change (development only)."),
) -> None:
    """Run the API service."""
    uvicorn.run("recoup.api.app:app", host=host, port=port, reload=reload)


@app.command()
def worker(
    poll_interval: float = typer.Option(5.0, help="Seconds between idle heartbeats."),
) -> None:
    """Run the worker pool.

    Placeholder: the outbox and executor it will claim from don't exist yet
    (ROADMAP P2/P3). This idles and writes a heartbeat so the compose stack
    (T0.5) has a real, health-checkable process to bring up now rather than
    inventing one later when the container shape is harder to change.
    """
    _run_placeholder_process("worker", poll_interval)


@app.command()
def scheduler(
    poll_interval: float = typer.Option(5.0, help="Seconds between idle heartbeats."),
) -> None:
    """Run the scheduler.

    Placeholder: there is no outbox to tick yet (ROADMAP P2/P3). See `worker`
    for why this exists in Phase 0 anyway.
    """
    _run_placeholder_process("scheduler", poll_interval)


@app.command("import-events")
def import_events(
    path: Path = typer.Argument(..., help="JSONL file, one Razorpay webhook body per line."),
) -> None:
    """Bulk-import historical webhook records into `raw_events` (T2.1).

    Skips HMAC verification -- these are already-trusted exported records,
    not live deliveries -- but otherwise takes the same parse, categorize,
    and durably-store path as the webhook route, so a record already
    present (matched by the same `sha256(raw_body)` dedup key) is a no-op
    (TR-3) and a line that fails to parse is stored flagged, not dropped
    (TR-5), exactly as it would be over HTTP (TR-4: interpretation is
    re-runnable over what is already stored, without re-fetching).
    """
    import asyncio

    # Read synchronously here, in the sync command, rather than inside the
    # async worker below -- a blocking `Path.read_text` call from inside
    # `async def` code holds up the event loop for no reason (ASYNC240).
    lines = path.read_text("utf-8").splitlines()
    asyncio.run(_import_events(lines))


async def _import_events(lines: list[str]) -> None:
    import hashlib

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from recoup.gateway.ingestion import (
        RAZORPAY_SOURCE,
        UnparseableEventError,
        parse_razorpay_event,
        store_raw_event,
    )
    from recoup.platform.clock import SystemClock
    from recoup.platform.config import get_settings
    from recoup.platform.logging import configure_logging, get_logger

    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger("recoup.cli.import_events")
    clock = SystemClock()

    # A dedicated engine, not the process-wide `lru_cache`d one
    # `platform.db.get_sessionmaker` hands a long-running server -- this
    # command is one-shot, and the event loop `asyncio.run` (above) gives
    # it closes the moment this coroutine returns, so the engine must be
    # created *and disposed* inside that same loop rather than left open
    # for a singleton that assumes the process keeps running.
    engine = create_async_engine(settings.database_url)
    inserted = duplicates = flagged = 0
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            for line_no, raw_line in enumerate(lines, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                raw_body = line.encode("utf-8")
                provider_event_id = hashlib.sha256(raw_body).hexdigest()
                try:
                    event_type, payload = parse_razorpay_event(raw_body)
                except UnparseableEventError as exc:
                    logger.warning("import_line_unparseable", line=line_no, error=str(exc))
                    event_type = "_unparseable"
                    payload = {"_ingestion_error": "unparseable_json", "raw_line": line}
                    flagged += 1

                was_new = await store_raw_event(
                    session,
                    clock,
                    source=RAZORPAY_SOURCE,
                    event_type=event_type,
                    provider_event_id=provider_event_id,
                    payload=payload,
                )
                inserted += was_new
                duplicates += not was_new
    finally:
        await engine.dispose()

    typer.echo(f"imported {inserted}, skipped {duplicates} duplicate(s), {flagged} unparseable")


@audit_app.command("verify")
def audit_verify(
    case: str = typer.Option(..., "--case", help="Case id (UUID) to verify."),
) -> None:
    """Recompute a case's audit chain and report the first divergence, if
    any (DOMAIN-MODEL SS10). Exits non-zero on a diverged or empty chain,
    so this is safe to run from a script that needs to know.
    """
    import asyncio

    try:
        case_id = uuid.UUID(case)
    except ValueError as exc:
        typer.echo(f"'{case}' is not a valid case id: {exc}")
        raise typer.Exit(code=1) from exc

    asyncio.run(_audit_verify(case_id))


async def _audit_verify(case_id: uuid.UUID) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from recoup.audit.events import Actor, ActorKind, AuditEvent, AuditKind
    from recoup.audit.verify import verify_chain
    from recoup.domain.identifiers import AuditEventId, CaseId
    from recoup.platform.config import get_settings
    from recoup.platform.models import AuditEventRow

    settings = get_settings()
    # A dedicated, disposed-on-exit engine -- see `_import_events` above
    # for why a one-shot command builds its own rather than reusing the
    # process-wide singleton `platform.db.get_sessionmaker` hands a
    # long-running server.
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            result = await session.execute(
                select(AuditEventRow)
                .where(AuditEventRow.case_id == case_id)
                .order_by(AuditEventRow.seq)
            )
            rows = result.scalars().all()
    finally:
        await engine.dispose()

    if not rows:
        typer.echo(f"case {case_id}: no audit events found")
        raise typer.Exit(code=1)

    events = [
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

    divergence = verify_chain(events)
    if divergence is not None:
        typer.echo(f"case {case_id}: chain diverges at seq {divergence}")
        raise typer.Exit(code=1)
    typer.echo(f"case {case_id}: chain intact, {len(events)} event(s)")


def _run_placeholder_process(name: str, poll_interval: float) -> None:
    from recoup.platform.config import get_settings
    from recoup.platform.logging import configure_logging, get_logger

    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger(f"recoup.{name}")
    heartbeat_file = Path(f"/tmp/recoup-{name}.heartbeat")  # noqa: S108 -- container-local, not shared

    logger.info("placeholder_process_started", process=name, poll_interval=poll_interval)
    # A plain blocking loop, deliberately -- there is no I/O to be concurrent
    # with yet. `async def` would invite the false impression that this
    # already does the work the real worker/scheduler will do.
    while True:
        heartbeat_file.write_text(str(time.time()))
        time.sleep(poll_interval)


if __name__ == "__main__":
    app()
