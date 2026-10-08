"""Bounded Clover v2 token requests; never retry single-use credentials."""

import time
from collections.abc import Callable

import httpx
from pydantic import SecretStr, ValidationError

from app.integrations.clover.oauth_models import TokenPair


class OAuthExchangeError(Exception):
    """A token exchange failed without exposing provider or credential data."""


class OAuthClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: SecretStr,
        token_url: str,
        refresh_url: str,
        http_client: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._refresh_url = refresh_url
        self._http_client = http_client
        self._clock = clock

    async def exchange_code(self, code: str) -> TokenPair:
        if not code.strip():
            raise OAuthExchangeError("Clover authorization could not be completed")
        return await self._request_tokens(
            self._token_url,
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret.get_secret_value(),
                "code": code,
            },
        )

    async def refresh(self, refresh_token: SecretStr) -> TokenPair:
        if not refresh_token.get_secret_value().strip():
            raise OAuthExchangeError("Clover authorization could not be completed")
        # Clover's official refresh example sends client_id + refresh_token;
        # client_secret belongs only to the initial authorization-code exchange.
        return await self._request_tokens(
            self._refresh_url,
            {
                "client_id": self._client_id,
                "refresh_token": refresh_token.get_secret_value(),
            },
        )

    async def _request_tokens(self, url: str, payload: dict[str, str]) -> TokenPair:
        try:
            response = await self._http_client.post(
                url,
                json=payload,
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
            )
            response.raise_for_status()
            tokens = TokenPair.model_validate(response.json())
            tokens.validate_expirations(int(self._clock()))
        except (httpx.HTTPError, ValueError, ValidationError):
            # HTTP and validation errors can contain credentials or raw bodies.
            raise OAuthExchangeError(
                "Clover authorization could not be completed"
            ) from None
        return tokens
