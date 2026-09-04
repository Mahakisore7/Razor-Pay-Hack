"""Every cohort-generation parameter is externalised YAML, loaded and
validated (T3.1 checklist; PHASE-03-measurement.md)."""

import pytest

from recoup.bench.cohort import load_default_cohort_config, parse_cohort_config
from recoup.domain.decline import DeclineCategory
from recoup.domain.signal import LeakClass

_MINIMAL_VALID = """
leak_class_mix: {L1: 0.6, L2: 0.4}
instrument_mix: {upi: 1.0}
issuer_mix: {HDFC: 1.0}
decline_category_mix:
  L1: {insufficient_funds: 1.0}
  L2: {mandate_exhausted: 1.0}
amount: {min_paise: 100, median_paise: 1000, sigma: 0.5}
customer_propensity: {alpha: 2.0, beta: 3.0}
detection_window_hours: 24.0
"""


def test_bundled_config_loads_and_validates() -> None:
    config = load_default_cohort_config()
    assert set(config.leak_class_mix) == {
        LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
        LeakClass.L2_FAILED_MANDATE_DEBIT,
        LeakClass.L3_HALTED_SUBSCRIPTION,
    }
    assert set(config.decline_category_mix) == set(config.leak_class_mix)


def test_minimal_valid_document_parses() -> None:
    config = parse_cohort_config(_MINIMAL_VALID)
    assert config.leak_class_mix[LeakClass.L1_FAILED_ONE_TIME_PAYMENT] == 0.6
    assert (
        config.decline_category_mix[LeakClass.L2_FAILED_MANDATE_DEBIT][
            DeclineCategory.MANDATE_EXHAUSTED
        ]
        == 1.0
    )


def test_parse_rejects_non_mapping_document() -> None:
    with pytest.raises(ValueError, match="mapping"):
        parse_cohort_config("- just\n- a\n- list\n")


def test_parse_rejects_missing_required_key() -> None:
    with pytest.raises(ValueError, match="missing required key"):
        parse_cohort_config("leak_class_mix: {L1: 1.0}\n")


def test_parse_rejects_empty_mix() -> None:
    broken = _MINIMAL_VALID.replace("instrument_mix: {upi: 1.0}", "instrument_mix: {}")
    with pytest.raises(ValueError, match="instrument_mix must not be empty"):
        parse_cohort_config(broken)


def test_parse_rejects_negative_weight() -> None:
    broken = _MINIMAL_VALID.replace("issuer_mix: {HDFC: 1.0}", "issuer_mix: {HDFC: -1.0}")
    with pytest.raises(ValueError, match="non-negative"):
        parse_cohort_config(broken)


def test_parse_rejects_a_mix_of_all_zero_weights() -> None:
    broken = _MINIMAL_VALID.replace("issuer_mix: {HDFC: 1.0}", "issuer_mix: {HDFC: 0.0}")
    with pytest.raises(ValueError, match="sum to more than zero"):
        parse_cohort_config(broken)


def test_parse_rejects_non_positive_amount_floor() -> None:
    broken = _MINIMAL_VALID.replace(
        "amount: {min_paise: 100, median_paise: 1000, sigma: 0.5}",
        "amount: {min_paise: 0, median_paise: 1000, sigma: 0.5}",
    )
    with pytest.raises(ValueError, match="min_paise must be positive"):
        parse_cohort_config(broken)


def test_parse_rejects_leak_class_outside_l1_l3() -> None:
    broken = _MINIMAL_VALID.replace(
        "leak_class_mix: {L1: 0.6, L2: 0.4}", "leak_class_mix: {L1: 0.6, L4: 0.4}"
    )
    with pytest.raises(ValueError, match="non-cohort leak classes"):
        parse_cohort_config(broken)


def test_parse_rejects_decline_category_mix_mismatched_with_leak_class_mix() -> None:
    broken = _MINIMAL_VALID.replace(
        "decline_category_mix:\n  L1: {insufficient_funds: 1.0}\n  L2: {mandate_exhausted: 1.0}\n",
        "decline_category_mix:\n  L1: {insufficient_funds: 1.0}\n",
    )
    with pytest.raises(ValueError, match="exactly one entry per leak_class_mix key"):
        parse_cohort_config(broken)


def test_parse_rejects_median_below_floor() -> None:
    broken = _MINIMAL_VALID.replace(
        "amount: {min_paise: 100, median_paise: 1000, sigma: 0.5}",
        "amount: {min_paise: 1000, median_paise: 100, sigma: 0.5}",
    )
    with pytest.raises(ValueError, match="median_paise must be >= "):
        parse_cohort_config(broken)


def test_parse_rejects_non_positive_sigma() -> None:
    broken = _MINIMAL_VALID.replace(
        "amount: {min_paise: 100, median_paise: 1000, sigma: 0.5}",
        "amount: {min_paise: 100, median_paise: 1000, sigma: 0.0}",
    )
    with pytest.raises(ValueError, match="sigma must be positive"):
        parse_cohort_config(broken)


def test_parse_rejects_non_positive_propensity_params() -> None:
    broken = _MINIMAL_VALID.replace(
        "customer_propensity: {alpha: 2.0, beta: 3.0}",
        "customer_propensity: {alpha: 0.0, beta: 3.0}",
    )
    with pytest.raises(ValueError, match="must both be positive"):
        parse_cohort_config(broken)


def test_parse_rejects_non_positive_detection_window() -> None:
    broken = _MINIMAL_VALID.replace("detection_window_hours: 24.0", "detection_window_hours: 0.0")
    with pytest.raises(ValueError, match="detection_window_hours must be positive"):
        parse_cohort_config(broken)
