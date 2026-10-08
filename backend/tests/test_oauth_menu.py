"""Exercise the real menu dependency with an encrypted temp store and mocked HTTP."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.core.config import get_settings
from app.integrations.clover.oauth_models import TokenPair
from app.integrations.clover.oauth_service import OAuthService
from app.integrations.clover.token_store import SQLiteTokenStore
from app.main import app
from tests.test_oauth_config import configured_settings

NOW = 2_000_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "refresh", "refresh_failure", "missing", "revoked"])
async def test_oauth_menu_authentication_and_concurrent_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    settings = configured_settings(
        clover_auth_mode="oauth",
        clover_oauth_enabled=False,
        clover_api_token="synthetic-forbidden-sandbox-fallback",
    )
    config = settings.require_oauth_configuration()
    store = SQLiteTokenStore(
        tmp_path / "menu.sqlite3", config.encryption_key,
        app_env="development", environment="sandbox",
    )
    original = TokenPair(
        access_token="synthetic-original-access",
        refresh_token="synthetic-original-refresh",
        access_token_expiration=NOW + (30 if case.startswith("refresh") else 300),
        refresh_token_expiration=NOW + 3600,
    )
    if case != "missing":
        await store.save_connection(config.identity, original, NOW)
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "apisandbox.dev.clover.com"
        if request.url.path == "/oauth/v2/refresh":
            assert json.loads(request.content) == {
                "client_id": "synthetic-app", "refresh_token": "synthetic-original-refresh"
            }
            await asyncio.sleep(0)
            if case == "refresh_failure":
                raise httpx.ReadTimeout("synthetic-sensitive-provider-text", request=request)
            return httpx.Response(200, json={
                "access_token": "synthetic-rotated-access",
                "refresh_token": "synthetic-rotated-refresh",
                "access_token_expiration": NOW + 900,
                "refresh_token_expiration": NOW + 7200,
            })
        assert request.url.path in {
            "/v3/merchants/synthetic-merchant/categories",
            "/v3/merchants/synthetic-merchant/items",
            "/v3/merchants/synthetic-merchant/modifier_groups",
        }
        expected = "synthetic-rotated-access" if case == "refresh" else "synthetic-original-access"
        assert request.headers["authorization"] == f"Bearer {expected}"
        if case == "revoked":
            return httpx.Response(401, json={"error": "synthetic-sensitive-provider-text"})
        if request.url.path.endswith("/categories"):
            return httpx.Response(200, json={"elements": [{"id": "C1", "name": "Biryani"}]})
        if request.url.path.endswith("/modifier_groups"):
            return httpx.Response(200, json={"elements": []})
        return httpx.Response(200, json={"elements": [{
            "id": "ABC123", "name": "Chicken Biryani", "price": 1699,
            "categories": {"elements": [{"id": "C1"}]},
            "modifierGroups": {"elements": []},
        }]})

    service = OAuthService(
        config, store,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        clock=lambda: NOW,
    )
    monkeypatch.setattr(app.state, "clover_oauth_service", service)
    app.dependency_overrides[get_settings] = lambda: settings
    count = 8 if case.startswith("refresh") else 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://restaurant.example.test"
    ) as browser:
        responses = await asyncio.gather(*(browser.get("/api/menu") for _ in range(count)))
        if case in ("refresh_failure", "missing", "revoked"):
            # A second request must not retry an ambiguous refresh or use the
            # separately configured sandbox token after rejection.
            responses.append(await browser.get("/api/menu"))

    if case in ("valid", "refresh"):
        assert all(response.status_code == 200 for response in responses)
        assert responses[0].json()["categories"][0]["items"][0]["id"] == "ABC123"
    else:
        assert all(response.status_code == 503 for response in responses)
        assert all(response.json() == {"detail": "Clover reconnection required"} for response in responses)

    assert sum(request.method == "POST" for request in requests) == int(case.startswith("refresh"))
    assert sum(request.method == "GET" for request in requests) == (
        3 if case in ("valid", "refresh") else 1 if case == "revoked" else 0
    )
    stored = await store.load_connection(config.identity)
    if case == "refresh":
        assert stored.tokens.access_token.get_secret_value() == "synthetic-rotated-access"
        assert stored.tokens.refresh_token.get_secret_value() == "synthetic-rotated-refresh"
        assert stored.tokens.access_token_expiration == NOW + 900
        assert stored.tokens.refresh_token_expiration == NOW + 7200
    elif case == "refresh_failure":
        assert stored.status == "reconnect_required"
        assert stored.tokens == original


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/clover/callback/", "/clover/launch/"])
async def test_oauth_slash_variants_cannot_redirect_to_untrusted_host(path: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://restaurant.example.test"
    ) as browser:
        response = await browser.get(
            path, params={"code": "synthetic-code", "state": "synthetic-state"},
            headers={"host": "evil.example.test"},
        )
    assert response.status_code == 404
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"
