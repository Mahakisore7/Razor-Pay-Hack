"""Unit tests for `recoup.policy.engine.evaluate` (T2.5) -- the dispatch
loop itself: rule ordering, short-circuiting on the first firing rule, and
the fallthrough `ALLOW` when nothing denies.
"""

from datetime import UTC, datetime

from recoup.domain.action import Action, ActionCategory, ActionPayload, Channel
from recoup.domain.case import Case, CaseState
from recoup.domain.identifiers import ActionId, CaseId, uuid7
from recoup.domain.money import Money
from recoup.domain.policy_decision import Verdict
from recoup.policy.context import DndStatus, KillSwitchState, PolicyContext
from recoup.policy.engine import evaluate
from tests.factories import make_case

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_NO_KILL_SWITCH = KillSwitchState(global_tripped=False, tripped_playbooks=frozenset())
_NOT_ON_DND = DndStatus(registered=False)


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
    dnd_status: DndStatus = _NOT_ON_DND,
    kill_switch: KillSwitchState = _NO_KILL_SWITCH,
) -> PolicyContext:
    return PolicyContext(
        now=_NOW,
        case=case,
        playbook_id=playbook_id,
        consent_events=(),
        dnd_status=dnd_status,
        mandate=None,
        kill_switch=kill_switch,
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


def test_evaluate_falls_through_to_cost_ceiling_when_earlier_rules_pass() -> None:
    case = make_case(state=CaseState.EXECUTING, cost_spent=Money(95), cost_ceiling=Money(100))
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY, cost=Money(10))

    decision = evaluate(action, _context(case=case))

    assert decision.verdict == Verdict.DENY
    assert decision.rule_id == "cost_ceiling"


def test_evaluate_is_deterministic_for_identical_inputs() -> None:
    case = make_case(state=CaseState.EXECUTING, cost_spent=Money(0), cost_ceiling=Money(100))
    action = _action(case_id=case.id, cost=Money(10))
    ctx = _context(case=case)

    assert evaluate(action, ctx) == evaluate(action, ctx)
