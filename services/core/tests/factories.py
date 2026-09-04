"""Builders for valid domain objects (ENGINEERING-STANDARDS SS4.2: fixtures
build valid objects by default; tests override only what they exercise)."""

from datetime import UTC, datetime

from recoup.domain.case import Arm, Case, CaseState
from recoup.domain.identifiers import CaseId, CustomerRef, SignalId, hash_contact, uuid7
from recoup.domain.money import Money

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def make_customer_ref(*, phone: str = "+919876543210") -> CustomerRef:
    return CustomerRef(
        id=f"cust_{uuid7()}",
        razorpay_customer_id="cust_test",
        contact_hash=hash_contact(phone),
    )


def make_case(
    *,
    case_id: CaseId | None = None,
    state: CaseState = CaseState.DETECTED,
    arm: Arm = Arm.TREATMENT,
    at_risk: Money = Money(249_900),
    cost_ceiling: Money = Money(9_996),  # 4% of at_risk, per the example playbook's ceiling
    cost_spent: Money = Money(0),
) -> Case:
    return Case(
        id=case_id if case_id is not None else CaseId(uuid7()),
        signal_id=SignalId(uuid7()),
        customer=make_customer_ref(),
        at_risk=at_risk,
        state=state,
        arm=arm,
        opened_at=EPOCH,
        cost_spent=cost_spent,
        cost_ceiling=cost_ceiling,
    )
