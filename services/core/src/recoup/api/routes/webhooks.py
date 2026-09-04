"""Razorpay webhook ingestion (TR-1..TR-5, RAZORPAY-INTEGRATION SS4).

Unauthenticated by design (API-SPEC SS2.1) -- authenticity comes from the
HMAC signature, not a bearer token. The raw body is read and the signature
verified before the JSON parser ever touches it (TR-1): a framework that
parses and re-serialises a body changes byte order and key spacing, so the
signature would never match a re-dump.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from recoup.gateway.ingestion import (
    RAZORPAY_SOURCE,
    UnparseableEventError,
    parse_razorpay_event,
    store_raw_event,
)
from recoup.platform.clock import Clock, get_clock
from recoup.platform.config import get_settings
from recoup.platform.db import get_session
from recoup.platform.logging import get_logger

router = APIRouter(tags=["webhooks"])
logger = get_logger(__name__)

_SIGNATURE_HEADER = "x-razorpay-signature"

# Subscribed events (RAZORPAY-INTEGRATION SS4.1). Not a filter -- every
# event is stored regardless (TR-5) -- this only flags one Recoup does not
# yet expect, so it is findable without grepping raw payloads by hand.
_SUBSCRIBED_EVENTS = frozenset(
    {
        "payment.failed",
        "payment.captured",
        "payment.authorized",
        "order.paid",
        "payment_link.paid",
        "payment_link.expired",
        "subscription.charged",
        "subscription.pending",
        "subscription.halted",
        "subscription.cancelled",
        "subscription.completed",
        "invoice.paid",
        "invoice.expired",
        "payment.dispute.created",
        "refund.created",
    }
)


@router.post("/webhooks/razorpay", status_code=200)
async def receive_razorpay_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> Response:
    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get(_SIGNATURE_HEADER))

    # A stable dedup key that needs nothing Razorpay might not send: their
    # webhook body carries no delivery-level id, only resource ids that
    # repeat across an object's lifecycle, so a redelivery of the same
    # bytes is what "duplicate" means here (TR-3).
    provider_event_id = hashlib.sha256(raw_body).hexdigest()

    try:
        event_type, payload = parse_razorpay_event(raw_body)
    except UnparseableEventError as exc:
        logger.warning("webhook_unparseable", error=str(exc))
        event_type = "_unparseable"
        payload = {
            "_ingestion_error": "unparseable_json",
            "raw_body_base64": base64.b64encode(raw_body).decode("ascii"),
        }

    if event_type not in _SUBSCRIBED_EVENTS:
        logger.warning("webhook_unrecognized_event_type", event_type=event_type)

    inserted = await store_raw_event(
        session,
        clock,
        source=RAZORPAY_SOURCE,
        event_type=event_type,
        provider_event_id=provider_event_id,
        payload=payload,
    )
    logger.info(
        "webhook_receive",
        provider="razorpay",
        event_type=event_type,
        duplicate=not inserted,
    )
    return Response(status_code=200)


def _verify_signature(raw_body: bytes, signature: str | None) -> None:
    secret = get_settings().razorpay_webhook_secret
    expected = (
        hmac.new(secret.get_secret_value().encode(), raw_body, hashlib.sha256).hexdigest()
        if secret is not None
        else None
    )
    # One uniform rejection whether the secret is unconfigured, the header
    # is absent, or the digest mismatches -- distinguishing those responses
    # would tell a caller something about server state (API-SPEC SS2.1: a
    # verbose signature error is an oracle).
    if expected is None or signature is None or not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=400, detail="invalid signature")
