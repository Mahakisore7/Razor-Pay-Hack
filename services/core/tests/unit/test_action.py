"""Action's idempotency_key is derived, never accepted from the caller
(DOMAIN-MODEL SS7): recomputing the same logical action after a crash must
produce the same key so the duplicate is suppressed."""

import pytest

from recoup.domain.action import Action, ActionCategory, ActionPayload, Channel
from recoup.domain.identifiers import ActionId, CaseId, uuid7
from recoup.domain.money import Money
from tests.factories import EPOCH


def _make_action(
    *, case_id: CaseId | None = None, step_id: str = "timed_retry", attempt: int = 1
) -> Action:
    return Action(
        id=ActionId(uuid7()),
        case_id=case_id if case_id is not None else CaseId(uuid7()),
        step_id=step_id,
        attempt=attempt,
        channel=Channel.PAYMENT_RETRY,
        category=ActionCategory.TRANSACTIONAL,
        payload=ActionPayload(),
        cost=Money(300),
        due_at=EPOCH,
    )


def test_idempotency_key_is_deterministic_for_the_same_logical_action() -> None:
    case_id = CaseId(uuid7())
    first = _make_action(case_id=case_id, step_id="timed_retry", attempt=1)
    second = _make_action(case_id=case_id, step_id="timed_retry", attempt=1)
    assert first.idempotency_key == second.idempotency_key


def test_idempotency_key_differs_by_attempt() -> None:
    case_id = CaseId(uuid7())
    first = _make_action(case_id=case_id, attempt=1)
    second = _make_action(case_id=case_id, attempt=2)
    assert first.idempotency_key != second.idempotency_key


def test_idempotency_key_differs_by_step() -> None:
    case_id = CaseId(uuid7())
    first = _make_action(case_id=case_id, step_id="timed_retry")
    second = _make_action(case_id=case_id, step_id="payment_link_sms")
    assert first.idempotency_key != second.idempotency_key


def test_idempotency_key_is_unaffected_by_category() -> None:
    """Only `case_id`, `step_id`, and `attempt` may influence the derived
    key (DOMAIN-MODEL SS7) -- `category` is a policy-time classification,
    not part of the action's logical identity."""
    case_id = CaseId(uuid7())
    transactional = Action(
        id=ActionId(uuid7()),
        case_id=case_id,
        step_id="timed_retry",
        attempt=1,
        channel=Channel.SMS,
        category=ActionCategory.TRANSACTIONAL,
        payload=ActionPayload(),
        cost=Money(300),
        due_at=EPOCH,
    )
    promotional = Action(
        id=ActionId(uuid7()),
        case_id=case_id,
        step_id="timed_retry",
        attempt=1,
        channel=Channel.SMS,
        category=ActionCategory.PROMOTIONAL,
        payload=ActionPayload(),
        cost=Money(300),
        due_at=EPOCH,
    )
    assert transactional.idempotency_key == promotional.idempotency_key


def test_action_consumes_mandate_budget_defaults_to_false() -> None:
    """Mirrors `PlaybookStep.consumes_mandate_budget`'s own default --
    most steps do not touch a mandate's representation budget."""
    action = _make_action()
    assert action.consumes_mandate_budget is False


def test_idempotency_key_is_unaffected_by_consumes_mandate_budget() -> None:
    case_id = CaseId(uuid7())
    non_consuming = Action(
        id=ActionId(uuid7()),
        case_id=case_id,
        step_id="timed_retry",
        attempt=1,
        channel=Channel.PAYMENT_RETRY,
        category=ActionCategory.TRANSACTIONAL,
        payload=ActionPayload(),
        cost=Money(300),
        due_at=EPOCH,
        consumes_mandate_budget=False,
    )
    consuming = Action(
        id=ActionId(uuid7()),
        case_id=case_id,
        step_id="timed_retry",
        attempt=1,
        channel=Channel.PAYMENT_RETRY,
        category=ActionCategory.TRANSACTIONAL,
        payload=ActionPayload(),
        cost=Money(300),
        due_at=EPOCH,
        consumes_mandate_budget=True,
    )
    assert non_consuming.idempotency_key == consuming.idempotency_key


def test_action_rejects_non_positive_attempt() -> None:
    with pytest.raises(ValueError, match="attempt"):
        _make_action(attempt=0)


def test_action_payload_defaults_to_empty_variables() -> None:
    payload = ActionPayload()
    assert payload.template is None
    assert dict(payload.variables) == {}
