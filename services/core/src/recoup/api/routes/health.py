"""Liveness and readiness, deliberately distinct (TR-71).

Liveness answers "is the process alive" and never touches a dependency -- an
orchestrator that conflates the two kills a healthy process during a brief
database blip. Readiness answers "can this process serve traffic right now"
and checks everything traffic would need.
"""

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from recoup.platform import cache, db
from recoup.platform.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyCheck(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class ReadyResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: list[ReadyCheck]


@router.get("/health/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse()


@router.get("/health/ready", response_model=ReadyResponse)
async def ready() -> ReadyResponse:
    checks = [await _check("database", db.ping), await _check("redis", cache.ping)]
    status: Literal["ok", "degraded"] = "ok" if all(c.ok for c in checks) else "degraded"
    return ReadyResponse(status=status, checks=checks)


async def _check(name: str, probe: Callable[[], Awaitable[bool]]) -> ReadyCheck:
    # Readiness must never 500 on a dependency outage -- a broad catch here is
    # the point, not an oversight: any failure means "not ready", full stop.
    try:
        await probe()
        return ReadyCheck(name=name, ok=True)
    except Exception as exc:
        logger.warning("readiness_check_failed", check=name, error=str(exc))
        return ReadyCheck(name=name, ok=False, detail=str(exc))
