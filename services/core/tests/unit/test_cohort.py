"""Seeded cohort generation (T3.1 checklist; PHASE-03-measurement.md).

The population the three-arm benchmark will run over -- built and tested
standalone, ahead of the runner (T3.5) that will actually replay it
through the pipeline.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from recoup.bench.cohort import generate_cohort, load_default_cohort_config
from recoup.domain.money import Currency
from recoup.domain.signal import LeakClass

_START = datetime(2026, 4, 10, 0, 0, tzinfo=UTC)
_CONFIG = load_default_cohort_config()


def test_same_seed_produces_an_identical_cohort() -> None:
    first = generate_cohort(_CONFIG, seed=42, size=200, start_at=_START)
    second = generate_cohort(_CONFIG, seed=42, size=200, start_at=_START)
    assert first == second


def test_different_seeds_produce_different_cohorts() -> None:
    first = generate_cohort(_CONFIG, seed=1, size=50, start_at=_START)
    second = generate_cohort(_CONFIG, seed=2, size=50, start_at=_START)
    assert first != second


def test_cohort_has_exactly_the_requested_size() -> None:
    cohort = generate_cohort(_CONFIG, seed=7, size=123, start_at=_START)
    assert cohort.size == 123
    assert len(cohort.cases) == 123
    assert [case.index for case in cohort.cases] == list(range(123))


def test_rejects_a_non_positive_size() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        generate_cohort(_CONFIG, seed=1, size=0, start_at=_START)


def test_leak_class_mix_matches_configuration_within_tolerance() -> None:
    cohort = generate_cohort(_CONFIG, seed=99, size=5000, start_at=_START)
    counts = Counter(case.ground_truth.leak_class for case in cohort.cases)
    for leak_class, expected_weight in _CONFIG.leak_class_mix.items():
        observed = counts[leak_class] / cohort.size
        assert abs(observed - expected_weight) < 0.03, (leak_class, observed, expected_weight)


def test_instrument_mix_matches_configuration_within_tolerance() -> None:
    cohort = generate_cohort(_CONFIG, seed=99, size=5000, start_at=_START)
    counts = Counter(case.instrument for case in cohort.cases)
    total_weight = sum(_CONFIG.instrument_mix.values())
    for instrument, weight in _CONFIG.instrument_mix.items():
        observed = counts[instrument] / cohort.size
        assert abs(observed - weight / total_weight) < 0.03, (instrument, observed)


def test_every_case_decline_category_is_valid_for_its_own_leak_class() -> None:
    cohort = generate_cohort(_CONFIG, seed=13, size=500, start_at=_START)
    for case in cohort.cases:
        leak_class = case.ground_truth.leak_class
        assert case.ground_truth.decline_category in _CONFIG.decline_category_mix[leak_class]


def test_amounts_are_heavy_tailed_and_never_below_the_floor() -> None:
    cohort = generate_cohort(_CONFIG, seed=21, size=2000, start_at=_START)
    amounts = [case.amount.paise for case in cohort.cases]
    assert all(paise >= _CONFIG.amount.min_paise for paise in amounts)
    assert all(case.amount.currency == Currency.INR for case in cohort.cases)
    # Heavy tail: some cases run well above the median, not just clustered near it.
    assert max(amounts) > _CONFIG.amount.median_paise * 5


def test_detected_at_falls_within_the_configured_window() -> None:
    cohort = generate_cohort(_CONFIG, seed=5, size=300, start_at=_START)
    window_end = _START + timedelta(hours=_CONFIG.detection_window_hours)
    for case in cohort.cases:
        assert _START <= case.detected_at < window_end


def test_would_recover_unaided_is_not_uniformly_one_value() -> None:
    cohort = generate_cohort(_CONFIG, seed=8, size=500, start_at=_START)
    flags = {case.ground_truth.would_recover_unaided for case in cohort.cases}
    assert flags == {True, False}


def test_customer_ids_are_unique_and_stable_across_runs() -> None:
    first = generate_cohort(_CONFIG, seed=3, size=10, start_at=_START)
    second = generate_cohort(_CONFIG, seed=3, size=10, start_at=_START)
    ids = [case.customer_id for case in first.cases]
    assert len(set(ids)) == len(ids)
    assert ids == [case.customer_id for case in second.cases]


def test_only_l1_l2_l3_leak_classes_are_ever_generated() -> None:
    cohort = generate_cohort(_CONFIG, seed=17, size=1000, start_at=_START)
    observed = {case.ground_truth.leak_class for case in cohort.cases}
    assert observed <= {
        LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
        LeakClass.L2_FAILED_MANDATE_DEBIT,
        LeakClass.L3_HALTED_SUBSCRIPTION,
    }
