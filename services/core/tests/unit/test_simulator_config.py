"""Every simulator parameter is externalised YAML, loaded and validated
(T1.9 checklist; RAZORPAY-INTEGRATION SS6)."""

import pytest

from recoup.gateway.simulator.config import load_default_simulator_config, parse_simulator_config

_MINIMAL_VALID = """
instruments:
  upi:
    base_success_rate: 0.9
    failure_mix: {insufficient_funds: 1.0}
issuer_outages:
  {daily_start_probability: 0.01, duration_hours_min: 1.0, duration_hours_max: 2.0, severity: 0.1}
salary_cycle: {pre_payday_days: [28, 29, 30, 31, 1], insufficient_funds_uplift: 2.0}
diurnal:
  hourly_multiplier: [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
                       1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
mandate_budgets: {default_representation_cap: 3}
customer_propensity: {alpha: 2.0, beta: 3.0}
intervention_response: {click_through_rate: {sms: 0.3}, conversion_given_click: 0.5}
network_faults: {base_rate: 0.01}
"""


def test_bundled_config_loads_and_validates() -> None:
    config = load_default_simulator_config()
    assert "upi" in config.instruments
    assert len(config.diurnal.hourly_multiplier) == 24


def test_minimal_valid_document_parses() -> None:
    config = parse_simulator_config(_MINIMAL_VALID)
    assert config.instruments["upi"].base_success_rate == 0.9
    assert config.salary_cycle.pre_payday_days == frozenset({28, 29, 30, 31, 1})


def test_parse_rejects_non_mapping_document() -> None:
    with pytest.raises(ValueError, match="mapping"):
        parse_simulator_config("- just\n- a\n- list\n")


def test_parse_rejects_missing_required_key() -> None:
    with pytest.raises(ValueError, match="missing required key"):
        parse_simulator_config("instruments: {}\n")


def test_parse_rejects_empty_failure_mix() -> None:
    broken = _MINIMAL_VALID.replace("failure_mix: {insufficient_funds: 1.0}", "failure_mix: {}")
    with pytest.raises(ValueError, match="empty failure_mix"):
        parse_simulator_config(broken)


def test_parse_rejects_wrong_length_diurnal_curve() -> None:
    broken = _MINIMAL_VALID.replace(
        "hourly_multiplier: [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,\n"
        "                       1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]",
        "hourly_multiplier: [1.0, 1.0, 1.0]",
    )
    with pytest.raises(ValueError, match="exactly 24 entries"):
        parse_simulator_config(broken)
