"""Lazily build one token provider per application process."""

from fastapi import Depends, HTTPException, Request

from app.core.config import Settings, get_settings
from app.integrations.clover.exceptions import CloverConfigurationError
from app.integrations.clover.oauth_service import OAuthService
from app.integrations.clover.token_store import SQLiteTokenStore, TokenStoreError


def get_enabled_oauth_settings(settings: Settings = Depends(get_settings)) -> Settings:
    if not settings.clover_oauth_enabled:
        raise HTTPException(501, "Clover OAuth is not enabled")
    return settings


async def get_oauth_service(
    request: Request,
    settings: Settings = Depends(get_enabled_oauth_settings),
) -> OAuthService:
    try:
        config = settings.require_oauth_configuration()
        service = getattr(request.app.state, "clover_oauth_service", None)
        if service is None:
            # No await before assignment: all tasks share the provider/refresh
            # lock. The SQLite adapter opens lazily on its first operation.
            store = SQLiteTokenStore(
                config.database_path,
                config.encryption_key,
                app_env=settings.app_env,
                environment=settings.clover_env,
            )
            service = OAuthService(config, store)
            request.app.state.clover_oauth_service = service
        return service
    except (CloverConfigurationError, TokenStoreError):
        raise HTTPException(503, "Clover OAuth is not configured") from None
