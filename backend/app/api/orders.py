"""Validated, idempotent Clover Sandbox order endpoint."""

import threading

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.api.menu import get_clover_client
from app.core import config as config_module
from app.core.config import Settings, get_settings
from app.integrations.clover.client import CloverClient
from app.orders.idempotency import SQLiteOrderLedger
from app.orders.models import OrderRequest, OrderResponse
from app.orders.service import OrderService

router = APIRouter(prefix="/api", tags=["orders"])
_service_guard = threading.Lock()


def get_order_service(request: Request) -> OrderService:
    with _service_guard:
        service = getattr(request.app.state, "order_service", None)
        if service is None:
            service = OrderService(SQLiteOrderLedger(config_module.LOCAL_ORDER_DATABASE))
            request.app.state.order_service = service
        return service


@router.post("/orders", response_model=OrderResponse, status_code=201)
async def create_order(
    order: OrderRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    client: CloverClient = Depends(get_clover_client),
    settings: Settings = Depends(get_settings),
    service: OrderService = Depends(get_order_service),
) -> OrderResponse:
    if idempotency_key is None:
        raise HTTPException(400, "Idempotency-Key is required")
    return await service.create(request=order, key=idempotency_key, client=client, settings=settings)
