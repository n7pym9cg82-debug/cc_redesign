"""Restaurant table sessions backed by one running Clover order per session."""

import threading

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.api.menu import MenuCache, get_clover_client
from app.api.orders import get_order_service
from app.core import config as config_module
from app.core.config import Settings, get_settings
from app.integrations.clover.client import CloverClient
from app.table_sessions.models import (
    AddSessionItemsRequest,
    CreateTableSessionRequest,
    SessionItemsResponse,
    TableSessionResponse,
)
from app.table_sessions.service import TableSessionService
from app.table_sessions.store import SQLiteTableSessionRepository

router = APIRouter(prefix="/api/table-sessions", tags=["table-sessions"])
_service_guard = threading.Lock()


def get_table_session_service(request: Request) -> TableSessionService:
    with _service_guard:
        service = getattr(request.app.state, "table_session_service", None)
        if service is None:
            cache = getattr(request.app.state, "menu_cache", None)
            if cache is None:
                cache = MenuCache()
                request.app.state.menu_cache = cache
            service = TableSessionService(
                SQLiteTableSessionRepository(config_module.LOCAL_TABLE_SESSION_DATABASE),
                get_order_service(request),
                menu_cache=cache,
            )
            request.app.state.table_session_service = service
        return service


@router.post("", response_model=TableSessionResponse)
async def create_or_resume_table_session(
    body: CreateTableSessionRequest,
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> TableSessionResponse:
    return await service.create_or_resume(settings, body.table)


@router.get("/{session_id}", response_model=TableSessionResponse)
async def get_table_session(
    session_id: str,
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> TableSessionResponse:
    return await service.get(settings, session_id)


@router.post("/{session_id}/items", response_model=SessionItemsResponse)
async def add_table_session_items(
    session_id: str,
    body: AddSessionItemsRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    settings: Settings = Depends(get_settings),
    client: CloverClient = Depends(get_clover_client),
    service: TableSessionService = Depends(get_table_session_service),
) -> SessionItemsResponse:
    if idempotency_key is None:
        raise HTTPException(400, "Idempotency-Key is required")
    return await service.add_items(
        settings=settings, session_id=session_id, body=body,
        key=idempotency_key, client=client,
    )
