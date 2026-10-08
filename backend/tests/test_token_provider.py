import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.integrations.clover.exceptions import CloverReconnectionRequiredError
from app.integrations.clover.oauth_client import OAuthClient
from app.integrations.clover.oauth_models import OAuthIdentity, TokenPair
from app.integrations.clover.token_provider import OAuthTokenProvider
from app.integrations.clover.token_store import SQLiteTokenStore


NOW = 2_000_000_000
IDENTITY = OAuthIdentity("synthetic-merchant", "synthetic-app", "sandbox")
SYNTHETIC_KEY = SecretStr(Fernet.generate_key().decode("ascii"))


def token_pair(*, access_lifetime: int = 30, prefix: str = "old") -> TokenPair:
    return TokenPair(
        access_token=f"synthetic-{prefix}-access",
        refresh_token=f"synthetic-{prefix}-refresh",
        access_token_expiration=NOW + access_lifetime,
        refresh_token_expiration=NOW + 900,
    )


def new_tokens() -> dict[str, object]:
    return {
        "access_token": "synthetic-new-access",
        "refresh_token": "synthetic-new-refresh",
        "access_token_expiration": NOW + 400,
        "refresh_token_expiration": NOW + 1600,
    }


@pytest.fixture
def store(tmp_path: Path) -> SQLiteTokenStore:
    return SQLiteTokenStore(
        tmp_path / "oauth.sqlite3", SYNTHETIC_KEY,
        app_env="development", environment="sandbox",
    )


def provider_for(store: SQLiteTokenStore, respond, *, clock=lambda: NOW) -> OAuthTokenProvider:
    @asynccontextmanager
    async def client_factory():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            yield OAuthClient(
                client_id=IDENTITY.app_id,
                client_secret=SecretStr("synthetic-app-secret"),
                token_url="https://apisandbox.dev.clover.com/oauth/v2/token",
                refresh_url="https://apisandbox.dev.clover.com/oauth/v2/refresh",
                http_client=http,
                clock=clock,
            )

    return OAuthTokenProvider(
        identity=IDENTITY, store=store,
        oauth_client_factory=client_factory, clock=clock,
    )


def reject_http(request: httpx.Request) -> httpx.Response:
    pytest.fail("No token request was expected")


@pytest.mark.asyncio
async def test_valid_stored_token_does_not_refresh(store: SQLiteTokenStore) -> None:
    await store.save_connection(IDENTITY, token_pair(access_lifetime=300), NOW)
    assert await provider_for(store, reject_http).get_access_token() == "synthetic-old-access"


@pytest.mark.asyncio
async def test_refresh_replaces_both_tokens_and_expirations(store: SQLiteTokenStore) -> None:
    await store.save_connection(IDENTITY, token_pair(), NOW)
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert json.loads(request.content)["refresh_token"] == "synthetic-old-refresh"
        during_request = await store.load_connection(IDENTITY)
        assert during_request.status == "refreshing"
        return httpx.Response(200, json=new_tokens())

    assert await provider_for(store, respond).get_access_token() == "synthetic-new-access"
    saved = await store.load_connection(IDENTITY)
    assert saved.status == "active"
    assert saved.version == 2
    assert saved.tokens.access_token.get_secret_value() == "synthetic-new-access"
    assert saved.tokens.refresh_token.get_secret_value() == "synthetic-new-refresh"
    assert saved.tokens.access_token_expiration == NOW + 400
    assert saved.tokens.refresh_token_expiration == NOW + 1600
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_token_requests_refresh_once(store: SQLiteTokenStore) -> None:
    await store.save_connection(IDENTITY, token_pair(), NOW)
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return httpx.Response(200, json=new_tokens())

    provider = provider_for(store, respond)
    results = await asyncio.gather(*(provider.get_access_token() for _ in range(12)))
    assert results == ["synthetic-new-access"] * 12
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "revoked", "partial"])
async def test_refresh_failure_requires_reconnection_without_retry_or_fallback(
    store: SQLiteTokenStore, failure: str, caplog: pytest.LogCaptureFixture,
) -> None:
    await store.save_connection(IDENTITY, token_pair(), NOW)
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic-secret-error", request=request)
        if failure == "revoked":
            return httpx.Response(401, text="synthetic-secret-error")
        return httpx.Response(200, json={"access_token": "synthetic-partial-token"})

    provider = provider_for(store, respond)
    for _ in range(2):
        with pytest.raises(CloverReconnectionRequiredError) as caught:
            await provider.get_access_token()
        assert str(caught.value) == "Clover reconnection is required"
        assert caught.value.__suppress_context__
    # A new process/provider must not retry the potentially consumed token either.
    with pytest.raises(CloverReconnectionRequiredError):
        await provider_for(store, respond).get_access_token()
    saved = await store.load_connection(IDENTITY)
    assert saved.status == "reconnect_required"
    assert saved.tokens == token_pair()
    assert calls == 1
    assert "synthetic-" not in caplog.text


@pytest.mark.asyncio
async def test_restart_during_ambiguous_refresh_requires_reconnection(store: SQLiteTokenStore) -> None:
    await store.save_connection(IDENTITY, token_pair(), NOW)
    await store.mark_refreshing(IDENTITY, 1, NOW)
    with pytest.raises(CloverReconnectionRequiredError):
        await provider_for(store, reject_http).get_access_token()
    assert (await store.load_connection(IDENTITY)).status == "reconnect_required"


@pytest.mark.asyncio
async def test_missing_connection_requires_reconnection(store: SQLiteTokenStore) -> None:
    with pytest.raises(CloverReconnectionRequiredError):
        await provider_for(store, reject_http).get_access_token()


@pytest.mark.asyncio
async def test_expired_refresh_token_is_not_sent(store: SQLiteTokenStore) -> None:
    await store.save_connection(IDENTITY, token_pair(), NOW)
    with pytest.raises(CloverReconnectionRequiredError):
        await provider_for(store, reject_http, clock=lambda: NOW + 901).get_access_token()
    assert (await store.load_connection(IDENTITY)).status == "reconnect_required"


@pytest.mark.asyncio
async def test_new_callback_connection_wins_over_inflight_refresh(store: SQLiteTokenStore) -> None:
    await store.save_connection(IDENTITY, token_pair(), NOW)

    async def respond(request: httpx.Request) -> httpx.Response:
        await store.save_connection(IDENTITY, token_pair(access_lifetime=300, prefix="callback"), NOW)
        return httpx.Response(200, json=new_tokens())

    assert await provider_for(store, respond).get_access_token() == "synthetic-callback-access"
    saved = await store.load_connection(IDENTITY)
    assert saved.tokens.refresh_token.get_secret_value() == "synthetic-callback-refresh"
    assert saved.version == 2


@pytest.mark.asyncio
async def test_invalidation_does_not_revoke_a_new_connection(store: SQLiteTokenStore) -> None:
    await store.save_connection(IDENTITY, token_pair(access_lifetime=300), NOW)
    provider = provider_for(store, reject_http)
    await provider.get_access_token()
    await store.save_connection(IDENTITY, token_pair(access_lifetime=300, prefix="callback"), NOW)
    await provider.invalidate()
    assert (await store.load_connection(IDENTITY)).status == "active"
    assert await provider.get_access_token() == "synthetic-callback-access"


@pytest.mark.asyncio
async def test_rejected_inventory_token_marks_its_generation_for_reconnection(store: SQLiteTokenStore) -> None:
    await store.save_connection(IDENTITY, token_pair(access_lifetime=300), NOW)
    provider = provider_for(store, reject_http)
    await provider.get_access_token()
    await provider.invalidate()
    assert (await store.load_connection(IDENTITY)).status == "reconnect_required"
    with pytest.raises(CloverReconnectionRequiredError):
        await provider.get_access_token()
