"""Loads and schema-validates every playbook YAML at startup (TR-16): an
invalid playbook must prevent boot, not degrade the first time a case
reaches planning.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from recoup.planning.playbooks.schema import Playbook

__all__ = ["PlaybookLoadError", "load_playbooks"]

_PLAYBOOKS_DIR = Path(__file__).parent


class PlaybookLoadError(RuntimeError):
    """Raised at startup when a playbook file fails to parse or validate."""


def load_playbooks(directory: Path | None = None) -> dict[str, Playbook]:
    """Every `*.yaml` file in `directory` (default: this package), keyed by
    playbook id. Raises `PlaybookLoadError` -- naming the offending file --
    on the first invalid one, so a broken playbook fails the boot it would
    have shipped with rather than the first case that reaches it.
    """
    target = directory if directory is not None else _PLAYBOOKS_DIR
    playbooks: dict[str, Playbook] = {}
    for path in sorted(target.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            playbook = Playbook.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            raise PlaybookLoadError(f"{path}: {exc}") from exc
        if playbook.id in playbooks:
            raise PlaybookLoadError(
                f"{path}: duplicate playbook id {playbook.id!r} "
                "(already loaded from another file in this directory)"
            )
        playbooks[playbook.id] = playbook
    return playbooks
