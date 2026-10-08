"""No real credentials, runtime database, or Clover network access."""

import asyncio
import json

import httpx
import pytest

from app.api.menu import get_clover_client
from app.core.config import Settings, get_settings
from app.integrations.clover.client import CloverClient
from app.orders.idempotency import SQLiteOrderLedger
from app.main import app

BODY = {"table": " T12 ", "items": [{"item_id": "ITEM1", "quantity": 2}]}
HEADERS = {"Idempotency-Key": "synthetic-key-123"}


@pytest.fixture
def clover_mock():
    calls: list[httpx.Request] = []
    responses: dict[str, object] = {
        "inventory": {"elements": [
            {"id": "ITEM1", "name": "Synthetic Soup", "price": 1099,
             "available": True, "priceType": "FIXED", "modifierGroups": {"elements": []}}
        ]},
        "order_type": {"id": "TYPE1"},
        "order": {"id": "ORDER1", "currency": "USD", "subtotal": 2198,
                  "totalTaxAmount": 176, "total": 2374},
        "delay": False,
        "omit_modifier_readback": False,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET" and request.url.path.endswith("/items"):
            return httpx.Response(200, json=responses["inventory"])
        if request.method == "GET" and request.url.path.endswith("/modifier_groups"):
            return httpx.Response(200, json={"elements": [
                {"id": "G1", "name": "Soup Style", "minRequired": 1, "maxAllowed": 1}
            ]})
        if request.method == "GET" and request.url.path.endswith("/modifier_groups/G1/modifiers"):
            return httpx.Response(200, json={"elements": [
                {"id": "M1", "name": "Mild", "price": 25},
                {"id": "M2", "name": "Spicy", "price": 50},
            ]})
        if request.method == "GET" and request.url.path.endswith("/order_types/TYPE1"):
            return httpx.Response(200, json=responses["order_type"])
        if request.method == "POST" and request.url.path.endswith("/atomic_order/orders"):
            if responses["delay"]:
                await asyncio.sleep(0.05)
            value = responses["order"]
            if isinstance(value, Exception):
                raise value
            if isinstance(value, int):
                return httpx.Response(value, json={"error": "secret provider detail"})
            if not responses["omit_modifier_readback"] and any("modifications" in line for line in json.loads(request.content)["orderCart"]["lineItems"]):
                value = {**value, "orderCart": {"lineItems": json.loads(request.content)["orderCart"]["lineItems"]}}
            return httpx.Response(200, json=value)
        raise AssertionError("Unexpected Clover request")

    async def dependency():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with CloverClient(
                base_url="https://apisandbox.dev.clover.com",
                merchant_id="synthetic-merchant",
                api_token="synthetic-token",
                http_client=http,
            ) as client:
                yield client

    app.dependency_overrides[get_clover_client] = dependency
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, clover_order_type_id="TYPE1"
    )
    return calls, responses


async def post(body=BODY, headers=HEADERS):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/api/orders", json=body, headers=headers)


@pytest.mark.asyncio
async def test_atomic_order_uses_inventory_prices_and_repeated_lines(clover_mock) -> None:
    calls, _ = clover_mock
    response = await post()
    assert response.status_code == 201, response.text
    assert response.json() == {
        "id": "ORDER1", "table": "T12", "currency": "USD",
        "items": [{"item_id": "ITEM1", "name": "Synthetic Soup", "quantity": 2,
                   "unit_price": 10.99, "modifiers": []}],
        "subtotal": 21.98, "tax": 1.76, "total": 23.74,
    }
    assert len(calls) == 3
    assert calls[0].url.path == "/v3/merchants/synthetic-merchant/items"
    assert calls[0].url.params["filter"] == "id in ('ITEM1')"
    assert calls[1].url.path.endswith("/order_types/TYPE1")
    assert calls[2].url.path == "/v3/merchants/synthetic-merchant/atomic_order/orders"
    assert calls[2].read().decode() == (
        '{"orderCart":{"lineItems":[{"item":{"id":"ITEM1"}},'
        '{"item":{"id":"ITEM1"}}],"orderType":{"id":"TYPE1"}}}'
    )
    assert b"T12" not in calls[2].read()
    assert b"synthetic-token" not in calls[2].read()


@pytest.mark.asyncio
async def test_atomic_order_includes_authorized_modifier_ids(clover_mock) -> None:
    calls, responses = clover_mock
    responses["inventory"]["elements"][0]["modifierGroups"] = {"elements": [{"id": "G1"}]}
    response = await post({"table": "T12", "items": [{"item_id": "ITEM1", "quantity": 1,
                                                   "modifier_ids": ["M1"]}]})
    assert response.status_code == 201, response.text
    assert response.json()["items"][0]["modifiers"] == [
        {"id": "M1", "name": "Mild", "price_delta": 0.25}]
    posted = [request for request in calls if request.url.path.endswith("/atomic_order/orders")]
    assert len(posted) == 1
    assert posted[0].read().decode().find('"modifications":[{"modifier":{"id":"M1"}}]') > 0


@pytest.mark.asyncio
async def test_same_item_with_distinct_modifier_variants_is_not_collapsed(clover_mock) -> None:
    calls, responses = clover_mock
    responses["inventory"]["elements"][0]["modifierGroups"] = {"elements": [{"id": "G1"}]}
    response = await post({"table": "T12", "items": [
        {"item_id": "ITEM1", "quantity": 1, "modifier_ids": ["M1"]},
        {"item_id": "ITEM1", "quantity": 1, "modifier_ids": ["M2"]},
    ]})
    assert response.status_code == 201, response.text
    assert len(response.json()["items"]) == 2
    inventory_get = [request for request in calls if request.method == "GET" and request.url.path.endswith("/items")]
    assert len(inventory_get) == 1 and inventory_get[0].url.params["limit"] == "1"
    post_body = json.loads(next(request for request in calls if request.url.path.endswith("/atomic_order/orders")).content)
    assert [line["modifications"][0]["modifier"]["id"] for line in post_body["orderCart"]["lineItems"]] == ["M1", "M2"]


@pytest.mark.asyncio
async def test_atomic_modifier_missing_from_readback_is_uncertain(clover_mock) -> None:
    calls, responses = clover_mock
    responses["inventory"]["elements"][0]["modifierGroups"] = {"elements": [{"id": "G1"}]}
    responses["omit_modifier_readback"] = True
    body = {"table": "T12", "items": [{"item_id": "ITEM1", "quantity": 1, "modifier_ids": ["M1"]}]}
    first = await post(body)
    replay = await post(body)
    assert (first.status_code, replay.status_code) == (502, 409)
    assert len([request for request in calls if request.method == "POST"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[], ["WRONG"], ["M1", "M2"]])
async def test_required_invalid_and_too_many_modifiers_rejected(clover_mock, ids) -> None:
    calls, responses = clover_mock
    responses["inventory"]["elements"][0]["modifierGroups"] = {"elements": [{"id": "G1"}]}
    response = await post({"table": "T12", "items": [{"item_id": "ITEM1", "quantity": 1,
                                                   "modifier_ids": ids}]})
    assert response.status_code == 400
    assert not any(request.method == "POST" for request in calls)


@pytest.mark.asyncio
async def test_modifier_from_unassociated_item_is_rejected(clover_mock) -> None:
    calls, _ = clover_mock
    response = await post({"table": "T12", "items": [{"item_id": "ITEM1", "quantity": 1,
                                                   "modifier_ids": ["M1"]}]})
    assert response.status_code == 400
    assert not any(request.method == "POST" for request in calls)


@pytest.mark.asyncio
async def test_same_key_returns_durable_success_and_conflict(clover_mock) -> None:
    calls, _ = clover_mock
    first = await post()
    app.state.order_service = None  # Simulate a process-local service restart.
    second = await post()
    changed = await post({"table": "T13", "items": BODY["items"]})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert changed.status_code == 409
    assert len([call for call in calls if call.method == "POST"]) == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_only_posts_once(clover_mock) -> None:
    calls, responses = clover_mock
    responses["delay"] = True
    first, second = await asyncio.gather(post(), post())
    assert sorted([first.status_code, second.status_code]) == [201, 201]
    assert len([call for call in calls if call.method == "POST"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"table": " ", "items": BODY["items"]},
    {"table": "T12", "items": []},
    {"table": "T12", "items": [{"item_id": "ITEM1", "quantity": 0}]},
    {"table": "T12", "items": [{"item_id": "ITEM1", "quantity": True}]},
    {"table": "T12", "items": [{"item_id": "ITEM1'", "quantity": 1}]},
    {"table": "T12", "items": BODY["items"] * 2},
    {"table": "T12", "items": BODY["items"], "total": 1},
])
async def test_invalid_requests_never_reach_clover(clover_mock, bad) -> None:
    calls, _ = clover_mock
    response = await post(bad)
    assert response.status_code == 422
    assert calls == []


@pytest.mark.asyncio
async def test_missing_idempotency_key_rejected(clover_mock) -> None:
    response = await post(headers={})
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("inventory", [
    {"elements": []},
    {"elements": [{"id": "ITEM1", "name": "Soup", "price": 100, "available": False}]},
    {"elements": [{"id": "ITEM1", "name": "Soup", "price": 100, "priceType": "PER_UNIT"}]},
    {"elements": [{"id": "ITEM1", "name": "Soup", "price": 100, "isAgeRestricted": True}]},
])
async def test_unavailable_inventory_never_creates_order(clover_mock, inventory) -> None:
    calls, responses = clover_mock
    responses["inventory"] = inventory
    response = await post()
    assert response.status_code == 400
    assert not any(call.method == "POST" for call in calls)


@pytest.mark.asyncio
async def test_post_timeout_is_persistently_uncertain(clover_mock) -> None:
    calls, responses = clover_mock
    responses["order"] = httpx.ReadTimeout("synthetic timeout")
    first = await post()
    app.state.order_service = None
    second = await post()
    assert first.status_code == 502
    assert second.status_code == 409
    assert len([call for call in calls if call.method == "POST"]) == 1
    assert "synthetic timeout" not in str(first.json())


@pytest.mark.asyncio
async def test_definite_provider_rejection_can_retry_same_key(clover_mock) -> None:
    calls, responses = clover_mock
    responses["order"] = 400
    first = await post()
    responses["order"] = {"id": "ORDER1", "total": 100}
    second = await post()
    assert first.status_code == 502
    assert second.status_code == 201
    assert second.json()["subtotal"] is None
    assert second.json()["tax"] is None
    assert second.json()["currency"] is None
    assert len([call for call in calls if call.method == "POST"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [408, 409, 429, 500, {"total": 100}, httpx.ReadTimeout("private")])
async def test_ambiguous_or_malformed_result_is_not_retried(clover_mock, outcome) -> None:
    calls, responses = clover_mock
    responses["order"] = outcome
    first = await post()
    second = await post()
    assert first.status_code == 502
    assert second.status_code == 409
    assert len([call for call in calls if call.method == "POST"]) == 1
    assert "private" not in str(first.json())


@pytest.mark.asyncio
async def test_invalid_order_type_fails_before_post(clover_mock) -> None:
    calls, responses = clover_mock
    responses["order_type"] = {"id": "TYPE1", "isDeleted": True}
    response = await post()
    assert response.status_code == 502
    assert not any(call.method == "POST" for call in calls)


@pytest.mark.asyncio
async def test_missing_order_type_configuration_fails_before_clover(clover_mock) -> None:
    calls, _ = clover_mock
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    response = await post()
    assert response.status_code == 503
    assert calls == []


@pytest.mark.asyncio
async def test_existing_pending_claim_survives_store_restart(tmp_path) -> None:
    path = tmp_path / "orders.sqlite3"
    first = SQLiteOrderLedger(path)
    second = SQLiteOrderLedger(path)
    assert await first.claim("scope", "keyhash", "fingerprint") == ("claimed", None)
    assert await second.claim("scope", "keyhash", "fingerprint") == ("pending", None)
    assert await second.claim("scope", "keyhash", "different") == ("conflict", None)


@pytest.mark.asyncio
async def test_oauth_client_never_uses_sandbox_token() -> None:
    requests = []

    class Provider:
        async def get_access_token(self):
            return "synthetic-oauth-token"

        async def invalidate(self):
            raise AssertionError("No invalidation expected")

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "ORDER1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with CloverClient(
            base_url="https://apisandbox.dev.clover.com",
            merchant_id="synthetic-merchant",
            api_token="should-not-be-used",
            http_client=http,
            token_provider=Provider(),
        ) as client:
            await client.create_atomic_order(item_ids=["ITEM1"], order_type_id="TYPE1")
    assert requests[0].headers["Authorization"] == "Bearer synthetic-oauth-token"
