"""`recoup events import` against a real, migrated Postgres (T2.1's bulk
import path).

Deliberately a plain sync test, not `async def` -- `import_events` drives
its own `asyncio.run()` internally (see `cli.py`), and calling that from
inside a test coroutine already running on pytest-asyncio's loop would nest
event loops, the exact class of bug this codebase's F-005 warns about. A
sync test has no loop already running on its thread, so `asyncio.run()`
starting a fresh one here is safe -- which is also why this test points the
command at the test database via `get_settings`, not by handing it an
already-loop-bound `AsyncEngine` the way `test_webhook_ingestion.py`'s
`client` fixture must avoid (see that fixture's docstring for the
cross-loop failure that produces): `_import_events` builds and disposes
its own engine inside its own loop.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from recoup.cli import app
from recoup.platform.config import Settings

pytestmark = pytest.mark.integration

runner = CliRunner()


def test_import_events_processes_a_jsonl_file(migrated_database_url: str, tmp_path: Path) -> None:
    settings = Settings(
        database_url=migrated_database_url, razorpay_webhook_secret=SecretStr("unused")
    )

    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        '{"event": "payment.captured", "payload": {}}\n'
        "\n"  # a blank line, as a real export might have -- skipped, not an error
        "{not valid json\n"
        '{"event": "payment.captured", "payload": {}}\n'  # duplicate of line 1
    )

    # `_import_events` imports `get_settings` locally (`from
    # recoup.platform.config import get_settings`), so the name is
    # resolved fresh from its origin module at call time -- patch it
    # there, not on `recoup.cli`, where no such module-level name exists.
    with patch("recoup.platform.config.get_settings", return_value=settings):
        result = runner.invoke(app, ["import-events", str(events_file)])

    assert result.exit_code == 0, result.stdout
    assert "imported 2" in result.stdout
    assert "1 duplicate" in result.stdout
    assert "1 unparseable" in result.stdout
