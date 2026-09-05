"""Property tests for `recoup.policy.engine.evaluate` (T2.5).

POLICY-ENGINE SS6.2's property list (P1-P9) targets the full R1-R11 rule
set; only a subset is provable against the rules that exist as of this
file's own last edit (kill switch, domain guards, consent, DND, quiet
hours, frequency cap, cost ceiling, rate limits):

- P1: `evaluate` never raises, for any well-formed action/context, and
  always returns one of the three verdicts.
- P4: no consent record at `due_at` never allows a non-exempt channel.
- P8: `evaluate` is deterministic -- identical inputs, identical output.
- A cost-ceiling invariant standing in for P2: whenever `evaluate` ALLOWs,
  `cost_spent + action.cost <= cost_ceiling`.
- A control-arm invariant standing in for P9: a control-arm case is never
  ALLOWed, for any other input this phase's rules can vary.

Every hypothesis-generated action here is `transactional`, with a fixed
`customer_timezone` and an empty `contact_history`/`rate_limit_tokens` --
DND, quiet hours, frequency cap, and rate limits exist as real rules
(T4.1) but are not yet exercised across *their own* dimensions (category,
timezone, prior contacts, token counts). The full P3 (quiet hours, any
timezone), P5 (mandate concurrency), P6 (stopping rules), P7 (frequency
cap across cases), and P9 (zero *executed* actions -- an executor-level
claim) are `test/policy-property-invariants`' own scope (T4.7), once
every rule they need exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import strategies as st

from recoup.domain.action import Action, ActionCategory, ActionPayload, Channel
from recoup.domain.case import Arm, CaseState
from recoup.domain.consent import ConsentEvent, ConsentSource
from recoup.domain.identifiers import ActionId, CaseId, CustomerRef, uuid7
from recoup.domain.money import Money
from recoup.domain.policy_decision import Verdict
from recoup.policy.context import DndStatus, KillSwitchState, PolicyContext
from recoup.policy.engine import evaluate
from tests.factories import make_case

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_NO_KILL_SWITCH = KillSwitchState(global_tripped=False, tripped_playbooks=frozenset())
_NOT_ON_DND = DndStatus(registered=False)
_UTC = ZoneInfo("UTC")

_case_states = st.sampled_from(list(CaseState))
_arms = st.sampled_from(list(Arm))
_channels = st.sampled_from(list(Channel))
_paise = st.integers(min_value=0, max_value=10_000_000)


def _action(channel: Channel, cost_paise: int, case_id: CaseId) -> Action:
    return Action(
        id=ActionId(uuid7()),
        case_id=case_id,
        step_id="retry",
        attempt=1,
        channel=channel,
        # Every hypothesis-generated action here is transactional --
        # DND (R5) is only provable, not yet exercised, against the
        # `category` dimension; that expansion is T4.7's own scope.
        category=ActionCategory.TRANSACTIONAL,
        payload=ActionPayload(),
        cost=Money(cost_paise),
        due_at=_NOW,
    )


def _granted_consent(case_customer: CustomerRef, channel: Channel) -> tuple[ConsentEvent, ...]:
    return (
        ConsentEvent(
            customer=case_customer,
            channel=channel,
            granted=True,
            source=ConsentSource.CHECKOUT,
            occurred_at=_NOW - timedelta(days=1),
        ),
    )


@given(
    state=_case_states,
    arm=_arms,
    channel=_channels,
    cost_spent_paise=_paise,
    ceiling_paise=_paise,
    action_cost_paise=_paise,
    global_tripped=st.booleans(),
    has_consent=st.booleans(),
)
def test_evaluate_always_returns_exactly_one_verdict_and_never_raises(
    state: CaseState,
    arm: Arm,
    channel: Channel,
    cost_spent_paise: int,
    ceiling_paise: int,
    action_cost_paise: int,
    global_tripped: bool,
    has_consent: bool,
) -> None:
    case = make_case(
        state=state, arm=arm, cost_spent=Money(cost_spent_paise), cost_ceiling=Money(ceiling_paise)
    )
    action = _action(channel, action_cost_paise, case.id)
    consent_events = _granted_consent(case.customer, channel) if has_consent else ()
    ctx = PolicyContext(
        now=_NOW,
        case=case,
        playbook_id="insufficient-funds",
        consent_events=consent_events,
        dnd_status=_NOT_ON_DND,
        customer_timezone=_UTC,
        contact_history=(),
        mandate=None,
        kill_switch=KillSwitchState(global_tripped=global_tripped, tripped_playbooks=frozenset()),
        rate_limit_tokens={},
    )

    decision = evaluate(action, ctx)

    assert decision.verdict in (Verdict.ALLOW, Verdict.DENY, Verdict.DEFER)


@given(channel=_channels, action_cost_paise=_paise)
def test_no_consent_at_due_at_never_allows_a_non_exempt_channel(
    channel: Channel, action_cost_paise: int
) -> None:
    case = make_case(state=CaseState.EXECUTING, cost_ceiling=Money(10_000_000))
    action = _action(channel, action_cost_paise, case.id)
    ctx = PolicyContext(
        now=_NOW,
        case=case,
        playbook_id="insufficient-funds",
        consent_events=(),
        dnd_status=_NOT_ON_DND,
        customer_timezone=_UTC,
        contact_history=(),
        mandate=None,
        kill_switch=_NO_KILL_SWITCH,
        rate_limit_tokens={},
    )

    decision = evaluate(action, ctx)

    if channel not in (Channel.PAYMENT_RETRY, Channel.LINK):
        assert decision.verdict != Verdict.ALLOW


@given(
    state=_case_states,
    arm=_arms,
    channel=_channels,
    cost_spent_paise=_paise,
    ceiling_paise=_paise,
    action_cost_paise=_paise,
)
def test_evaluate_is_deterministic(
    state: CaseState,
    arm: Arm,
    channel: Channel,
    cost_spent_paise: int,
    ceiling_paise: int,
    action_cost_paise: int,
) -> None:
    case = make_case(
        state=state, arm=arm, cost_spent=Money(cost_spent_paise), cost_ceiling=Money(ceiling_paise)
    )
    action = _action(channel, action_cost_paise, case.id)
    ctx = PolicyContext(
        now=_NOW,
        case=case,
        playbook_id="insufficient-funds",
        consent_events=(),
        dnd_status=_NOT_ON_DND,
        customer_timezone=_UTC,
        contact_history=(),
        mandate=None,
        kill_switch=_NO_KILL_SWITCH,
        rate_limit_tokens={},
    )

    assert evaluate(action, ctx) == evaluate(action, ctx)


@given(
    state=_case_states,
    channel=_channels,
    cost_spent_paise=_paise,
    ceiling_paise=_paise,
    action_cost_paise=_paise,
)
def test_an_allow_never_lets_cost_spent_exceed_the_ceiling(
    state: CaseState,
    channel: Channel,
    cost_spent_paise: int,
    ceiling_paise: int,
    action_cost_paise: int,
) -> None:
    case = make_case(
        state=state,
        arm=Arm.TREATMENT,
        cost_spent=Money(cost_spent_paise),
        cost_ceiling=Money(ceiling_paise),
    )
    action = _action(channel, action_cost_paise, case.id)
    ctx = PolicyContext(
        now=_NOW,
        case=case,
        playbook_id="insufficient-funds",
        consent_events=_granted_consent(case.customer, channel),
        dnd_status=_NOT_ON_DND,
        customer_timezone=_UTC,
        contact_history=(),
        mandate=None,
        kill_switch=_NO_KILL_SWITCH,
        rate_limit_tokens={},
    )

    decision = evaluate(action, ctx)

    if decision.verdict == Verdict.ALLOW:
        assert cost_spent_paise + action_cost_paise <= ceiling_paise


@given(
    state=_case_states,
    channel=_channels,
    cost_spent_paise=_paise,
    ceiling_paise=_paise,
    action_cost_paise=_paise,
)
def test_a_control_arm_case_is_never_allowed(
    state: CaseState,
    channel: Channel,
    cost_spent_paise: int,
    ceiling_paise: int,
    action_cost_paise: int,
) -> None:
    case = make_case(
        state=state,
        arm=Arm.CONTROL,
        cost_spent=Money(cost_spent_paise),
        cost_ceiling=Money(ceiling_paise),
    )
    action = _action(channel, action_cost_paise, case.id)
    ctx = PolicyContext(
        now=_NOW,
        case=case,
        playbook_id="insufficient-funds",
        consent_events=_granted_consent(case.customer, channel),
        dnd_status=_NOT_ON_DND,
        customer_timezone=_UTC,
        contact_history=(),
        mandate=None,
        kill_switch=_NO_KILL_SWITCH,
        rate_limit_tokens={},
    )

    decision = evaluate(action, ctx)

    assert decision.verdict != Verdict.ALLOW
