"""PolicyDecision stores its inputs verbatim (DOMAIN-MODEL SS8) so a denial
is auditable, not merely logged."""

from recoup.domain.identifiers import ActionId, uuid7
from recoup.domain.policy_decision import PolicyDecision, Verdict
from tests.factories import EPOCH


def test_deny_decision_carries_the_inputs_it_was_decided_on() -> None:
    decision = PolicyDecision(
        action_id=ActionId(uuid7()),
        attempt=1,
        verdict=Verdict.DENY,
        rule_id="quiet_hours",
        inputs={"local_hour": 23, "quiet_hours_start": 22, "quiet_hours_end": 8},
        defer_until=None,
        decided_at=EPOCH,
    )
    assert decision.verdict == Verdict.DENY
    assert decision.inputs["local_hour"] == 23


def test_allow_decision_has_no_rule_id_when_nothing_denied_it() -> None:
    decision = PolicyDecision(
        action_id=ActionId(uuid7()),
        attempt=1,
        verdict=Verdict.ALLOW,
        rule_id=None,
        inputs={},
        defer_until=None,
        decided_at=EPOCH,
    )
    assert decision.rule_id is None
