"""The benchmark's baseline-arm playbook (T3.3 checklist; PHASE-03-
measurement.md): naive, fixed-schedule, diagnosis-blind."""

import pytest

from recoup.bench.baseline import load_baseline_playbook, parse_baseline_playbook
from recoup.domain.action import Channel
from recoup.domain.signal import LeakClass
from recoup.planning.playbooks.loader import PlaybookLoadError, load_playbooks


def test_bundled_baseline_playbook_loads_and_validates() -> None:
    playbook = load_baseline_playbook()
    assert playbook.id == "baseline-naive"


def test_baseline_playbook_is_three_retries_and_one_generic_email() -> None:
    playbook = load_baseline_playbook()
    channels = [step.channel for step in playbook.steps]
    assert channels.count(Channel.PAYMENT_RETRY) == 3
    assert channels.count(Channel.EMAIL) == 1
    assert len(playbook.steps) == 4


def test_baseline_playbook_retries_are_fixed_at_1_24_and_72_hours() -> None:
    playbook = load_baseline_playbook()
    retry_offsets = sorted(
        step.timing.offset_hours for step in playbook.steps if step.channel == Channel.PAYMENT_RETRY
    )
    assert retry_offsets == [1.0, 24.0, 72.0]
    assert all(
        step.timing.policy == "fixed"
        for step in playbook.steps
        if step.channel == Channel.PAYMENT_RETRY
    )


def test_baseline_playbook_applies_to_every_cohort_leak_class() -> None:
    playbook = load_baseline_playbook()
    assert set(playbook.applies_to.leak_classes) == {
        LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
        LeakClass.L2_FAILED_MANDATE_DEBIT,
        LeakClass.L3_HALTED_SUBSCRIPTION,
    }


def test_baseline_playbook_is_never_selected_by_diagnosis_driven_routing() -> None:
    """T3.3: "no diagnosis-driven routing" -- `select_playbook` (T2.4)
    matches by `(root_cause, leak_class)`, so the baseline playbook must
    never be among the playbooks that routing considers; it is loaded
    directly by `recoup.bench.baseline`, never through
    `planning.playbooks.loader.load_playbooks`'s directory scan.
    """
    routed_ids = set(load_playbooks())
    assert load_baseline_playbook().id not in routed_ids


def test_parse_baseline_playbook_rejects_malformed_yaml() -> None:
    with pytest.raises(PlaybookLoadError):
        parse_baseline_playbook("not: [valid, yaml, :::")


def test_parse_baseline_playbook_rejects_a_schema_violation() -> None:
    with pytest.raises(PlaybookLoadError):
        parse_baseline_playbook("id: broken\nversion: 1\n")  # missing required fields


def test_baseline_playbook_costs_stay_well_under_its_own_ceiling() -> None:
    """T3.3: "no cost ceiling" in spirit -- the ceiling is set to the
    schema's own maximum precisely so it never actually binds against
    this playbook's small, fixed step costs for any realistic at_risk
    amount."""
    playbook = load_baseline_playbook()
    total_cost = sum(step.expected_cost_paise for step in playbook.steps)
    # A ceiling that would only start dropping steps below an
    # unrealistically tiny at_risk amount -- e.g. below INR 2 at 10%.
    assert total_cost <= 20
    assert playbook.cost_ceiling_pct == 10.0
