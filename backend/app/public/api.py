"""Customer-safe QR resolution, session bootstrap, and ordering endpoints."""

import asyncio
import time
from collections import deque
from typing import Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.menu import get_clover_client
from app.api.table_sessions import get_table_session_service
from app.core.config import Settings, get_settings
from app.integrations.clover.client import CloverClient
from app.orders.models import OrderLineRequest
from app.public.tokens import TableTokenError, TableTokenService, display_table
from app.table_sessions.models import AddSessionItemsRequest, SessionLineResponse
from app.table_sessions.service import TableSessionService

router = APIRouter(prefix="/api/public", tags=["public-ordering"])


class RateLimiter(Protocol):
    async def check(self, key: str) -> None: ...


class InProcessRateLimiter:
    """Small development gate; replace with shared proxy/Redis limits in production."""

    def __init__(self, limit: int = 120, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._visits: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = time.monotonic()
        async with self._lock:
            visits = self._visits.setdefault(key, deque())
            while visits and visits[0] <= now - self.window_seconds:
                visits.popleft()
            if len(visits) >= self.limit:
                raise HTTPException(429, "Please wait before trying again")
            visits.append(now)


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "public_rate_limiter", None)
    if limiter is None:
        limiter = InProcessRateLimiter()
        request.app.state.public_rate_limiter = limiter
    return limiter


async def rate_limit(
    request: Request, limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    await limiter.check(request.client.host if request.client else "unknown")


def resolve_table_token(settings: Settings, token: str) -> str:
    """Canonicalize transport encoding, verify claims, return a trusted table."""
    try:
        return TableTokenService(settings).resolve(token)
    except TableTokenError as exc:
        if str(exc) == "Table tokens are not configured":
            raise HTTPException(503, "Table ordering is unavailable") from None
        raise HTTPException(404, "Table link is invalid or no longer active") from None


class PublicTable(BaseModel):
    display_name: str


class PublicTableResolution(BaseModel):
    table: str
    display_name: str
    valid: bool = True


class PublicSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_token: str = Field(min_length=1, max_length=512)


class PublicSessionResponse(BaseModel):
    session_id: str
    table: PublicTable
    status: str


class PublicOrderRequest(PublicSessionRequest):
    items: list[OrderLineRequest] = Field(min_length=1, max_length=20)


class PublicOrderDetails(BaseModel):
    currency: str | None
    total: float | None
    items: list[SessionLineResponse]


class PublicOrderResponse(PublicSessionResponse):
    order: PublicOrderDetails


@router.get("/table/{token}", response_model=PublicTableResolution,
            dependencies=[Depends(rate_limit)])
async def table_resolution(
    token: str, settings: Settings = Depends(get_settings),
) -> PublicTableResolution:
    table = resolve_table_token(settings, token)
    return PublicTableResolution(table=table, display_name=display_table(table))


@router.post("/table-sessions", response_model=PublicSessionResponse,
             dependencies=[Depends(rate_limit)])
async def bootstrap_session(
    body: PublicSessionRequest,
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> PublicSessionResponse:
    table = resolve_table_token(settings, body.table_token)
    session = await service.create_or_resume(settings, table)
    return PublicSessionResponse(
        session_id=session.session_id, table=PublicTable(display_name=display_table(table)),
        status=session.status,
    )


@router.post("/table-sessions/{session_id}/orders", response_model=PublicOrderResponse,
             dependencies=[Depends(rate_limit)])
async def place_public_order(
    session_id: str, body: PublicOrderRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
    client: CloverClient = Depends(get_clover_client),
) -> PublicOrderResponse:
    table = resolve_table_token(settings, body.table_token)
    session = await service.get(settings, session_id)
    if session.table != table or session.status != "open":
        raise HTTPException(404, "Table session is unavailable")
    if idempotency_key is None:
        raise HTTPException(400, "Idempotency-Key is required")
    result = await service.add_items(
        settings=settings, session_id=session_id,
        body=AddSessionItemsRequest(items=body.items),
        key=idempotency_key, client=client,
    )
    return PublicOrderResponse(
        session_id=result.session_id,
        table=PublicTable(display_name=display_table(table)), status=result.status,
        order=PublicOrderDetails(
            currency=result.order.currency,
            total=float(result.order.total) if result.order.total is not None else None,
            items=result.order.items,
        ),
    )
