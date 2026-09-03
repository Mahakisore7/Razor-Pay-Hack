"""Pure domain model: entities, value objects, state machines.

No I/O, no framework imports, no clock access, no randomness. Everything here
is a function of its inputs, which is what makes the pipeline replayable."""

from recoup.domain.identifiers import (
    ActionId,
    AuditEventId,
    CaseId,
    CustomerRef,
    SignalId,
    hash_contact,
    uuid7,
)
from recoup.domain.money import Currency, Money

__all__ = [
    "ActionId",
    "AuditEventId",
    "CaseId",
    "Currency",
    "CustomerRef",
    "Money",
    "SignalId",
    "hash_contact",
    "uuid7",
]
