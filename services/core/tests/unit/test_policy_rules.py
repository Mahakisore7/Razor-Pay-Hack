"""Unit tests for the four T2.5 policy rules (`recoup.policy.rules.*`) --
each is a pure function of `(action, ctx)`, so every case here is
in-memory: no database, no mocking.
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from recoup.domain.action import Action, ActionCategory, ActionPayload, Channel
from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.consent import ConsentEvent, ConsentSource
from recoup.domain.contact import ContactEvent
from recoup.domain.decline import DeclineCategory
from recoup.domain.diagnosis import Diagnosis, DiagnosisMethod, Hypothesis, RootCause
from recoup.domain.identifiers import ActionId, CaseId, uuid7
from recoup.domain.mandate import Frequency, Mandate, MandateRail, MandateStatus
from recoup.domain.money import Money
from recoup.domain.policy_decision import Verdict
from recoup.policy.context import DndStatus, KillSwitchState, PolicyContext
from recoup.policy.rules import (
    consent,
    cost_ceiling,
    dnd,
    domain_guards,
    frequency_cap,
    kill_switch,
    quiet_hours,
    rate_limit,
)
from tests.factories import make_case, make_customer_ref

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_NO_KILL_SWITCH = KillSwitchState(global_tripped=False, tripped_playbooks=frozenset())
_NOT_ON_DND = DndStatus(registered=False)
_UTC = ZoneInfo("UTC")
_IST = ZoneInfo("Asia/Kolkata")


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
    customer_timezone: ZoneInfo = _UTC,
    contact_history: tuple[ContactEvent, ...] = (),
    mandate: Mandate | None = None,
    kill_switch_state: KillSwitchState = _NO_KILL_SWITCH,
    rate_limit_tokens: Mapping[Channel, int] = MappingProxyType({}),
    now: datetime = _NOW,
) -> PolicyContext:
    return PolicyContext(
        now=now,
        case=case if case is not None else make_case(),
        playbook_id=playbook_id,
        consent_events=consent_events,
        dnd_status=dnd_status,
        customer_timezone=customer_timezone,
        contact_history=contact_history,
        mandate=mandate,
        kill_switch=kill_switch_state,
        rate_limit_tokens=rate_limit_tokens,
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


# --- R6: quiet hours ---------------------------------------------------------------


def test_quiet_hours_exempts_payment_retry_link_and_email() -> None:
    """None of the three is a message a customer perceives at a time of
    day at all (payment_retry/link: not contact; email: asynchronous) --
    `_NOW` (midnight UTC) is deep inside the forbidden window, so this
    would fail for any non-exempt channel."""
    case = make_case()
    for channel in (Channel.PAYMENT_RETRY, Channel.LINK, Channel.EMAIL):
        action = _action(case_id=case.id, channel=channel)
        assert quiet_hours.evaluate(action, _context(case=case)) is None


def test_quiet_hours_allows_sms_inside_the_default_window() -> None:
    case = make_case()
    noon_utc = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    action = _action(case_id=case.id, channel=Channel.SMS, due_at=noon_utc)
    ctx = _context(case=case, now=noon_utc)
    assert quiet_hours.evaluate(action, ctx) is None


def test_quiet_hours_allows_sms_exactly_at_the_windows_open() -> None:
    case = make_case()
    at_open = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)  # half-open [start, end)
    action = _action(case_id=case.id, channel=Channel.SMS, due_at=at_open)
    ctx = _context(case=case, now=at_open)
    assert quiet_hours.evaluate(action, ctx) is None


def test_quiet_hours_defers_sms_exactly_at_the_windows_close() -> None:
    case = make_case()
    at_close = datetime(2026, 1, 1, 21, 0, tzinfo=UTC)  # half-open [start, end)
    action = _action(case_id=case.id, channel=Channel.SMS, due_at=at_close)
    ctx = _context(case=case, now=at_close)
    decision = quiet_hours.evaluate(action, ctx)
    assert decision is not None
    assert decision.rule_id == "quiet_hours"


def test_quiet_hours_defers_sms_before_the_window_opens_to_later_today() -> None:
    case = make_case()
    early = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    action = _action(case_id=case.id, channel=Channel.SMS, due_at=early)
    ctx = _context(case=case, now=early)
    decision = quiet_hours.evaluate(action, ctx)
    assert decision is not None
    assert decision.verdict == Verdict.DEFER
    assert decision.rule_id == "quiet_hours"
    assert decision.defer_until == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    assert decision.decided_at == early


def test_quiet_hours_defers_sms_after_the_window_closes_to_tomorrow() -> None:
    case = make_case()
    late = datetime(2026, 1, 1, 22, 0, tzinfo=UTC)
    action = _action(case_id=case.id, channel=Channel.SMS, due_at=late)
    ctx = _context(case=case, now=late)
    decision = quiet_hours.evaluate(action, ctx)
    assert decision is not None
    assert decision.defer_until == datetime(2026, 1, 2, 9, 0, tzinfo=UTC)


def test_quiet_hours_is_evaluated_in_the_customers_own_timezone() -> None:
    """The same UTC instant is inside the window in one timezone and
    outside it in another (P3, POLICY-ENGINE SS6.2: "for any timezone") --
    05:00 UTC is 10:30 IST (allowed) but 21:00 the previous day in US
    Pacific, standard time in January (forbidden, exactly at the close)."""
    case = make_case()
    instant = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)  # 10:30 IST, 21:00 Pacific (prev day)
    action = _action(case_id=case.id, channel=Channel.SMS, due_at=instant)

    ist_ctx = _context(case=case, now=instant, customer_timezone=_IST)
    pacific_ctx = _context(
        case=case, now=instant, customer_timezone=ZoneInfo("America/Los_Angeles")
    )

    assert quiet_hours.evaluate(action, ist_ctx) is None
    pacific_decision = quiet_hours.evaluate(action, pacific_ctx)
    assert pacific_decision is not None
    assert pacific_decision.rule_id == "quiet_hours"


def test_quiet_hours_gives_voice_a_stricter_window_than_the_default() -> None:
    """09:30 is inside SMS's default window (09:00-21:00) but before
    voice's own, later-opening one (10:00-19:00)."""
    case = make_case()
    at_0930 = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    sms_action = _action(case_id=case.id, channel=Channel.SMS, due_at=at_0930)
    voice_action = _action(case_id=case.id, channel=Channel.VOICE, due_at=at_0930)
    ctx = _context(case=case, now=at_0930)

    assert quiet_hours.evaluate(sms_action, ctx) is None
    voice_decision = quiet_hours.evaluate(voice_action, ctx)
    assert voice_decision is not None
    assert voice_decision.rule_id == "quiet_hours"
    assert voice_decision.defer_until == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


# --- R7: frequency cap ---------------------------------------------------------------


def test_frequency_cap_exempts_payment_retry_and_link() -> None:
    case = make_case()
    history = tuple(
        ContactEvent(customer=case.customer, channel=Channel.SMS, occurred_at=_NOW)
        for _ in range(10)
    )
    for channel in (Channel.PAYMENT_RETRY, Channel.LINK):
        action = _action(case_id=case.id, channel=channel)
        assert frequency_cap.evaluate(action, _context(case=case, contact_history=history)) is None


def test_frequency_cap_allows_with_no_prior_contact() -> None:
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.SMS)
    assert frequency_cap.evaluate(action, _context(case=case)) is None


def test_frequency_cap_denies_a_second_sms_within_24h_per_channel_cap() -> None:
    case = make_case()
    prior = ContactEvent(customer=case.customer, channel=Channel.SMS, occurred_at=_NOW)
    action = _action(case_id=case.id, channel=Channel.SMS)
    ctx = _context(case=case, contact_history=(prior,), now=_NOW + timedelta(hours=1))
    decision = frequency_cap.evaluate(action, ctx)
    assert decision is not None
    assert decision.verdict == Verdict.DEFER
    assert decision.rule_id == "frequency_cap"
    assert decision.inputs["scope"] == "per_channel_24h"
    assert decision.defer_until == _NOW + timedelta(hours=24)


def test_frequency_cap_allows_a_second_sms_once_24h_has_elapsed() -> None:
    case = make_case()
    prior = ContactEvent(customer=case.customer, channel=Channel.SMS, occurred_at=_NOW)
    action = _action(case_id=case.id, channel=Channel.SMS)
    ctx = _context(case=case, contact_history=(prior,), now=_NOW + timedelta(hours=24))
    assert frequency_cap.evaluate(action, ctx) is None


def test_frequency_cap_denies_a_different_channel_at_the_all_channels_7d_cap() -> None:
    """Three prior contacts on three different channels -- none of them
    trips the per-channel-24h cap on their own, but a fourth on a fourth
    channel still hits the "all channels, rolling 7d" cap of three."""
    case = make_case()
    history = (
        ContactEvent(customer=case.customer, channel=Channel.SMS, occurred_at=_NOW),
        ContactEvent(customer=case.customer, channel=Channel.EMAIL, occurred_at=_NOW),
        ContactEvent(customer=case.customer, channel=Channel.WHATSAPP, occurred_at=_NOW),
    )
    action = _action(case_id=case.id, channel=Channel.VOICE)
    ctx = _context(case=case, contact_history=history, now=_NOW + timedelta(days=1))
    decision = frequency_cap.evaluate(action, ctx)
    assert decision is not None
    assert decision.inputs["scope"] == "all_channels_7d"
    assert decision.defer_until == _NOW + timedelta(days=7)


def test_frequency_cap_allows_the_third_contact_within_the_all_channels_cap() -> None:
    """Exactly at the cap boundary (T4.8's own philosophy): two prior
    contacts is still under the cap of three, so a third is allowed."""
    case = make_case()
    history = (
        ContactEvent(customer=case.customer, channel=Channel.SMS, occurred_at=_NOW),
        ContactEvent(customer=case.customer, channel=Channel.EMAIL, occurred_at=_NOW),
    )
    action = _action(case_id=case.id, channel=Channel.WHATSAPP)
    ctx = _context(case=case, contact_history=history, now=_NOW + timedelta(days=1))
    assert frequency_cap.evaluate(action, ctx) is None


def test_frequency_cap_denies_a_second_voice_call_within_its_own_7d_cap() -> None:
    """Voice's cap (1 per 7d) is stricter than the default per-channel-24h
    cap alone would imply -- a call from 2 days ago is already outside
    the 24h window but still active against voice's own 7-day one."""
    case = make_case()
    prior = ContactEvent(customer=case.customer, channel=Channel.VOICE, occurred_at=_NOW)
    action = _action(case_id=case.id, channel=Channel.VOICE)
    ctx = _context(case=case, contact_history=(prior,), now=_NOW + timedelta(days=2))
    decision = frequency_cap.evaluate(action, ctx)
    assert decision is not None
    assert decision.inputs["scope"] == "voice_7d"
    assert decision.defer_until == _NOW + timedelta(days=7)


def test_frequency_cap_allows_a_second_voice_call_once_7d_has_elapsed() -> None:
    case = make_case()
    prior = ContactEvent(customer=case.customer, channel=Channel.VOICE, occurred_at=_NOW)
    action = _action(case_id=case.id, channel=Channel.VOICE)
    ctx = _context(case=case, contact_history=(prior,), now=_NOW + timedelta(days=7))
    assert frequency_cap.evaluate(action, ctx) is None


def test_frequency_cap_is_counted_across_cases_not_per_case() -> None:
    """`ContactEvent` carries no `case_id` at all (domain/contact.py) --
    the rule cannot distinguish "this case" from "another case for the
    same customer" even if it wanted to, which is the point (POLICY-ENGINE
    SS3: counted across all cases for that customer, not per case)."""
    case_one = make_case()
    case_two = make_case()
    # Same customer, two different cases -- deliberately overwritten so
    # `ContactEvent`'s own customer, not either case's, is what R7 sees.
    prior = ContactEvent(customer=case_one.customer, channel=Channel.SMS, occurred_at=_NOW)
    action = _action(case_id=case_two.id, channel=Channel.SMS)
    ctx = _context(case=case_two, contact_history=(prior,), now=_NOW + timedelta(hours=1))
    decision = frequency_cap.evaluate(action, ctx)
    assert decision is not None
    assert decision.rule_id == "frequency_cap"


# --- R11: rate limits ----------------------------------------------------------------


def test_rate_limit_allows_a_channel_with_tokens_remaining() -> None:
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.SMS)
    ctx = _context(case=case, rate_limit_tokens={Channel.SMS: 5})
    assert rate_limit.evaluate(action, ctx) is None


def test_rate_limit_allows_a_channel_absent_from_the_mapping() -> None:
    """Missing throughput data is unconstrained, not exhausted -- this
    rule's own docstring."""
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.SMS)
    ctx = _context(case=case, rate_limit_tokens={})
    assert rate_limit.evaluate(action, ctx) is None


def test_rate_limit_defers_a_channel_with_zero_tokens_remaining() -> None:
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.SMS)
    ctx = _context(case=case, rate_limit_tokens={Channel.SMS: 0}, now=_NOW)
    decision = rate_limit.evaluate(action, ctx)
    assert decision is not None
    assert decision.verdict == Verdict.DEFER
    assert decision.rule_id == "rate_limited"
    assert decision.inputs == {"channel": "sms", "tokens_remaining": 0}
    assert decision.defer_until == _NOW + timedelta(minutes=1)
    assert decision.decided_at == _NOW


def test_rate_limit_does_not_affect_a_different_channels_tokens() -> None:
    case = make_case()
    action = _action(case_id=case.id, channel=Channel.WHATSAPP)
    ctx = _context(case=case, rate_limit_tokens={Channel.SMS: 0})
    assert rate_limit.evaluate(action, ctx) is None


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
