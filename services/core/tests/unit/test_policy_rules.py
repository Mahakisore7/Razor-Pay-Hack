"""Unit tests for the four T2.5 policy rules (`recoup.policy.rules.*`) --
each is a pure function of `(action, ctx)`, so every case here is
in-memory: no database, no mocking.
"""

from datetime import UTC, date, datetime

from recoup.domain.action import Action, ActionCategory, ActionPayload, Channel
from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.consent import ConsentEvent, ConsentSource
from recoup.domain.decline import DeclineCategory
from recoup.domain.diagnosis import Diagnosis, DiagnosisMethod, Hypothesis, RootCause
from recoup.domain.identifiers import ActionId, CaseId, uuid7
from recoup.domain.mandate import Frequency, Mandate, MandateRail, MandateStatus
from recoup.domain.money import Money
from recoup.domain.policy_decision import Verdict
from recoup.policy.context import DndStatus, KillSwitchState, PolicyContext
from recoup.policy.rules import consent, cost_ceiling, dnd, domain_guards, kill_switch
from tests.factories import make_case, make_customer_ref

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_NO_KILL_SWITCH = KillSwitchState(global_tripped=False, tripped_playbooks=frozenset())
_NOT_ON_DND = DndStatus(registered=False)


def _action(
    *,
    case_id: CaseId,
    channel: Channel = Channel.PAYMENT_RETRY,
    category: ActionCategory = ActionCategory.TRANSACTIONAL,
    cost: Money = Money(0),
    due_at: datetime = _NOW,
    attempt: int = 1,
) -> Action:
    return Action(
        id=ActionId(uuid7()),
        case_id=case_id,
        step_id="retry",
        attempt=attempt,
        channel=channel,
        category=category,
        payload=ActionPayload(),
        cost=cost,
        due_at=due_at,
    )


def _context(
    *,
    case: Case | None = None,
    playbook_id: str = "insufficient-funds",
    consent_events: tuple[ConsentEvent, ...] = (),
    dnd_status: DndStatus = _NOT_ON_DND,
    mandate: Mandate | None = None,
    kill_switch_state: KillSwitchState = _NO_KILL_SWITCH,
    now: datetime = _NOW,
) -> PolicyContext:
    return PolicyContext(
        now=now,
        case=case if case is not None else make_case(),
        playbook_id=playbook_id,
        consent_events=consent_events,
        dnd_status=dnd_status,
        mandate=mandate,
        kill_switch=kill_switch_state,
    )


def _diagnosis(root_cause: str | None) -> Diagnosis:
    hypotheses = (
        ()
        if root_cause is None
        else (Hypothesis(RootCause(root_cause), confidence=1.0, evidence=(), narration=None),)
    )
    return Diagnosis(
        case_id=CaseId(uuid7()),
        hypotheses=hypotheses,
        method=DiagnosisMethod.ABSTAINED if root_cause is None else DiagnosisMethod.STATISTICAL,
        computed_at=_NOW,
        llm_model=None,
        fallback_reason=None,
    )


def _mandate(
    *,
    max_amount: Money = Money(500_000),
    valid_from: date = date(2025, 1, 1),
    valid_until: date = date(2027, 1, 1),
) -> Mandate:
    return Mandate(
        id="mandate_test",
        customer=make_customer_ref(),
        rail=MandateRail.UPI_AUTOPAY,
        max_amount=max_amount,
        frequency=Frequency.MONTHLY,
        valid_from=valid_from,
        valid_until=valid_until,
        status=MandateStatus.ACTIVE,
        representations_used_this_cycle=0,
        representation_cap=4,
    )


# --- R1: kill switch ------------------------------------------------------------


def test_kill_switch_passes_when_nothing_is_tripped() -> None:
    case = make_case()
    action = _action(case_id=case.id)
    assert kill_switch.evaluate(action, _context(case=case)) is None


def test_kill_switch_denies_when_globally_tripped() -> None:
    case = make_case()
    action = _action(case_id=case.id)
    ctx = _context(
        case=case,
        kill_switch_state=KillSwitchState(global_tripped=True, tripped_playbooks=frozenset()),
    )
    decision = kill_switch.evaluate(action, ctx)
    assert decision is not None
    assert decision.verdict == Verdict.DENY
    assert decision.rule_id == "kill_switch_active"
    assert decision.action_id == action.id
    assert decision.attempt == action.attempt
    assert decision.inputs["global_tripped"] is True
    assert decision.decided_at == _NOW


def test_kill_switch_denies_when_this_playbook_is_tripped() -> None:
    case = make_case()
    action = _action(case_id=case.id)
    ctx = _context(
        case=case,
        playbook_id="insufficient-funds",
        kill_switch_state=KillSwitchState(
            global_tripped=False, tripped_playbooks=frozenset({"insufficient-funds"})
        ),
    )
    decision = kill_switch.evaluate(action, ctx)
    assert decision is not None
    assert decision.inputs["playbook_tripped"] is True


def test_kill_switch_ignores_a_different_playbooks_trip() -> None:
    case = make_case()
    action = _action(case_id=case.id)
    ctx = _context(
        case=case,
        playbook_id="insufficient-funds",
        kill_switch_state=KillSwitchState(
            global_tripped=False, tripped_playbooks=frozenset({"some-other-playbook"})
        ),
    )
    assert kill_switch.evaluate(action, ctx) is None


# --- R3: domain guards ------------------------------------------------------------


def test_domain_guards_passes_a_normal_retry() -> None:
    case = make_case(state=CaseState.EXECUTING)
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY)
    assert domain_guards.evaluate(action, _context(case=case)) is None


def test_domain_guards_denies_against_a_terminal_case() -> None:
    case = make_case(state=CaseState.RECOVERED)
    action = _action(case_id=case.id)
    decision = domain_guards.evaluate(action, _context(case=case))
    assert decision is not None
    assert decision.rule_id == "domain_guard"
    assert decision.inputs["reason"] == "case_is_terminal"


def test_domain_guards_denies_a_control_arm_case() -> None:
    """I7 (Case.transition_to) already keeps a control case out of
    EXECUTING; this is defence in depth for the same invariant, and the
    mechanism behind P9 (POLICY-ENGINE SS6.2)."""
    case = make_case(arm=Arm.CONTROL, state=CaseState.PLANNED)
    action = _action(case_id=case.id)
    decision = domain_guards.evaluate(action, _context(case=case))
    assert decision is not None
    assert decision.inputs["reason"] == "control_arm_case"


def test_domain_guards_denies_a_retry_against_a_non_retryable_decline() -> None:
    case = make_case(state=CaseState.EXECUTING)
    case.diagnosis = _diagnosis(DeclineCategory.INVALID_INSTRUMENT.value)
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY)
    decision = domain_guards.evaluate(action, _context(case=case))
    assert decision is not None
    assert decision.inputs["reason"] == "decline_not_retryable"
    assert decision.inputs["decline_category"] == "invalid_instrument"


def test_domain_guards_allows_a_retry_against_a_retryable_decline() -> None:
    case = make_case(state=CaseState.EXECUTING)
    case.diagnosis = _diagnosis(DeclineCategory.INSUFFICIENT_FUNDS.value)
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY)
    assert domain_guards.evaluate(action, _context(case=case)) is None


def test_domain_guards_ignores_an_unrecognised_root_cause() -> None:
    """`RootCause` is playbook-defined vocabulary, not a closed enum -- an
    unrecognised value is not enough information to refuse on, not a
    reason to refuse."""
    case = make_case(state=CaseState.EXECUTING)
    case.diagnosis = _diagnosis("some_future_playbooks_own_root_cause")
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY)
    assert domain_guards.evaluate(action, _context(case=case)) is None


def test_domain_guards_ignores_a_case_with_no_diagnosis_yet() -> None:
    case = make_case(state=CaseState.EXECUTING)
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY)
    assert domain_guards.evaluate(action, _context(case=case)) is None


def test_domain_guards_only_checks_retryability_for_payment_retry() -> None:
    case = make_case(state=CaseState.EXECUTING)
    case.diagnosis = _diagnosis(DeclineCategory.INVALID_INSTRUMENT.value)
    action = _action(case_id=case.id, channel=Channel.LINK)
    assert domain_guards.evaluate(action, _context(case=case)) is None


def test_domain_guards_denies_a_debit_above_the_mandates_max_amount() -> None:
    case = make_case(state=CaseState.EXECUTING, at_risk=Money(600_000))
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY)
    mandate = _mandate(max_amount=Money(500_000))
    decision = domain_guards.evaluate(action, _context(case=case, mandate=mandate))
    assert decision is not None
    assert decision.inputs["reason"] == "mandate_amount_exceeded"
    assert decision.inputs["amount_paise"] == 600_000
    assert decision.inputs["max_amount_paise"] == 500_000


def test_domain_guards_denies_a_debit_outside_the_mandates_validity_window() -> None:
    case = make_case(state=CaseState.EXECUTING, at_risk=Money(100_000))
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY)
    mandate = _mandate(valid_from=date(2020, 1, 1), valid_until=date(2020, 12, 31))
    decision = domain_guards.evaluate(action, _context(case=case, mandate=mandate))
    assert decision is not None
    assert decision.inputs["reason"] == "mandate_not_valid"
    assert decision.inputs["valid_until"] == "2020-12-31"


def test_domain_guards_allows_a_debit_a_valid_mandate_authorises() -> None:
    case = make_case(state=CaseState.EXECUTING, at_risk=Money(100_000))
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY)
    mandate = _mandate(max_amount=Money(500_000))
    assert domain_guards.evaluate(action, _context(case=case, mandate=mandate)) is None


def test_domain_guards_ignores_mandate_checks_when_there_is_no_mandate() -> None:
    """L1 one-time-payment cases have no mandate at all."""
    case = make_case(state=CaseState.EXECUTING)
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY)
    assert domain_guards.evaluate(action, _context(case=case, mandate=None)) is None


# --- R4: consent -------------------------------------------------------------------


def test_consent_exempts_payment_retry() -> None:
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.PAYMENT_RETRY)
    assert consent.evaluate(action, _context(case=case, consent_events=())) is None


def test_consent_exempts_link() -> None:
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.LINK)
    assert consent.evaluate(action, _context(case=case, consent_events=())) is None


def test_consent_denies_a_messaging_channel_with_no_consent_record() -> None:
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.SMS)
    decision = consent.evaluate(action, _context(case=case, consent_events=()))
    assert decision is not None
    assert decision.rule_id == "no_consent"
    assert decision.inputs["channel"] == "sms"


def test_consent_allows_a_messaging_channel_with_consent_granted_before_due_at() -> None:
    case = make_case()
    customer = make_customer_ref()
    event = ConsentEvent(
        customer=customer,
        channel=Channel.SMS,
        granted=True,
        source=ConsentSource.CHECKOUT,
        occurred_at=datetime(2025, 12, 1, tzinfo=UTC),
    )
    action = _action(case_id=case.id, channel=Channel.SMS, due_at=_NOW)
    assert consent.evaluate(action, _context(case=case, consent_events=(event,))) is None


def test_consent_denies_when_consent_was_revoked_before_due_at() -> None:
    case = make_case()
    customer = make_customer_ref()
    granted = ConsentEvent(
        customer=customer,
        channel=Channel.SMS,
        granted=True,
        source=ConsentSource.CHECKOUT,
        occurred_at=datetime(2025, 12, 1, tzinfo=UTC),
    )
    revoked = ConsentEvent(
        customer=customer,
        channel=Channel.SMS,
        granted=False,
        source=ConsentSource.SMS_STOP,
        occurred_at=datetime(2025, 12, 15, tzinfo=UTC),
    )
    action = _action(case_id=case.id, channel=Channel.SMS, due_at=_NOW)
    decision = consent.evaluate(action, _context(case=case, consent_events=(granted, revoked)))
    assert decision is not None
    assert decision.rule_id == "no_consent"


def test_consent_ignores_a_grant_that_happens_after_due_at() -> None:
    case = make_case()
    customer = make_customer_ref()
    event = ConsentEvent(
        customer=customer,
        channel=Channel.SMS,
        granted=True,
        source=ConsentSource.CHECKOUT,
        occurred_at=datetime(2026, 6, 1, tzinfo=UTC),  # after due_at
    )
    action = _action(case_id=case.id, channel=Channel.SMS, due_at=_NOW)
    decision = consent.evaluate(action, _context(case=case, consent_events=(event,)))
    assert decision is not None


# --- R5: DND ---------------------------------------------------------------------


def test_dnd_passes_a_transactional_action_when_registered() -> None:
    """The regulatory carve-out this rule exists for: a customer on the
    DND registry still receives their transactional notices."""
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.SMS, category=ActionCategory.TRANSACTIONAL)
    ctx = _context(case=case, dnd_status=DndStatus(registered=True))
    assert dnd.evaluate(action, ctx) is None


def test_dnd_passes_a_promotional_action_when_not_registered() -> None:
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.SMS, category=ActionCategory.PROMOTIONAL)
    assert dnd.evaluate(action, _context(case=case, dnd_status=_NOT_ON_DND)) is None


def test_dnd_denies_a_promotional_action_when_registered() -> None:
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.SMS, category=ActionCategory.PROMOTIONAL)
    ctx = _context(case=case, dnd_status=DndStatus(registered=True))
    decision = dnd.evaluate(action, ctx)
    assert decision is not None
    assert decision.verdict == Verdict.DENY
    assert decision.rule_id == "dnd_registered"
    assert decision.inputs == {"category": "promotional"}
    assert decision.action_id == action.id
    assert decision.attempt == action.attempt
    assert decision.decided_at == _NOW


def test_dnd_ignores_channel_entirely() -> None:
    """R5 reads only `action.category` (POLICY-ENGINE SS3) -- unlike R4,
    there is no per-channel exemption list; `payment_retry` is denied here
    just as readily as `sms` would be, if it were ever marked promotional."""
    case = make_case()
    action = _action(
        case_id=case.id, channel=Channel.PAYMENT_RETRY, category=ActionCategory.PROMOTIONAL
    )
    ctx = _context(case=case, dnd_status=DndStatus(registered=True))
    decision = dnd.evaluate(action, ctx)
    assert decision is not None
    assert decision.rule_id == "dnd_registered"


# --- R8: cost ceiling ---------------------------------------------------------------


def test_cost_ceiling_allows_a_cost_within_the_ceiling() -> None:
    case = make_case(cost_spent=Money(0), cost_ceiling=Money(100))
    action = _action(case_id=case.id, cost=Money(50))
    assert cost_ceiling.evaluate(action, _context(case=case)) is None


def test_cost_ceiling_allows_a_cost_exactly_at_the_ceiling() -> None:
    case = make_case(cost_spent=Money(60), cost_ceiling=Money(100))
    action = _action(case_id=case.id, cost=Money(40))
    assert cost_ceiling.evaluate(action, _context(case=case)) is None


def test_cost_ceiling_denies_a_cost_that_would_exceed_the_ceiling() -> None:
    case = make_case(cost_spent=Money(90), cost_ceiling=Money(100))
    action = _action(case_id=case.id, cost=Money(11))
    decision = cost_ceiling.evaluate(action, _context(case=case))
    assert decision is not None
    assert decision.rule_id == "cost_ceiling"
    assert decision.inputs == {
        "cost_spent_paise": 90,
        "action_cost_paise": 11,
        "cost_ceiling_paise": 100,
    }
