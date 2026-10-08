import asyncio
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.config import Settings, get_settings
from app.integrations.clover.client import CloverClient
from app.integrations.clover.dependencies import get_oauth_service
from app.integrations.clover.exceptions import (
    CloverConfigurationError,
    CloverReconnectionRequiredError,
    CloverUnavailableError,
)
from app.menu_import.catalog import MenuResponse, load_catalog

router = APIRouter(prefix="/api", tags=["menu"])


async def get_clover_client(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> AsyncIterator[CloverClient]:
    if settings.clover_auth_mode == "oauth":
        service = await get_oauth_service(request, settings)
        async with service.http_client_factory() as http:
            async with CloverClient(
                base_url=str(settings.clover_base_url),
                merchant_id=service.config.identity.merchant_id,
                http_client=http,
                token_provider=service.token_provider,
            ) as client:
                yield client
        return
    try:
        merchant_id, api_token = settings.require_inventory_credentials()
    except (ValueError, CloverConfigurationError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Clover integration is not configured",
        ) from None

    async with CloverClient(
        base_url=str(settings.clover_base_url),
        merchant_id=merchant_id,
        api_token=api_token,
    ) as client:
        yield client


class MenuCache:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._value: MenuResponse | None = None
        self._expires_at = 0.0
        self._scope: tuple[str, str] | None = None

    async def get(self, client: CloverClient, settings: Settings) -> MenuResponse:
        scope = (client.merchant_id, settings.clover_auth_mode)
        now = time.monotonic()
        if self._value is not None and self._scope == scope and now < self._expires_at:
            return self._value
        async with self._lock:
            now = time.monotonic()
            if self._value is not None and self._scope == scope and now < self._expires_at:
                return self._value
            value = await load_catalog(client)
            self._value = value
            self._scope = scope
            self._expires_at = time.monotonic() + settings.menu_cache_ttl_seconds
            return value

    def cached_item_names(self, client: CloverClient, settings: Settings) -> dict[str, str]:
        """Read only the current merchant's already-normalized catalog; never fetch."""
        if (self._value is None or
                self._scope != (client.merchant_id, settings.clover_auth_mode) or
                time.monotonic() >= self._expires_at):
            return {}
        return {
            item.id: item.name
            for category in self._value.categories for item in category.items
        }


@router.get("/menu", response_model=MenuResponse)
async def menu(request: Request, client: CloverClient = Depends(get_clover_client),
               settings: Settings = Depends(get_settings)) -> MenuResponse:
    cache = getattr(request.app.state, "menu_cache", None)
    if cache is None:
        cache = MenuCache()
        request.app.state.menu_cache = cache
    try:
        return await cache.get(client, settings)
    except CloverReconnectionRequiredError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Clover reconnection required",
        ) from None
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Menu is temporarily unavailable",
        ) from None

