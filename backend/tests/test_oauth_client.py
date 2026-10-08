import json

import httpx
import pytest
from pydantic import SecretStr

from app.integrations.clover.oauth_client import OAuthClient, OAuthExchangeError


NOW = 2_000_000_000
TOKEN_URL = "https://apisandbox.dev.clover.com/oauth/v2/token"
REFRESH_URL = "https://apisandbox.dev.clover.com/oauth/v2/refresh"


def token_response(**changes: object) -> dict[str, object]:
    return {
        "access_token": "synthetic-new-access",
        "refresh_token": "synthetic-new-refresh",
        "access_token_expiration": NOW + 300,
        "refresh_token_expiration": NOW + 900,
        **changes,
    }


def oauth_client(http_client: httpx.AsyncClient) -> OAuthClient:
    return OAuthClient(
        client_id="synthetic-app",
        client_secret=SecretStr("synthetic-app-secret"),
        token_url=TOKEN_URL,
        refresh_url=REFRESH_URL,
        http_client=http_client,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_code_exchange_uses_official_json_contract_and_bounded_timeout() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == TOKEN_URL
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "client_id": "synthetic-app",
            "client_secret": "synthetic-app-secret",
            "code": "synthetic-code",
        }
        assert request.extensions["timeout"] == {
            "connect": 5.0, "read": 10.0, "write": 10.0, "pool": 10.0
        }
        return httpx.Response(200, json=token_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        tokens = await oauth_client(http).exchange_code("synthetic-code")
    assert len(requests) == 1
    assert tokens.access_token.get_secret_value() == "synthetic-new-access"
    assert tokens.refresh_token.get_secret_value() == "synthetic-new-refresh"
    assert tokens.access_token_expiration == NOW + 300
    assert tokens.refresh_token_expiration == NOW + 900


@pytest.mark.asyncio
async def test_refresh_sends_only_app_id_and_refresh_token() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == REFRESH_URL
        assert json.loads(request.content) == {
            "client_id": "synthetic-app", "refresh_token": "synthetic-old-refresh"
        }
        return httpx.Response(200, json=token_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await oauth_client(http).refresh(SecretStr("synthetic-old-refresh"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        token_response(access_token=""),
        token_response(refresh_token=" \t"),
        token_response(access_token_expiration="2000000300"),
        token_response(access_token_expiration=True),
        token_response(access_token_expiration=NOW),
        token_response(refresh_token_expiration=NOW - 1),
        token_response(refresh_token_expiration=NOW + 100),
    ],
)
async def test_invalid_token_responses_are_sanitized(payload: object) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(OAuthExchangeError) as caught:
            await oauth_client(http).exchange_code("synthetic-code")
    assert str(caught.value) == "Clover authorization could not be completed"
    assert caught.value.__suppress_context__


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "http_error", "redirect", "invalid_json"])
async def test_token_failure_never_retries_or_follows_redirects(failure: str) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic-secret-provider-text", request=request)
        if failure == "http_error":
            return httpx.Response(400, text="synthetic-secret-provider-text")
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://unexpected.invalid/"})
        return httpx.Response(200, content=b"synthetic-secret-provider-text")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as http:
        with pytest.raises(OAuthExchangeError) as caught:
            await oauth_client(http).exchange_code("synthetic-code")
    assert calls == 1
    assert "synthetic" not in str(caught.value)
    assert caught.value.__suppress_context__
