"""Unit tests for `recoup.planning.playbooks.loader` (T2.4, TR-16) -- the
shipped playbook loads correctly, and every way a playbook YAML can be
malformed fails loudly, naming the offending file, rather than booting on
a broken config.
"""

from pathlib import Path

import pytest

from recoup.domain.action import Channel
from recoup.domain.signal import LeakClass
from recoup.planning.playbooks.loader import PlaybookLoadError, load_playbooks

_VALID_YAML = """\
id: test-playbook
version: 1
applies_to:
  root_cause: insufficient_funds
  leak_classes: [L1]
cost_ceiling_pct: 4.0
max_attempts: 3
max_case_age_days: 21
steps:
  - id: retry
    channel: payment_retry
    timing: { policy: fixed, offset_hours: 6 }
    expected_cost_paise: 0
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# --- the shipped playbook -----------------------------------------------------


def test_the_shipped_playbook_directory_loads() -> None:
    playbooks = load_playbooks()
    assert set(playbooks) == {"insufficient-funds"}

    playbook = playbooks["insufficient-funds"]
    assert playbook.version == 1
    assert playbook.applies_to.root_cause == "insufficient_funds"
    assert playbook.applies_to.leak_classes == (
        LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
        LeakClass.L2_FAILED_MANDATE_DEBIT,
        LeakClass.L3_HALTED_SUBSCRIPTION,
    )
    assert [step.id for step in playbook.steps] == ["retry", "payment_link"]
    assert playbook.steps[0].channel == Channel.PAYMENT_RETRY
    assert playbook.steps[1].channel == Channel.LINK
    assert playbook.steps[1].timing.after_step == "retry"


# --- a well-formed directory ---------------------------------------------------


def test_load_playbooks_reads_every_yaml_file_in_the_directory(tmp_path: Path) -> None:
    _write(tmp_path, "test-playbook.yaml", _VALID_YAML)
    playbooks = load_playbooks(tmp_path)
    assert set(playbooks) == {"test-playbook"}


def test_load_playbooks_returns_an_empty_dict_for_an_empty_directory(tmp_path: Path) -> None:
    assert load_playbooks(tmp_path) == {}


# --- malformed playbooks --------------------------------------------------------


def test_load_playbooks_rejects_invalid_yaml_syntax(tmp_path: Path) -> None:
    _write(tmp_path, "broken.yaml", "id: [unterminated\n")
    with pytest.raises(PlaybookLoadError, match="broken"):
        load_playbooks(tmp_path)


def test_load_playbooks_rejects_a_missing_required_field(tmp_path: Path) -> None:
    _write(tmp_path, "missing-field.yaml", _VALID_YAML.replace("version: 1\n", ""))
    with pytest.raises(PlaybookLoadError, match="missing-field"):
        load_playbooks(tmp_path)


def test_load_playbooks_rejects_an_unknown_field(tmp_path: Path) -> None:
    _write(tmp_path, "extra-field.yaml", _VALID_YAML + "not_a_real_field: true\n")
    with pytest.raises(PlaybookLoadError):
        load_playbooks(tmp_path)


def test_load_playbooks_rejects_cost_ceiling_pct_above_ten(tmp_path: Path) -> None:
    _write(tmp_path, "too-expensive.yaml", _VALID_YAML.replace("4.0", "10.5"))
    with pytest.raises(PlaybookLoadError):
        load_playbooks(tmp_path)


def test_load_playbooks_rejects_an_unknown_channel(tmp_path: Path) -> None:
    _write(tmp_path, "bad-channel.yaml", _VALID_YAML.replace("payment_retry", "carrier_pigeon"))
    with pytest.raises(PlaybookLoadError):
        load_playbooks(tmp_path)


def test_load_playbooks_rejects_a_duplicate_step_id(tmp_path: Path) -> None:
    duplicated = _VALID_YAML + (
        "  - id: retry\n"
        "    channel: payment_retry\n"
        "    timing: { policy: fixed, offset_hours: 1 }\n"
        "    expected_cost_paise: 0\n"
    )
    _write(tmp_path, "dup-step.yaml", duplicated)
    with pytest.raises(PlaybookLoadError, match="duplicate step id"):
        load_playbooks(tmp_path)


def test_load_playbooks_rejects_a_relative_step_referencing_an_unknown_step(
    tmp_path: Path,
) -> None:
    extended = _VALID_YAML + (
        "  - id: follow_up\n"
        "    channel: link\n"
        "    timing: { policy: relative, after_step: nonexistent, offset_hours: 4 }\n"
        "    expected_cost_paise: 0\n"
    )
    _write(tmp_path, "dangling-ref.yaml", extended)
    with pytest.raises(PlaybookLoadError, match="after_step"):
        load_playbooks(tmp_path)


def test_load_playbooks_rejects_a_relative_step_referencing_a_later_step(tmp_path: Path) -> None:
    """`after_step` must name an *earlier* step -- a forward reference is
    just as invalid as a missing one, since the step it names has no
    `due_at` yet when this one would need it."""
    reordered = """\
id: test-playbook
version: 1
applies_to:
  root_cause: insufficient_funds
  leak_classes: [L1]
cost_ceiling_pct: 4.0
max_attempts: 3
max_case_age_days: 21
steps:
  - id: follow_up
    channel: link
    timing: { policy: relative, after_step: retry, offset_hours: 4 }
    expected_cost_paise: 0
  - id: retry
    channel: payment_retry
    timing: { policy: fixed, offset_hours: 6 }
    expected_cost_paise: 0
"""
    _write(tmp_path, "forward-ref.yaml", reordered)
    with pytest.raises(PlaybookLoadError, match="after_step"):
        load_playbooks(tmp_path)


def test_load_playbooks_rejects_a_relative_step_missing_after_step(tmp_path: Path) -> None:
    bad_timing = _VALID_YAML.replace(
        "timing: { policy: fixed, offset_hours: 6 }",
        "timing: { policy: relative, offset_hours: 6 }",
    )
    _write(tmp_path, "no-anchor.yaml", bad_timing)
    with pytest.raises(PlaybookLoadError):
        load_playbooks(tmp_path)


def test_load_playbooks_rejects_a_fixed_step_with_after_step(tmp_path: Path) -> None:
    bad_timing = _VALID_YAML.replace(
        "timing: { policy: fixed, offset_hours: 6 }",
        "timing: { policy: fixed, offset_hours: 6, after_step: retry }",
    )
    _write(tmp_path, "stray-anchor.yaml", bad_timing)
    with pytest.raises(PlaybookLoadError):
        load_playbooks(tmp_path)


def test_load_playbooks_rejects_duplicate_playbook_ids_across_files(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _VALID_YAML)
    _write(tmp_path, "b.yaml", _VALID_YAML)
    with pytest.raises(PlaybookLoadError, match="duplicate playbook id"):
        load_playbooks(tmp_path)
