"""Temporary development-only Clover inspection endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.menu import get_clover_client
from app.core.config import SANDBOX_API_URL, Settings, get_settings
from app.integrations.clover.client import CloverClient
from app.integrations.clover.exceptions import (
    CloverReconnectionRequiredError,
    CloverUnavailableError,
)
from app.integrations.clover.models import SafeOrderTypesResponse, normalize_order_types

router = APIRouter(prefix="/api/dev/clover", tags=["development"])


def require_development_sandbox(settings: Settings = Depends(get_settings)) -> None:
    if (
        settings.app_env != "development"
        or settings.clover_env != "sandbox"
        or str(settings.clover_base_url).rstrip("/") != SANDBOX_API_URL
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


@router.get(
    "/order-types",
    response_model=SafeOrderTypesResponse,
    dependencies=[Depends(require_development_sandbox)],
)
async def order_types(client: CloverClient = Depends(get_clover_client)) -> SafeOrderTypesResponse:
    try:
        types = await client.get_order_types()
    except CloverReconnectionRequiredError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Clover reconnection required"
        ) from None
    except CloverUnavailableError:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Clover order types are temporarily unavailable"
        ) from None
    return normalize_order_types(types)
