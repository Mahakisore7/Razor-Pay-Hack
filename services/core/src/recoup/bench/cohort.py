"""Seeded cohort generator (T3.1; PHASE-03-measurement.md): the population
of at-risk cases the three-arm benchmark (T3.2-T3.8) will run.

Deliberately self-contained and independent of `gateway.simulator` --
PHASE-03's own ordering argument is that the harness is "built *before*
the features it will measure", so it must not inherit the simulator's
assumptions or its config. Every case carries its own answer key
(`CohortCase.ground_truth`): the leak class and decline category it was
generated to represent, and the "would recover unaided" counterfactual
T3.6's incremental-value statistics need. This is a *cohort-level*
ground truth, sampled at population time from `cohort.yaml`'s own
distributions -- a deliberately separate thing from `gateway.simulator.
ground_truth.GroundTruthRecord`, which the simulator records per payment
*attempt* once a future benchmark runner actually replays a case through
it.

Same seeded-hash technique as `gateway.simulator.world` (SHA-256 of
`(seed, ...parts)`, never a sequentially-mutating RNG, deliberately
duplicated here rather than imported -- see the module docstring above
on why this stays independent of the simulator): every case's attributes
are a pure function of `(seed, index)`, independent of generation order
and of any other case in the cohort. That is what makes "two calls at
the same seed produce a byte-identical cohort" (A3.2's phase gate) hold
regardless of how the population loop happens to be structured.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml
from scipy.stats import beta as beta_distribution

from recoup.domain.decline import DeclineCategory
from recoup.domain.money import Money
from recoup.domain.signal import LeakClass

__all__ = [
    "AmountDistributionConfig",
    "Cohort",
    "CohortCase",
    "CohortConfig",
    "CohortGroundTruth",
    "CustomerPropensityConfig",
    "generate_cohort",
    "load_default_cohort_config",
    "parse_cohort_config",
]

_COHORT_LEAK_CLASSES = frozenset(
    {
        LeakClass.L1_FAILED_ONE_TIME_PAYMENT,
        LeakClass.L2_FAILED_MANDATE_DEBIT,
        LeakClass.L3_HALTED_SUBSCRIPTION,
    }
)


@dataclass(frozen=True, slots=True)
class AmountDistributionConfig:
    """Log-normal: `median_paise * exp(sigma * z)`, floored at `min_paise`.

    Heavy-tailed by construction (T3.1 checklist) -- a normal distribution
    would under-represent the rare, large-amount cases that dominate the
    amount-weighted recovery rate T3.6 reports.
    """

    min_paise: int
    median_paise: int
    sigma: float


@dataclass(frozen=True, slots=True)
class CustomerPropensityConfig:
    alpha: float
    beta: float


@dataclass(frozen=True, slots=True)
class CohortConfig:
    leak_class_mix: Mapping[LeakClass, float]
    instrument_mix: Mapping[str, float]
    issuer_mix: Mapping[str, float]
    decline_category_mix: Mapping[LeakClass, Mapping[DeclineCategory, float]]
    amount: AmountDistributionConfig
    customer_propensity: CustomerPropensityConfig
    detection_window_hours: float


@dataclass(frozen=True, slots=True)
class CohortGroundTruth:
    """The cohort's own answer key for one case -- what it was generated
    to represent, for T3.6's diagnosis-accuracy and incremental-value
    scoring to grade against."""

    leak_class: LeakClass
    decline_category: DeclineCategory
    would_recover_unaided: bool


@dataclass(frozen=True, slots=True)
class CohortCase:
    index: int
    customer_id: str
    razorpay_customer_id: str
    amount: Money
    instrument: str
    issuer: str
    detected_at: datetime
    ground_truth: CohortGroundTruth


@dataclass(frozen=True, slots=True)
class Cohort:
    seed: int
    size: int
    cases: tuple[CohortCase, ...]


def _uniform(*parts: object) -> float:
    """A deterministic pseudo-random float in [0, 1), derived from `parts`.

    Identical technique to `gateway.simulator.world._uniform` -- see the
    module docstring for why this is a deliberate duplication rather than
    a shared import.
    """
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _weighted_choice[K](mix: Mapping[K, float], roll: float) -> K:
    total = sum(mix.values())
    threshold = roll * total
    cumulative = 0.0
    ordered = sorted(mix, key=str)
    for key in ordered:
        cumulative += mix[key]
        if threshold < cumulative:
            return key
    # Unreachable in practice -- see world._pick_failure_category's
    # identical note: `roll` is strictly < 1.0, so `threshold` is
    # strictly less than the final cumulative total and the loop above
    # always returns first. Kept explicit rather than assumed.
    return ordered[-1]  # pragma: no cover


def _sample_amount(config: AmountDistributionConfig, *, seed: int, index: int) -> Money:
    z = statistics.NormalDist().inv_cdf(_uniform(seed, "amount", index))
    paise = round(config.median_paise * math.exp(config.sigma * z))
    return Money(max(paise, config.min_paise))


def _sample_propensity(config: CustomerPropensityConfig, *, seed: int, index: int) -> float:
    u = _uniform(seed, "propensity", index)
    return float(beta_distribution.ppf(u, config.alpha, config.beta))


def _build_case(config: CohortConfig, *, seed: int, index: int, start_at: datetime) -> CohortCase:
    customer_id = f"bench_cust_{seed}_{index}"
    leak_class = _weighted_choice(config.leak_class_mix, _uniform(seed, "leak_class", index))
    instrument = _weighted_choice(config.instrument_mix, _uniform(seed, "instrument", index))
    issuer = _weighted_choice(config.issuer_mix, _uniform(seed, "issuer", index))
    decline_category = _weighted_choice(
        config.decline_category_mix[leak_class], _uniform(seed, "decline_category", index)
    )
    propensity = _sample_propensity(config.customer_propensity, seed=seed, index=index)
    would_recover_unaided = _uniform(seed, "recovers_unaided", index) < propensity
    offset_hours = _uniform(seed, "detected_at", index) * config.detection_window_hours

    return CohortCase(
        index=index,
        customer_id=customer_id,
        razorpay_customer_id=customer_id,
        amount=_sample_amount(config.amount, seed=seed, index=index),
        instrument=instrument,
        issuer=issuer,
        detected_at=start_at + timedelta(hours=offset_hours),
        ground_truth=CohortGroundTruth(
            leak_class=leak_class,
            decline_category=decline_category,
            would_recover_unaided=would_recover_unaided,
        ),
    )


def generate_cohort(config: CohortConfig, *, seed: int, size: int, start_at: datetime) -> Cohort:
    if size <= 0:
        raise ValueError(f"cohort size must be positive, got {size}")
    cases = tuple(_build_case(config, seed=seed, index=i, start_at=start_at) for i in range(size))
    return Cohort(seed=seed, size=size, cases=cases)


def parse_cohort_config(raw_yaml: str) -> CohortConfig:
    """Parse and validate a cohort-config YAML document.

    Separated from `load_default_cohort_config` so validation is directly
    testable without touching the filesystem (mirrors
    `gateway.simulator.config.parse_simulator_config`).
    """
    doc = yaml.safe_load(raw_yaml)
    if not isinstance(doc, dict):
        raise ValueError("cohort config must be a YAML mapping")

    try:
        leak_class_mix = {
            LeakClass(name): float(weight) for name, weight in doc["leak_class_mix"].items()
        }
        _validate_mix(leak_class_mix, "leak_class_mix")
        unknown = set(leak_class_mix) - _COHORT_LEAK_CLASSES
        if unknown:
            raise ValueError(f"leak_class_mix contains non-cohort leak classes: {sorted(unknown)}")

        instrument_mix = {name: float(weight) for name, weight in doc["instrument_mix"].items()}
        _validate_mix(instrument_mix, "instrument_mix")

        issuer_mix = {name: float(weight) for name, weight in doc["issuer_mix"].items()}
        _validate_mix(issuer_mix, "issuer_mix")

        raw_decline_mix = doc["decline_category_mix"]
        if set(raw_decline_mix) != {lc.value for lc in leak_class_mix}:
            raise ValueError(
                "decline_category_mix must have exactly one entry per leak_class_mix key, "
                f"got {sorted(raw_decline_mix)} vs {sorted(lc.value for lc in leak_class_mix)}"
            )
        decline_category_mix = {}
        for leak_name, raw_mix in raw_decline_mix.items():
            per_class_mix = {
                DeclineCategory(name): float(weight) for name, weight in raw_mix.items()
            }
            _validate_mix(per_class_mix, f"decline_category_mix[{leak_name}]")
            decline_category_mix[LeakClass(leak_name)] = per_class_mix

        amount_doc = doc["amount"]
        amount = AmountDistributionConfig(
            min_paise=int(amount_doc["min_paise"]),
            median_paise=int(amount_doc["median_paise"]),
            sigma=float(amount_doc["sigma"]),
        )
        if amount.min_paise <= 0:
            raise ValueError("amount.min_paise must be positive")
        if amount.median_paise < amount.min_paise:
            raise ValueError("amount.median_paise must be >= amount.min_paise")
        if amount.sigma <= 0:
            raise ValueError("amount.sigma must be positive")

        propensity_doc = doc["customer_propensity"]
        customer_propensity = CustomerPropensityConfig(
            alpha=float(propensity_doc["alpha"]), beta=float(propensity_doc["beta"])
        )
        if customer_propensity.alpha <= 0 or customer_propensity.beta <= 0:
            raise ValueError("customer_propensity.alpha and .beta must both be positive")

        detection_window_hours = float(doc["detection_window_hours"])
        if detection_window_hours <= 0:
            raise ValueError("detection_window_hours must be positive")

        config = CohortConfig(
            leak_class_mix=leak_class_mix,
            instrument_mix=instrument_mix,
            issuer_mix=issuer_mix,
            decline_category_mix=decline_category_mix,
            amount=amount,
            customer_propensity=customer_propensity,
            detection_window_hours=detection_window_hours,
        )
    except KeyError as exc:
        raise ValueError(f"cohort config is missing required key {exc}") from exc

    return config


def _validate_mix(mix: Mapping[Any, float], name: str) -> None:
    if not mix:
        raise ValueError(f"{name} must not be empty")
    if any(weight < 0 for weight in mix.values()):
        raise ValueError(f"{name} weights must be non-negative")
    if sum(mix.values()) <= 0:
        raise ValueError(f"{name} weights must sum to more than zero")


@lru_cache
def load_default_cohort_config() -> CohortConfig:
    """Parse and validate the bundled config. Cached: read once, not per call."""
    raw = resources.files("recoup.bench").joinpath("cohort.yaml").read_text("utf-8")
    return parse_cohort_config(raw)
