"""World models the phenomena the diagnosis engine and timing bandit claim
to exploit (RAZORPAY-INTEGRATION SS6, ADR-0004) -- not uniform random
failure. Each test proves one modelled phenomenon actually moves the
numbers the way it should, using the real bundled config.
"""

from datetime import UTC, datetime, timedelta

from recoup.domain.decline import DeclineCategory
from recoup.gateway.simulator.config import load_default_simulator_config
from recoup.gateway.simulator.world import World

_CONFIG = load_default_simulator_config()


def _world(seed: int = 1) -> World:
    return World(config=_CONFIG, seed=seed)


# --- Determinism of a single call --------------------------------------------


def test_attempt_outcome_is_deterministic_for_identical_inputs() -> None:
    world = _world()
    at = datetime(2026, 3, 15, 10, 0, tzinfo=UTC)
    first = world.attempt_outcome(customer_id="c1", instrument="upi", issuer="HDFC", at=at)
    second = world.attempt_outcome(customer_id="c1", instrument="upi", issuer="HDFC", at=at)
    assert first == second


def test_attempt_outcome_differs_across_customers() -> None:
    world = _world()
    at = datetime(2026, 3, 15, 10, 0, tzinfo=UTC)
    outcomes = {
        world.attempt_outcome(customer_id=f"c{i}", instrument="upi", issuer="HDFC", at=at)
        for i in range(50)
    }
    assert len(outcomes) > 1  # not every customer gets the same outcome


# --- Issuer outages: correlated bursts, not independent coin flips ----------


def test_issuer_outage_fires_over_a_long_enough_window() -> None:
    """Sampled at `daily_start_probability` per issuer per day, so a single
    fixed issuer probed across many days should see at least one outage."""
    world = _world(seed=7)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    down_hits = sum(
        1
        for day in range(180)
        for hour in (2, 8, 14, 20)
        if world.attempt_outcome(
            customer_id="probe",
            instrument="upi",
            issuer="OUTAGE_PROBE_ISSUER",
            at=base + timedelta(days=day, hours=hour),
        ).decline_category
        == DeclineCategory.ISSUER_DOWN
    )
    assert down_hits > 0


def test_issuer_outage_is_correlated_within_its_window() -> None:
    """Once one attempt to an issuer at a given hour is hit by the outage
    model, immediately adjacent hours to the SAME issuer should mostly be
    hit too -- a burst, not independent per-attempt noise."""
    world = _world(seed=7)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for day in range(180):
        at = base + timedelta(days=day, hours=10)
        if world._is_issuer_down("BURST_PROBE_ISSUER", at):
            neighbours_down = sum(
                1
                for offset_hours in (-1, 1)
                if world._is_issuer_down("BURST_PROBE_ISSUER", at + timedelta(hours=offset_hours))
            )
            assert neighbours_down > 0
            return
    raise AssertionError("no outage window found to probe in 180 days -- widen the search")


def test_issuer_outages_are_independent_per_issuer() -> None:
    """Different issuers should not all be down (or up) at the same time
    for the same reason -- the outage roll is keyed by issuer, not global."""
    world = _world(seed=3)
    at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    statuses = {f"ISSUER_{i}": world._is_issuer_down(f"ISSUER_{i}", at) for i in range(30)}
    assert not all(statuses.values())  # not every issuer coincidentally down


# --- Salary-cycle effect on insufficient_funds -------------------------------


def test_insufficient_funds_is_more_common_pre_payday() -> None:
    world = _world(seed=11)

    def _insufficient_funds_count(day: int, label: str) -> int:
        count = 0
        for i in range(300):
            at = datetime(2026, 2, day, i % 24, tzinfo=UTC) + timedelta(minutes=i)
            outcome = world.attempt_outcome(
                customer_id=f"{label}_{i}", instrument="card", issuer="STABLE_ISSUER", at=at
            )
            if outcome.decline_category == DeclineCategory.INSUFFICIENT_FUNDS:
                count += 1
        return count

    pre_payday_count = _insufficient_funds_count(27, "pre")
    mid_month_count = _insufficient_funds_count(10, "mid")
    assert pre_payday_count > mid_month_count


# --- Diurnal variation --------------------------------------------------------


def test_diurnal_curve_is_read_from_config() -> None:
    assert len(_CONFIG.diurnal.hourly_multiplier) == 24
    # 03:00 is modelled as lower-success than 09:00 in the bundled config.
    assert _CONFIG.diurnal.hourly_multiplier[3] < _CONFIG.diurnal.hourly_multiplier[9]


# --- Customer propensity and the control-arm counterfactual ------------------


def test_customer_propensity_is_stable_for_the_same_customer() -> None:
    world = _world()
    assert world.customer_propensity("cust_1") == world.customer_propensity("cust_1")


def test_customer_propensity_is_bounded() -> None:
    world = _world()
    for i in range(100):
        p = world.customer_propensity(f"cust_{i}")
        assert 0.0 <= p <= 1.0


def test_would_recover_unaided_varies_across_customers() -> None:
    world = _world(seed=42)
    outcomes = {world.would_recover_unaided(f"cust_{i}") for i in range(200)}
    assert outcomes == {True, False}  # both outcomes occur -- not a constant


# --- Intervention response ----------------------------------------------------


def test_click_through_respects_configured_channel_rate() -> None:
    world = _world(seed=5)
    # payment_retry is configured at rate 1.0 -- always "delivered".
    for i in range(20):
        assert world.click_through(customer_id=f"c{i}", channel="payment_retry", message_id=f"m{i}")


def test_click_through_is_deterministic() -> None:
    world = _world()
    first = world.click_through(customer_id="c1", channel="sms", message_id="m1")
    second = world.click_through(customer_id="c1", channel="sms", message_id="m1")
    assert first == second


def test_converts_given_click_is_deterministic() -> None:
    world = _world()
    first = world.converts_given_click(customer_id="c1", message_id="m1")
    second = world.converts_given_click(customer_id="c1", message_id="m1")
    assert first == second


# --- Network faults ------------------------------------------------------------


def test_network_faults_occur_at_roughly_the_configured_rate() -> None:
    world = _world(seed=99)
    at = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)  # not pre-payday, no outage expected
    n = 2000
    faults = sum(
        1
        for i in range(n)
        if world.attempt_outcome(
            customer_id=f"c{i}", instrument="wallet", issuer=f"issuer_{i % 7}", at=at
        ).decline_category
        == DeclineCategory.NETWORK_TIMEOUT
    )
    rate = faults / n
    configured = _CONFIG.network_faults.base_rate
    assert abs(rate - configured) < 0.02  # within 2pp over 2000 draws
