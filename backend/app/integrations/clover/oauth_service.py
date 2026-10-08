"""Browser authorization coordination; HTTP routes never handle token storage."""

import hashlib
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from app.core.config import OAuthConfiguration, SANDBOX_API_URL
from app.integrations.clover.client import CloverClient
from app.integrations.clover.exceptions import CloverUnavailableError
from app.integrations.clover.oauth_client import OAuthClient, OAuthExchangeError
from app.integrations.clover.token_provider import OAuthTokenProvider
from app.integrations.clover.token_store import TokenStore, TokenStoreError

AUTHORIZATION_WINDOW_SECONDS = 600
HttpClientFactory = Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]


def new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=False,
        trust_env=False,
    )


@dataclass(frozen=True)
class AuthorizationLaunch:
    url: str = field(repr=False)
    browser_nonce: str = field(repr=False)


class OAuthCallbackError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class OAuthService:
    def __init__(
        self,
        config: OAuthConfiguration,
        store: TokenStore,
        *,
        http_client_factory: HttpClientFactory = new_http_client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store
        self.http_client_factory = http_client_factory
        self.clock = clock
        self.token_provider = OAuthTokenProvider(
            identity=config.identity,
            store=store,
            oauth_client_factory=self.oauth_client,
            clock=clock,
        )

    @asynccontextmanager
    async def oauth_client(self) -> AsyncIterator[OAuthClient]:
        async with self.http_client_factory() as client:
            yield OAuthClient(
                client_id=self.config.identity.app_id,
                client_secret=self.config.app_secret,
                token_url=self.config.token_url,
                refresh_url=self.config.refresh_url,
                http_client=client,
                clock=self.clock,
            )

    async def launch(self) -> AuthorizationLaunch:
        state = secrets.token_urlsafe(32)
        browser_nonce = secrets.token_urlsafe(32)
        now = int(self.clock())
        await self.store.add_pending(
            state_hash=self._digest(state),
            nonce_hash=self._digest(browser_nonce),
            identity=self.config.identity,
            expires_at=now + AUTHORIZATION_WINDOW_SECONDS,
            now=now,
        )
        query = urlencode(
            {
                "client_id": self.config.identity.app_id,
                "response_type": "code",
                "redirect_uri": self.config.callback_url,
                "state": state,
            }
        )
        return AuthorizationLaunch(f"{self.config.authorize_url}?{query}", browser_nonce)

    async def complete_authorization(
        self,
        *,
        state: str | None,
        browser_nonce: str | None,
        code: str | None,
        merchant_id: str | None,
        client_id: str | None,
        error: str | None,
    ) -> None:
        if not state or not browser_nonce or len(state) > 256 or len(browser_nonce) > 256:
            raise OAuthCallbackError(400, "Invalid or expired Clover authorization")
        identity = self.config.identity
        if (merchant_id is not None and merchant_id != identity.merchant_id) or (
            client_id is not None and client_id != identity.app_id
        ):
            raise OAuthCallbackError(400, "Clover authorization identity does not match")
        if not error and (merchant_id is None or not code or len(code) > 4096):
            raise OAuthCallbackError(400, "Incomplete Clover authorization response")
        consumed = await self.store.consume_pending(
            state_hash=self._digest(state),
            nonce_hash=self._digest(browser_nonce),
            identity=identity,
            now=int(self.clock()),
        )
        if not consumed:
            raise OAuthCallbackError(400, "Invalid or expired Clover authorization")
        if error:
            raise OAuthCallbackError(400, "Clover authorization was declined")

        # Commit consumption before HTTP. Never retry a code after an ambiguous
        # exchange failure; the merchant must launch a new authorization.
        try:
            async with self.oauth_client() as oauth:
                tokens = await oauth.exchange_code(code)
            async with self.http_client_factory() as http:
                async with CloverClient(
                    base_url=SANDBOX_API_URL,
                    merchant_id=identity.merchant_id,
                    api_token=tokens.access_token.get_secret_value(),
                    http_client=http,
                ) as clover:
                    await clover.get_inventory_items(limit=1)
            tokens.validate_expirations(int(self.clock()))
        except (OAuthExchangeError, CloverUnavailableError, ValueError):
            raise OAuthCallbackError(502, "Clover connection could not be verified; reconnect") from None
        try:
            await self.store.save_connection(identity, tokens, int(self.clock()))
        except TokenStoreError:
            raise OAuthCallbackError(503, "Clover connection could not be saved; reconnect") from None

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
