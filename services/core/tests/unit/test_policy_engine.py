"""Unit tests for `recoup.policy.engine.evaluate` (T2.5) -- the dispatch
loop itself: rule ordering, short-circuiting on the first firing rule, and
the fallthrough `ALLOW` when nothing denies.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from zoneinfo import ZoneInfo

from recoup.domain.action import Action, ActionCategory, ActionPayload, Channel
from recoup.domain.case import Case, CaseState
from recoup.domain.consent import ConsentEvent, ConsentSource
from recoup.domain.contact import ContactEvent
from recoup.domain.identifiers import ActionId, CaseId, uuid7
from recoup.domain.money import Money
from recoup.domain.policy_decision import Verdict
from recoup.policy.context import DndStatus, KillSwitchState, PolicyContext
from recoup.policy.engine import evaluate
from tests.factories import make_case

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_NO_KILL_SWITCH = KillSwitchState(global_tripped=False, tripped_playbooks=frozenset())
_NOT_ON_DND = DndStatus(registered=False)
_UTC = ZoneInfo("UTC")


def _action(
    *,
    case_id: CaseId,
    channel: Channel = Channel.PAYMENT_RETRY,
    category: ActionCategory = ActionCategory.TRANSACTIONAL,
    cost: Money = Money(0),
) -> Action:
    return Action(
        id=ActionId(uuid7()),
        case_id=case_id,
        step_id="retry",
        attempt=1,
        channel=channel,
        category=category,
        payload=ActionPayload(),
        cost=cost,
        due_at=_NOW,
    )


def _context(
    *,
    case: Case,
    playbook_id: str = "insufficient-funds",
    consent_events: tuple[ConsentEvent, ...] = (),
    dnd_status: DndStatus = _NOT_ON_DND,
    customer_timezone: ZoneInfo = _UTC,
    contact_history: tuple[ContactEvent, ...] = (),
    kill_switch: KillSwitchState = _NO_KILL_SWITCH,
    rate_limit_tokens: Mapping[Channel, int] = MappingProxyType({}),
) -> PolicyContext:
    return PolicyContext(
        now=_NOW,
        case=case,
        playbook_id=playbook_id,
        consent_events=consent_events,
        dnd_status=dnd_status,
        customer_timezone=customer_timezone,
        contact_history=contact_history,
        mandate=None,
        kill_switch=kill_switch,
        rate_limit_tokens=rate_limit_tokens,
    )


def _granted_sms_consent(case: Case) -> ConsentEvent:
    return ConsentEvent(
        customer=case.customer,
        channel=Channel.SMS,
        granted=True,
        source=ConsentSource.CHECKOUT,
        occurred_at=_NOW,
    )


def test_evaluate_allows_when_no_rule_fires() -> None:
    case = make_case(state=CaseState.EXECUTING, cost_spent=Money(0), cost_ceiling=Money(100))
    action = _action(case_id=case.id, cost=Money(10))

    decision = evaluate(action, _context(case=case))

    assert decision.verdict == Verdict.ALLOW
    assert decision.rule_id is None
    assert decision.inputs == {}
    assert decision.action_id == action.id
    assert decision.attempt == action.attempt
    assert decision.defer_until is None
    assert decision.decided_at == _NOW


def test_evaluate_returns_the_kill_switch_denial_before_checking_anything_else() -> None:
    """Kill switch runs first (POLICY-ENGINE SS2.2): a terminal case *and*
    a tripped kill switch must still report `kill_switch_active`, the more
    fundamental reason."""
    case = make_case(state=CaseState.RECOVERED)  # would also fail domain_guard
    action = _action(case_id=case.id)
    ctx = _context(case=case, kill_switch=KillSwitchState(True, frozenset()))

    decision = evaluate(action, ctx)

    assert decision.verdict == Verdict.DENY
    assert decision.rule_id == "kill_switch_active"


def test_evaluate_falls_through_to_domain_guard_when_kill_switch_passes() -> None:
    case = make_case(state=CaseState.RECOVERED)
    action = _action(case_id=case.id)

    decision = evaluate(action, _context(case=case))

    assert decision.verdict == Verdict.DENY
    assert decision.rule_id == "domain_guard"


def test_evaluate_falls_through_to_consent_when_earlier_rules_pass() -> None:
    case = make_case(state=CaseState.EXECUTING)
    action = _action(case_id=case.id, channel=Channel.SMS)

    decision = evaluate(action, _context(case=case))

    assert decision.verdict == Verdict.DENY
    assert decision.rule_id == "no_consent"


def test_evaluate_falls_through_to_dnd_when_earlier_rules_pass() -> None:
    """`payment_retry` is R4-exempt, so this isolates R5 from consent
    entirely -- DND is checked independently of the consent ledger
    (POLICY-ENGINE SS3, R5: a national registry, not merchant-collected
    permission)."""
    case = make_case(state=CaseState.EXECUTING)
    action = _action(
        case_id=case.id, channel=Channel.PAYMENT_RETRY, category=ActionCategory.PROMOTIONAL
    )

    decision = evaluate(action, _context(case=case, dnd_status=DndStatus(registered=True)))

    assert decision.verdict == Verdict.DENY
    assert decision.rule_id == "dnd_registered"


def test_evaluate_falls_through_to_quiet_hours_when_earlier_rules_pass() -> None:
    """SMS passes R4 only with consent granted -- granting it here isolates
    R6 the same way `test_evaluate_falls_through_to_dnd_...` isolates R5,
    by letting every earlier rule pass on its own terms. `_NOW` is
    midnight UTC, outside the default 09:00-21:00 allowed window."""
    case = make_case(state=CaseState.EXECUTING)
    action = _action(case_id=case.id, channel=Channel.SMS)
    ctx = _context(case=case, consent_events=(_granted_sms_consent(case),))

    decision = evaluate(action, ctx)

    assert decision.verdict == Verdict.DEFER
    assert decision.rule_id == "quiet_hours"


def test_evaluate_falls_through_to_frequency_cap_when_earlier_rules_pass() -> None:
    """Voice passes consent (granted) and quiet hours (a call inside the
    default IST-anchored voice window), so a single prior voice contact
    -- already at the per-channel-24h cap of one -- is what stops this
    one; `test_policy_rules.py`'s own R7 tests isolate each of the three
    caps individually."""
    case = make_case(state=CaseState.EXECUTING)
    ist_noon = datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    action = Action(
        id=ActionId(uuid7()),
        case_id=case.id,
        step_id="retry",
        attempt=1,
        channel=Channel.VOICE,
        category=ActionCategory.TRANSACTIONAL,
        payload=ActionPayload(),
        cost=Money(0),
        due_at=ist_noon,
    )
    ctx = PolicyContext(
        now=ist_noon.astimezone(UTC),
        case=case,
        playbook_id="insufficient-funds",
        consent_events=(
            ConsentEvent(
                customer=case.customer,
                channel=Channel.VOICE,
                granted=True,
                source=ConsentSource.CHECKOUT,
                occurred_at=ist_noon,
            ),
        ),
        dnd_status=_NOT_ON_DND,
        customer_timezone=ZoneInfo("Asia/Kolkata"),
        contact_history=(
            ContactEvent(customer=case.customer, channel=Channel.VOICE, occurred_at=ist_noon),
        ),
        mandate=None,
        kill_switch=_NO_KILL_SWITCH,
        rate_limit_tokens={},
    )

    decision = evaluate(action, ctx)

    assert decision.verdict == Verdict.DEFER
    assert decision.rule_id == "frequency_cap"


def test_evaluate_falls_through_to_cost_ceiling_when_earlier_rules_pass() -> None:
    case = make_case(state=CaseState.EXECUTING, cost_spent=Money(95), cost_ceiling=Money(100))
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY, cost=Money(10))

    decision = evaluate(action, _context(case=case))

    assert decision.verdict == Verdict.DENY
    assert decision.rule_id == "cost_ceiling"


def test_evaluate_falls_through_to_rate_limit_when_earlier_rules_pass() -> None:
    """`payment_retry` is exempt from every rule but R8/R11 -- an empty
    cost ceiling and an exhausted rate-limit bucket isolate R11 cleanly."""
    case = make_case(state=CaseState.EXECUTING, cost_spent=Money(0), cost_ceiling=Money(100))
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY, cost=Money(10))
    ctx = _context(case=case, rate_limit_tokens={Channel.PAYMENT_RETRY: 0})

    decision = evaluate(action, ctx)

    assert decision.verdict == Verdict.DEFER
    assert decision.rule_id == "rate_limited"


def test_evaluate_is_deterministic_for_identical_inputs() -> None:
    case = make_case(state=CaseState.EXECUTING, cost_spent=Money(0), cost_ceiling=Money(100))
    action = _action(case_id=case.id, cost=Money(10))
    ctx = _context(case=case)

    assert evaluate(action, ctx) == evaluate(action, ctx)
