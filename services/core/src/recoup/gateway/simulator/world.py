"""The stochastic model behind `RazorpaySimulator` (RAZORPAY-INTEGRATION SS6,
ADR-0004): not a mock, a model of the phenomena the diagnosis engine and
timing bandit claim to exploit. A simulator returning uniform random
failures would let those components "detect an issuer outage" that was
never simulated, and the benchmark would measure nothing.

Every stochastic decision is derived by hashing `(seed, ...identifying
parts)` rather than drawn from a sequentially-mutating RNG. That makes each
decision referentially transparent -- independent of call order -- which is
what makes "two independent runs at the same seed are byte-identical"
(A1.7, the phase gate) safe against any difference in how the two runs
happen to interleave their calls, not just a property that holds if nobody
ever calls things in a different order.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from scipy.stats import beta as beta_distribution

from recoup.domain.decline import DeclineCategory
from recoup.gateway.simulator.config import SimulatorConfig

__all__ = ["AttemptOutcome", "World"]


def _uniform(*parts: object) -> float:
    """A deterministic pseudo-random float in [0, 1), derived from `parts`."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    success: bool
    decline_category: DeclineCategory | None
    true_cause: str


class World:
    def __init__(self, config: SimulatorConfig, seed: int) -> None:
        self._config = config
        self._seed = seed

    def attempt_outcome(
        self, *, customer_id: str, instrument: str, issuer: str, at: datetime, attempt_no: int = 1
    ) -> AttemptOutcome:
        instrument_cfg = self._config.instruments[instrument]
        success_rate = (
            instrument_cfg.base_success_rate * self._config.diurnal.hourly_multiplier[at.hour]
        )

        issuer_down = self._is_issuer_down(issuer, at)
        if issuer_down:
            success_rate = self._config.issuer_outages.severity

        network_roll = _uniform(
            self._seed, "network_fault", customer_id, issuer, at.isoformat(), attempt_no
        )
        if network_roll < self._config.network_faults.base_rate:
            return AttemptOutcome(False, DeclineCategory.NETWORK_TIMEOUT, "network_fault")

        success_roll = _uniform(
            self._seed, "success", customer_id, instrument, issuer, at.isoformat(), attempt_no
        )
        if success_roll < success_rate:
            return AttemptOutcome(True, None, "success")

        if issuer_down:
            return AttemptOutcome(False, DeclineCategory.ISSUER_DOWN, f"issuer_outage:{issuer}")

        pre_payday = at.day in self._config.salary_cycle.pre_payday_days
        category = self._pick_failure_category(
            instrument_cfg.failure_mix,
            pre_payday=pre_payday,
            customer_id=customer_id,
            at=at,
            attempt_no=attempt_no,
        )
        cause = (
            "salary_cycle"
            if pre_payday and category == DeclineCategory.INSUFFICIENT_FUNDS
            else "instrument_baseline"
        )
        return AttemptOutcome(False, category, cause)

    def customer_propensity(self, customer_id: str) -> float:
        """A latent, per-customer willingness to pay -- sampled once (via
        hashing, so it's stable across calls) from Beta(alpha, beta)."""
        u = _uniform(self._seed, "propensity", customer_id)
        cfg = self._config.customer_propensity
        return float(beta_distribution.ppf(u, cfg.alpha, cfg.beta))

    def would_recover_unaided(self, customer_id: str) -> bool:
        """Whether this customer would eventually pay with no intervention
        at all -- the control-arm counterfactual (DOMAIN-MODEL SS3)."""
        propensity = self.customer_propensity(customer_id)
        roll = _uniform(self._seed, "recovers_unaided", customer_id)
        return roll < propensity

    def click_through(self, *, customer_id: str, channel: str, message_id: str) -> bool:
        rate = self._config.intervention_response.click_through_rate.get(channel, 0.0)
        roll = _uniform(self._seed, "click_through", customer_id, channel, message_id)
        return roll < rate

    def converts_given_click(self, *, customer_id: str, message_id: str) -> bool:
        propensity = self.customer_propensity(customer_id)
        base = self._config.intervention_response.conversion_given_click
        probability = min(1.0, base * (0.5 + propensity))
        roll = _uniform(self._seed, "converts", customer_id, message_id)
        return roll < probability

    def _is_issuer_down(self, issuer: str, at: datetime) -> bool:
        """A correlated failure burst: once an outage starts on some day, it
        covers every attempt to that issuer for its sampled duration, not
        just an independent per-attempt coin flip -- that correlation is
        what L6 detection and hypothesis ranking exist to find.
        """
        cfg = self._config.issuer_outages
        max_days_back = math.ceil(cfg.duration_hours_max / 24) + 1
        for days_back in range(max_days_back + 1):
            day = (at - timedelta(days=days_back)).date()
            start_roll = _uniform(self._seed, "outage_starts", issuer, day.isoformat())
            if start_roll >= cfg.daily_start_probability:
                continue
            start_hour_roll = _uniform(self._seed, "outage_start_hour", issuer, day.isoformat())
            start_dt = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(
                hours=start_hour_roll * 24
            )
            duration_roll = _uniform(self._seed, "outage_duration", issuer, day.isoformat())
            duration_hours = cfg.duration_hours_min + duration_roll * (
                cfg.duration_hours_max - cfg.duration_hours_min
            )
            end_dt = start_dt + timedelta(hours=duration_hours)
            if start_dt <= at < end_dt:
                return True
        return False

    def _pick_failure_category(
        self,
        failure_mix: Mapping[str, float],
        *,
        pre_payday: bool,
        customer_id: str,
        at: datetime,
        attempt_no: int,
    ) -> DeclineCategory:
        weights = dict(failure_mix)
        if pre_payday and "insufficient_funds" in weights:
            weights["insufficient_funds"] *= self._config.salary_cycle.insufficient_funds_uplift
        total = sum(weights.values())
        roll = _uniform(self._seed, "failure_category", customer_id, at.isoformat(), attempt_no)
        threshold = roll * total
        cumulative = 0.0
        for name, weight in weights.items():
            cumulative += weight
            if threshold < cumulative:
                return DeclineCategory(name)
        # Unreachable: `_uniform` is strictly < 1.0 (a 64-bit numerator over
        # a 2**64 denominator), so `threshold < total = final cumulative`
        # always holds and the loop above always returns first. Kept as an
        # explicit total rather than an assumed-exhaustive loop, in case
        # that invariant ever changes.
        return DeclineCategory(next(reversed(weights)))  # pragma: no cover
