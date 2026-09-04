"""Every simulator parameter, externalised (T1.9 checklist; RAZORPAY-
INTEGRATION SS6, ADR-0004): published in the benchmark report so a
reviewer can read exactly what world produced the numbers, and changed
without touching code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

import yaml

__all__ = [
    "CustomerPropensityConfig",
    "DiurnalConfig",
    "InstrumentConfig",
    "InterventionResponseConfig",
    "IssuerOutageConfig",
    "MandateBudgetConfig",
    "NetworkFaultConfig",
    "SalaryCycleConfig",
    "SimulatorConfig",
    "load_default_simulator_config",
    "parse_simulator_config",
]


@dataclass(frozen=True, slots=True)
class InstrumentConfig:
    base_success_rate: float
    failure_mix: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class IssuerOutageConfig:
    daily_start_probability: float
    duration_hours_min: float
    duration_hours_max: float
    severity: float


@dataclass(frozen=True, slots=True)
class SalaryCycleConfig:
    pre_payday_days: frozenset[int]
    insufficient_funds_uplift: float


@dataclass(frozen=True, slots=True)
class DiurnalConfig:
    hourly_multiplier: tuple[float, ...]  # length 24, index = hour of day (UTC)


@dataclass(frozen=True, slots=True)
class MandateBudgetConfig:
    default_representation_cap: int


@dataclass(frozen=True, slots=True)
class CustomerPropensityConfig:
    alpha: float
    beta: float


@dataclass(frozen=True, slots=True)
class InterventionResponseConfig:
    click_through_rate: Mapping[str, float]
    conversion_given_click: float


@dataclass(frozen=True, slots=True)
class NetworkFaultConfig:
    base_rate: float


@dataclass(frozen=True, slots=True)
class SimulatorConfig:
    instruments: Mapping[str, InstrumentConfig]
    issuer_outages: IssuerOutageConfig
    salary_cycle: SalaryCycleConfig
    diurnal: DiurnalConfig
    mandate_budgets: MandateBudgetConfig
    customer_propensity: CustomerPropensityConfig
    intervention_response: InterventionResponseConfig
    network_faults: NetworkFaultConfig


def parse_simulator_config(raw_yaml: str) -> SimulatorConfig:
    """Parse and lightly validate a simulator-config YAML document.

    Separated from `load_default_simulator_config` so validation is
    directly testable without touching the filesystem.
    """
    doc = yaml.safe_load(raw_yaml)
    if not isinstance(doc, dict):
        raise ValueError("simulator config must be a YAML mapping")

    try:
        instruments = {
            name: InstrumentConfig(
                base_success_rate=cfg["base_success_rate"], failure_mix=dict(cfg["failure_mix"])
            )
            for name, cfg in doc["instruments"].items()
        }
        for name, instrument in instruments.items():
            if not instrument.failure_mix:
                raise ValueError(f"instrument {name!r} has an empty failure_mix")

        diurnal_values = tuple(float(v) for v in doc["diurnal"]["hourly_multiplier"])
        if len(diurnal_values) != 24:
            raise ValueError(
                f"diurnal.hourly_multiplier must have exactly 24 entries, got {len(diurnal_values)}"
            )

        config = SimulatorConfig(
            instruments=instruments,
            issuer_outages=IssuerOutageConfig(**doc["issuer_outages"]),
            salary_cycle=SalaryCycleConfig(
                pre_payday_days=frozenset(doc["salary_cycle"]["pre_payday_days"]),
                insufficient_funds_uplift=doc["salary_cycle"]["insufficient_funds_uplift"],
            ),
            diurnal=DiurnalConfig(hourly_multiplier=diurnal_values),
            mandate_budgets=MandateBudgetConfig(**doc["mandate_budgets"]),
            customer_propensity=CustomerPropensityConfig(**doc["customer_propensity"]),
            intervention_response=InterventionResponseConfig(
                click_through_rate=dict(doc["intervention_response"]["click_through_rate"]),
                conversion_given_click=doc["intervention_response"]["conversion_given_click"],
            ),
            network_faults=NetworkFaultConfig(**doc["network_faults"]),
        )
    except KeyError as exc:
        raise ValueError(f"simulator config is missing required key {exc}") from exc

    return config


@lru_cache
def load_default_simulator_config() -> SimulatorConfig:
    """Parse and validate the bundled config. Cached: read once, not per call."""
    raw = resources.files("recoup.gateway.simulator").joinpath("simulator.yaml").read_text("utf-8")
    return parse_simulator_config(raw)
