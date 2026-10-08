"""Access-token selection and single-process, durable refresh coordination."""

import asyncio
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from typing import NoReturn

from app.integrations.clover.exceptions import CloverReconnectionRequiredError
from app.integrations.clover.oauth_client import OAuthClient
from app.integrations.clover.oauth_models import OAuthIdentity, StoredConnection
from app.integrations.clover.token_store import TokenStore, TokenStoreError


class OAuthTokenProvider:
    REFRESH_LEEWAY_SECONDS = 60

    def __init__(
        self,
        *,
        identity: OAuthIdentity,
        store: TokenStore,
        oauth_client_factory: Callable[[], AbstractAsyncContextManager[OAuthClient]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._identity = identity
        self._store = store
        self._oauth_client_factory = oauth_client_factory
        self._clock = clock
        self._refresh_lock = asyncio.Lock()
        # Inventory failures only invalidate the generation used by that task,
        # never a newer connection saved by another callback or refresh.
        self._issued_version: ContextVar[int | None] = ContextVar(
            "clover_oauth_issued_version", default=None
        )

    async def get_access_token(self) -> str:
        self._issued_version.set(None)
        try:
            current = await self._store.load_connection(self._identity)
            if self._usable(current):
                return self._issue(current)

            async with self._refresh_lock:
                # Another request may have rotated the tokens while we waited.
                current = await self._store.load_connection(self._identity)
                if self._usable(current):
                    return self._issue(current)
                if current is None:
                    self._reconnection_required()
                now = int(self._clock())
                if current.status != "active":
                    # A durable "refreshing" marker on restart means a request
                    # might have consumed this single-use refresh token.
                    if current.status == "refreshing":
                        await self._store.mark_reconnect_required(
                            self._identity, current.version, now
                        )
                    self._reconnection_required()
                if current.tokens.refresh_token_expiration <= now:
                    await self._store.mark_reconnect_required(
                        self._identity, current.version, now
                    )
                    self._reconnection_required()
                claimed = await self._store.mark_refreshing(
                    self._identity, current.version, now
                )
                if not claimed:
                    return await self._issue_latest_or_reconnect()
                try:
                    async with self._oauth_client_factory() as client:
                        tokens = await client.refresh(current.tokens.refresh_token)
                except Exception:
                    # An ambiguous failure is terminal for this generation.
                    # No automatic retry, including on the next menu request.
                    await self._store.mark_reconnect_required(
                        self._identity, current.version, int(self._clock())
                    )
                    self._reconnection_required()
                await self._store.save_connection(
                    self._identity,
                    tokens,
                    int(self._clock()),
                    expected_version=current.version,
                )
                # Always reread; a concurrent callback can replace this record.
                # A failed CAS must never overwrite that newer connection.
                return await self._issue_latest_or_reconnect()
        except TokenStoreError:
            self._reconnection_required()

    async def invalidate(self) -> None:
        version = self._issued_version.get()
        self._issued_version.set(None)
        if version is None:
            return
        try:
            async with self._refresh_lock:
                await self._store.mark_reconnect_required(
                    self._identity, version, int(self._clock())
                )
        except TokenStoreError:
            self._reconnection_required()

    def _usable(
        self, connection: StoredConnection | None, *, leeway: int = REFRESH_LEEWAY_SECONDS
    ) -> bool:
        return (
            connection is not None
            and connection.status == "active"
            and connection.tokens.access_token_expiration
            > int(self._clock()) + leeway
        )

    def _issue(self, connection: StoredConnection) -> str:
        self._issued_version.set(connection.version)
        return connection.tokens.access_token.get_secret_value()

    async def _issue_latest_or_reconnect(self) -> str:
        latest = await self._store.load_connection(self._identity)
        if self._usable(latest, leeway=0):
            return self._issue(latest)
        self._reconnection_required()

    @staticmethod
    def _reconnection_required() -> NoReturn:
        raise CloverReconnectionRequiredError("Clover reconnection is required") from None
