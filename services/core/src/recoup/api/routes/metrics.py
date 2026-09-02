"""Prometheus exposition endpoint.

Deliberately network-restricted in deployment (SECURITY section 5), not
authenticated here -- that boundary is enforced at the reverse proxy / network
layer, not in application code.
"""

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["observability"])


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
