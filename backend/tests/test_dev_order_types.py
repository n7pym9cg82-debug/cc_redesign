"""Development order-type chooser: synthetic settings and mocked Clover only."""

import httpx
import pytest

from app.api.menu import get_clover_client
from app.core.config import Settings, get_settings
from app.integrations.clover.client import CloverClient
from app.integrations.clover.models import CloverOrderTypeListing
from app.main import app


async def open_order_types(path: str = "/api/dev/clover/order-types") -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


def mock_clover(payload: object, *, code: int = 200, provider=None):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(code, json=payload)

    async def dependency():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with CloverClient(
                base_url="https://apisandbox.dev.clover.com",
                merchant_id="trusted-merchant",
                api_token="synthetic-sandbox-token" if provider is None else None,
                token_provider=provider,
                http_client=http,
            ) as clover:
                yield clover

    app.dependency_overrides[get_clover_client] = dependency
    return requests


@pytest.mark.asyncio
async def test_order_types_are_normalized_and_trusted_merchant_is_used() -> None:
    requests = mock_clover({"elements": [
        {"id": "TYPE1", "label": "Dine In", "isDefault": True,
         "isHidden": False, "customerIdMethod": "TABLE", "secret": "do not expose"},
        {"id": "TYPE2", "label": "Takeout", "isHidden": True,
         "customerIdMethod": "NAME_TABLE", "attributes": {"private": True}},
        {"id": "DELETED", "label": "Old", "isDeleted": True},
        {"id": "DELETED2", "label": "Old 2", "deletedTime": 1},
    ], "private": "do not expose"})
    response = await open_order_types("/api/dev/clover/order-types?merchant_id=attacker")
    assert response.status_code == 200
    assert response.json() == {"order_types": [
        {"id": "TYPE1", "label": "Dine In", "is_default": True,
         "is_hidden": False, "customer_id_method": "TABLE"},
        {"id": "TYPE2", "label": "Takeout", "is_default": False,
         "is_hidden": True, "customer_id_method": "NAME_TABLE"},
    ]}
    assert response.headers["Cache-Control"] == "no-store"
    assert len(requests) == 1
    assert requests[0].url.path == "/v3/merchants/trusted-merchant/order_types"
    assert "attacker" not in str(requests[0].url)
    assert requests[0].headers["Authorization"] == "Bearer synthetic-sandbox-token"


@pytest.mark.asyncio
async def test_missing_optional_provider_fields_remain_safe() -> None:
    mock_clover({"elements": [{"id": "TYPE1"}]})
    response = await open_order_types()
    assert response.status_code == 200
    assert response.json() == {"order_types": [
        {"id": "TYPE1", "label": None, "is_default": False,
         "is_hidden": False, "customer_id_method": None}
    ]}


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [
    {"app_env": "production"},
    {"clover_env": "production"},
    {"clover_base_url": "https://api.clover.com"},
])
async def test_disabled_configuration_never_creates_clover_client(setting) -> None:
    invoked = False

    def forbidden_client():
        nonlocal invoked
        invoked = True
        raise AssertionError("Clover client must not be created")

    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, **setting)
    app.dependency_overrides[get_clover_client] = forbidden_client
    response = await open_order_types()
    assert response.status_code == 404
    assert invoked is False


@pytest.mark.asyncio
async def test_missing_credentials_are_sanitized() -> None:
    response = await open_order_types()
    assert response.status_code == 503
    assert response.json() == {"detail": "Clover integration is not configured"}


@pytest.mark.asyncio
async def test_real_dependency_uses_configured_merchant_not_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOVER_MERCHANT_ID", "configured-merchant")
    monkeypatch.setenv("CLOVER_API_TOKEN", "synthetic-token")

    async def fake_order_types(self: CloverClient):
        assert self.merchant_id == "configured-merchant"
        assert self._api_token == "synthetic-token"
        return [CloverOrderTypeListing(id="TYPE1", label="Synthetic")]

    monkeypatch.setattr(CloverClient, "get_order_types", fake_order_types)
    response = await open_order_types("/api/dev/clover/order-types?merchant_id=attacker")
    assert response.status_code == 200
    assert response.json()["order_types"][0]["id"] == "TYPE1"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,code", [
    ({"error": "private provider text"}, 500),
    ({"not_elements": []}, 200),
    ({"elements": [{"id": "bad id"}]}, 200),
])
async def test_upstream_errors_and_malformed_payload_are_sanitized(payload, code) -> None:
    mock_clover(payload, code=code)
    response = await open_order_types()
    assert response.status_code == 502
    assert response.json() == {"detail": "Clover order types are temporarily unavailable"}
    assert "private provider text" not in response.text


@pytest.mark.asyncio
async def test_oauth_provider_supplies_token_without_sandbox_fallback() -> None:
    class Provider:
        calls = 0

        async def get_access_token(self):
            self.calls += 1
            return "synthetic-oauth-token"

        async def invalidate(self):
            raise AssertionError("Unexpected invalidation")

    provider = Provider()
    requests = mock_clover({"elements": []}, provider=provider)
    response = await open_order_types()
    assert response.status_code == 200
    assert response.json() == {"order_types": []}
    assert provider.calls == 1
    assert requests[0].headers["Authorization"] == "Bearer synthetic-oauth-token"


@pytest.mark.asyncio
async def test_oauth_rejected_token_requires_reconnection() -> None:
    class Provider:
        invalidated = False

        async def get_access_token(self):
            return "synthetic-oauth-token"

        async def invalidate(self):
            self.invalidated = True

    provider = Provider()
    mock_clover({"error": "private"}, code=401, provider=provider)
    response = await open_order_types()
    assert response.status_code == 503
    assert response.json() == {"detail": "Clover reconnection required"}
    assert provider.invalidated is True
