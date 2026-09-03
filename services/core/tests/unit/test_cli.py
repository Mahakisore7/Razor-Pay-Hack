"""The `recoup` CLI's own wiring, and the worker/scheduler placeholder loop.

`serve` is exercised implicitly by every other test importing `recoup.api.app`
(it is a thin `uvicorn.run` call with no branch of its own); the interesting
surface here is `version`, the worker/scheduler commands delegating correctly,
and the placeholder loop actually writing a heartbeat before it would sleep.
"""

from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from recoup.cli import _run_placeholder_process, app

runner = CliRunner()


def test_version_prints_the_installed_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


@pytest.mark.parametrize("command", ["worker", "scheduler"])
def test_process_commands_delegate_to_the_placeholder_loop(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    delegate = MagicMock()
    monkeypatch.setattr("recoup.cli._run_placeholder_process", delegate)

    result = runner.invoke(app, [command, "--poll-interval", "1.5"])

    assert result.exit_code == 0
    delegate.assert_called_once_with(command, 1.5)


def test_placeholder_loop_writes_a_heartbeat_before_each_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat_file = tmp_path / "recoup-worker.heartbeat"
    monkeypatch.setattr("recoup.cli.Path", lambda _path: heartbeat_file)

    class _LoopStoppedError(Exception):
        pass

    def _sleep_then_stop(_seconds: float) -> NoReturn:
        raise _LoopStoppedError

    monkeypatch.setattr("recoup.cli.time.sleep", _sleep_then_stop)

    with pytest.raises(_LoopStoppedError):
        _run_placeholder_process("worker", 0.01)

    written = float(heartbeat_file.read_text())
    assert written > 0
