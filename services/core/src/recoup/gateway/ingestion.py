"""Turns a Razorpay-shaped webhook payload into a durable, deduped
`RawEvent` row (TR-3, TR-4, TR-5; RAZORPAY-INTEGRATION SS4).

Shared by the webhook route (`recoup.api.routes.webhooks`) and the
`recoup events import` bulk-import command -- both need the same
"parse -> categorize -> durable write, deduped" path, and neither should
reimplement the other's copy of TR-3's replay-is-a-no-op guarantee.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from recoup.gateway.decline_taxonomy import categorize
from recoup.platform.clock import Clock
from recoup.platform.models import RawEvent

__all__ = [
    "RAZORPAY_SOURCE",
    "UnparseableEventError",
    "extract_decline_reason",
    "normalize_decline_category",
    "parse_razorpay_event",
    "store_raw_event",
]

RAZORPAY_SOURCE = "razorpay_webhook"


class UnparseableEventError(Exception):
    """The raw body is not a Razorpay-shaped event.

    Raised on invalid JSON or a missing/non-string top-level `event` field.
    TR-5 requires these be stored and flagged, not silently dropped -- the
    caller catches this and stores a flagged stub instead of the parsed
    fields it could not get."""


def parse_razorpay_event(raw_body: bytes) -> tuple[str, dict[str, Any]]:
    """Parse a Razorpay webhook envelope into `(event_type, full payload)`.

    Raises `UnparseableEventError` rather than returning a partial result --
    there is no safe default for "what event type was this" when the body
    does not parse."""
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise UnparseableEventError(str(exc)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
        raise UnparseableEventError("missing or non-string 'event' field")
    return payload["event"], payload


def extract_decline_reason(event_type: str, payload: dict[str, Any]) -> str | None:
    """Pull Razorpay's raw `error_reason` out of a payment-shaped envelope
    (`payload.payment.entity.error_reason`, RAZORPAY-INTEGRATION SS5).

    Structural, not gated on `event_type`: a `payment.*` event carries this
    shape, but so does `subscription.charged` when the underlying charge
    attempt against a mandate fails (RAZORPAY-INTEGRATION SS4.1's L2
    mapping) -- a charge attempt is still a `Payment` underneath. Only a
    failure actually populates `error_reason`; a successful envelope has
    the same shape with no error fields, which the defensive `.get`/
    `isinstance` chain here treats the same as "absent" rather than
    raising, for any event type that happens not to carry this shape at
    all (`subscription.halted` has no nested payment entity, for one).

    `event_type` stays a parameter even though it is now unused here --
    every other function in this module's `(event_type, payload)` shape
    is what callers already pass positionally, and dropping it here alone
    would be a needless asymmetry for the one caller unaffected by it.
    """
    payment = payload.get("payload")
    if not isinstance(payment, dict):
        return None
    entity_wrapper = payment.get("payment")
    if not isinstance(entity_wrapper, dict):
        return None
    entity = entity_wrapper.get("entity")
    if not isinstance(entity, dict):
        return None
    reason = entity.get("error_reason")
    return reason if isinstance(reason, str) else None


def normalize_decline_category(event_type: str, payload: dict[str, Any]) -> str | None:
    """The canonical `DeclineCategory` name for a payment-failure event, or
    `None` when there is no `error_reason` to categorize -- a non-payment
    event, or a payment event that is not a failure. Left `None` rather
    than `UNKNOWN` in that case: `UNKNOWN` means "a failure we could not
    classify," not "not applicable."""
    reason = extract_decline_reason(event_type, payload)
    if reason is None:
        return None
    return categorize(reason).name


async def store_raw_event(
    session: AsyncSession,
    clock: Clock,
    *,
    source: str,
    event_type: str,
    provider_event_id: str,
    payload: dict[str, Any],
) -> bool:
    """Durable, deduped write of one raw event (TR-3). Commits internally,
    so a caller that returns 200 after this awaits knows the write is
    durable (TR-2), not merely staged in an uncommitted transaction.

    Returns `True` if this call inserted a new row, `False` if the
    `provider_event_id` already existed and `ON CONFLICT DO NOTHING`
    made the insert a no-op -- i.e. a replay.
    """
    stmt = (
        pg_insert(RawEvent)
        .values(
            id=uuid4(),
            source=source,
            event_type=event_type,
            provider_event_id=provider_event_id,
            payload=payload,
            decline_category=normalize_decline_category(event_type, payload),
            received_at=clock.now(),
        )
        .on_conflict_do_nothing(index_elements=["provider_event_id"])
        .returning(RawEvent.id)
    )
    result = await session.execute(stmt)
    inserted = result.first() is not None
    await session.commit()
    return inserted
