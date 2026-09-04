"""Loads the benchmark's baseline-arm playbook (T3.3; PHASE-03-
measurement.md): a naive, diagnosis-blind recovery flow -- three fixed
retries and one generic dunning email, the same for every case
regardless of decline category or leak class. "Deliberately competent
but naive" (PHASE-03's own words): what a good engineer builds in a
weekend, and the comparison that actually matters against the
diagnosis-driven treatment arm.

Loaded from its own bundled file, not `planning.playbooks.loader`'s
directory scan: `select_playbook` (T2.4) matches a playbook by
`(root_cause, leak_class)`, and this playbook is never meant to be
reachable through that path -- a baseline-arm case gets it directly,
bypassing diagnosis-driven routing entirely (T3.3's own checklist: "no
diagnosis-driven routing"). Keeping it out of `planning/playbooks/`
means it can never accidentally compete with, or be shadowed by, a real
diagnosis-routed playbook there.

Once loaded, it is an ordinary `Playbook` -- `planning.planner.build_plan`
and `planning.repository.persist_plan` need no baseline-specific code
path at all; a baseline-arm case reaches `EXECUTING` and gets real,
claimable `ScheduledActionRow`s exactly like a treatment-arm case does,
just against this playbook instead of a diagnosis-selected one.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

import yaml
from pydantic import ValidationError

from recoup.planning.playbooks.loader import PlaybookLoadError
from recoup.planning.playbooks.schema import Playbook

__all__ = ["load_baseline_playbook", "parse_baseline_playbook"]


def parse_baseline_playbook(raw_yaml: str) -> Playbook:
    """Parse and validate a baseline-playbook YAML document. Separated
    from `load_baseline_playbook` so a malformed document is directly
    testable without touching the filesystem -- mirrors
    `gateway.simulator.config.parse_simulator_config` and
    `bench.cohort.parse_cohort_config`'s own split.
    """
    try:
        return Playbook.model_validate(yaml.safe_load(raw_yaml))
    except (yaml.YAMLError, ValidationError) as exc:
        raise PlaybookLoadError(f"baseline playbook: {exc}") from exc


@lru_cache
def load_baseline_playbook() -> Playbook:
    """Parse and validate the bundled baseline playbook. Cached: read
    once, not per call -- matches `load_default_simulator_config` and
    `load_default_cohort_config`'s own convention.
    """
    raw = resources.files("recoup.bench").joinpath("baseline_playbook.yaml").read_text("utf-8")
    return parse_baseline_playbook(raw)
