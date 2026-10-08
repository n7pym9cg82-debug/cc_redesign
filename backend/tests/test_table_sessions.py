"""Table sessions use synthetic credentials, temporary SQLite, mocked Clover."""

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from decimal import Decimal

import httpx
import pytest
from cryptography.fernet import Fernet

from app.api.menu import get_clover_client
from app.api.table_sessions import get_table_session_service
from app.core.config import Settings, get_settings
from app.integrations.clover.client import CloverClient
from app.integrations.clover.models import CloverOrderReadback
from app.main import app
from app.menu_import.catalog import MenuResponse
from app.orders.total_calculator import (
    UnsupportedOrderCalculationError, calculate_order_total,
)
from app.public.tokens import TableTokenService
from app.table_sessions.service import normalize_readback
from scripts.inspect_table_session import inspect as inspect_table_session

ITEMS = {
    "BIRYANI": ("Chicken Biryani", 1699),
    "NAAN": ("Garlic Naan", 399),
    "LASSI": ("Mango Lassi", 499),
    "TEA": ("Indian Masala Tea", 299),
}


@pytest.fixture
def clover():
    calls: list[httpx.Request] = []
    state: dict[str, object] = {
        "lines": [], "append_result": 200, "append_delay": False,
        "inventory_missing": False, "readback_fail_once": False,
        "final_readback_fail_once": False,
        "atomic_delay": False, "atomic_result": 200, "provider": None,
        "total": 2996, "total_update_result": 200,
        "total_update_response": "updated",
        "line_overrides": {}, "order_overrides": {}, "readback_total_override": None,
        "lassi_modifiers": False, "modifications_by_line": {},
        "suppress_modification_readback": False,
        "verify_line_fail_once": None,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/items"):
            requested = [item_id for item_id in ITEMS if item_id in request.url.params["filter"]]
            if state["inventory_missing"]:
                requested = []
            return httpx.Response(200, json={"elements": [
                {"id": item_id, "name": ITEMS[item_id][0], "price": ITEMS[item_id][1],
                 "available": True, "priceType": "FIXED", "modifierGroups": {"elements": [
                     {"id": "G1"}] if state["lassi_modifiers"] and item_id == "LASSI" else []}}
                for item_id in requested
            ]})
        if request.method == "GET" and path.endswith("/modifier_groups"):
            return httpx.Response(200, json={"elements": [
                {"id": "G1", "name": "Lassi Style", "minRequired": 1, "maxAllowed": 1}
            ]})
        if request.method == "GET" and path.endswith("/modifier_groups/G1/modifiers"):
            return httpx.Response(200, json={"elements": [
                {"id": "SWEET", "name": "Sweet", "price": 25},
            ]})
        if request.method == "GET" and path.endswith("/order_types/TYPE1"):
            return httpx.Response(200, json={"id": "TYPE1"})
        if request.method == "POST" and path.endswith("/atomic_order/orders"):
            if state["atomic_delay"]:
                await asyncio.sleep(0.05)
            result = state["atomic_result"]
            if isinstance(result, Exception):
                raise result
            if result != 200:
                return httpx.Response(result, json={"error": "private provider detail"})
            state["lines"] = [entry["item"]["id"] for entry in
                              json.loads(request.content)["orderCart"]["lineItems"]]
            return httpx.Response(200, json={"id": "ORDER1", "total": 9999})
        if request.method == "POST" and (
            path.endswith("/line_items") or path.endswith("/bulk_line_items")
        ):
            if state["append_delay"]:
                await asyncio.sleep(0.05)
            result = state["append_result"]
            if isinstance(result, Exception):
                raise result
            if result != 200:
                return httpx.Response(result, json={"error": "private provider detail"})
            body = json.loads(request.content)
            additions = (
                [body["item"]["id"]] if path.endswith("/line_items")
                else [entry["item"]["id"] for entry in body["items"]]
            )
            state["lines"] = [*state["lines"], *additions]
            return httpx.Response(200, json={"id": f"LINE{len(state['lines']) - 1}"} if state["lassi_modifiers"] else {"elements": []})
        if request.method == "POST" and path.endswith("/modifications"):
            line_id = path.split("/")[-2]
            state["modifications_by_line"].setdefault(line_id, []).append({
                "modifier": {"id": json.loads(request.content)["modifier"]["id"]},
                "name": "Sweet", "amount": 25,
            })
            return httpx.Response(200, json={"id": "MODIFICATION1"})
        if request.method == "GET" and "/line_items/LINE" in path:
            line_id = path.rsplit("/", 1)[-1]
            if state["verify_line_fail_once"] == line_id:
                state["verify_line_fail_once"] = None
                return httpx.Response(503, json={"error": "private"})
            index = int(line_id[4:])
            return httpx.Response(200, json={
                "id": line_id, "item": {"id": state["lines"][index]},
                "modifications": {"elements": [] if state["suppress_modification_readback"] else state["modifications_by_line"].get(line_id, [])},
            })
        if request.method == "GET" and path.endswith("/orders/ORDER1/line_items"):
            lines = []
            for index, item_id in enumerate(state["lines"]):
                line = {"id": f"LINE{index}", "item": {"id": item_id},
                        "name": ITEMS[item_id][0], "price": ITEMS[item_id][1],
                        "modifications": {"elements": [] if state["suppress_modification_readback"] else state["modifications_by_line"].get(f"LINE{index}", [])}, "discounts": {"elements": []},
                        "taxRates": {"elements": []}}
                line.update(state["line_overrides"].get(index, {}))
                lines.append(line)
            return httpx.Response(200, json={"elements": lines})
        if request.method == "POST" and path.endswith("/orders/ORDER1"):
            result = state["total_update_result"]
            if isinstance(result, Exception):
                raise result
            if result != 200:
                return httpx.Response(result, json={"error": "private provider detail"})
            state["total"] = json.loads(request.content)["total"]
            update_response = state["total_update_response"]
            if update_response == "updated":
                update_response = {"id": "ORDER1", "total": state["total"]}
            if update_response == "empty":
                return httpx.Response(200, content=b"")
            return httpx.Response(200, json=update_response)
        if request.method == "GET" and path.endswith("/orders/ORDER1"):
            if (state["final_readback_fail_once"] and
                    request.url.params.get("expand") == "lineItems"):
                state["final_readback_fail_once"] = False
                return httpx.Response(503, json={"error": "private"})
            if state["readback_fail_once"]:
                state["readback_fail_once"] = False
                return httpx.Response(503, json={"error": "private"})
            lines = [
                {"id": f"LINE{index}", "item": {"id": item_id},
                 "name": ITEMS[item_id][0], "price": ITEMS[item_id][1]}
                for index, item_id in enumerate(state["lines"])
            ]
            payload = {
                "id": "ORDER1", "currency": "USD",
                "total": state["readback_total_override"] if
                state["readback_total_override"] is not None and
                request.url.params.get("expand") == "lineItems" else state["total"],
                "lineItems": {"elements": lines},
            }
            payload.update(state["order_overrides"])
            return httpx.Response(200, json=payload)
        raise AssertionError("Unexpected Clover request")

    async def dependency():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = state["provider"]
            async with CloverClient(
                base_url="https://apisandbox.dev.clover.com",
                merchant_id="synthetic-merchant",
                api_token="synthetic-token" if provider is None else None,
                token_provider=provider,
                http_client=http,
            ) as client:
                yield client

    app.dependency_overrides[get_clover_client] = dependency
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, clover_merchant_id="synthetic-merchant",
        clover_order_type_id="TYPE1",
    )
    return calls, state


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


async def create(table: str = "T12") -> dict:
    result = await request("POST", "/api/table-sessions", json={"table": table})
    assert result.status_code == 200, result.text
    return result.json()


async def add(session_id: str, item_id: str, quantity: int, key: str) -> httpx.Response:
    return await request(
        "POST", f"/api/table-sessions/{session_id}/items",
        json={"items": [{"item_id": item_id, "quantity": quantity}]},
        headers={"Idempotency-Key": key},
    )


@pytest.mark.asyncio
async def test_create_resume_and_distinct_tables(clover) -> None:
    first = await create(" t12 ")
    repeated = await create("T12")
    other = await create("T13")
    assert first == repeated
    assert first["table"] == "T12"
    assert first["status"] == "open"
    assert first["clover_order_id"] is None
    assert first["session_id"] != other["session_id"]
    assert first["created_at"] and first["updated_at"]
    get = await request("GET", f"/api/table-sessions/{first['session_id']}")
    assert get.status_code == 200
    assert get.json() == first


@pytest.mark.asyncio
async def test_session_diagnostic_is_read_only_and_redacted(clover) -> None:
    session = await create("T27")
    await add(session["session_id"], "BIRYANI", 1, "secret-key-123")
    db_path = app.state.table_session_service.repository.path
    before = db_path.stat().st_mtime_ns
    report = "\n".join(inspect_table_session("T27", db_path))
    assert "operation_state=idle" in report
    assert "mutation_count=1" in report
    assert "order_suffix=DER1" in report
    assert "ORDER1" not in report
    assert "secret-key-123" not in report
    assert "BIRYANI" not in report
    assert db_path.stat().st_mtime_ns == before


@pytest.mark.asyncio
async def test_auth_mode_change_resumes_same_merchant_session(clover) -> None:
    first = await create()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, clover_merchant_id="synthetic-merchant",
        clover_auth_mode="oauth", clover_app_id="synthetic-app",
    )
    resumed = await create()
    assert resumed["session_id"] == first["session_id"]


@pytest.mark.asyncio
async def test_first_submission_creates_one_order_and_saves_id(clover) -> None:
    calls, state = clover
    session = await create()
    response = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["clover_order_id"] == "ORDER1"
    assert data["order"]["id"] == "ORDER1"
    assert data["order"]["total"] == 29.96  # Clover readback, not local sum.
    assert data["order"]["subtotal"] is None
    assert data["order"]["tax"] is None
    assert data["order"]["items"] == [{
        "item_id": "BIRYANI", "name": "Chicken Biryani",
        "quantity": 1, "unit_price": 16.99, "modifiers": [],
    }]
    assert state["lines"] == ["BIRYANI"]
    assert sum(call.url.path.endswith("/atomic_order/orders") for call in calls) == 1
    stored = await request("GET", f"/api/table-sessions/{session['session_id']}")
    assert stored.json()["clover_order_id"] == "ORDER1"


@pytest.mark.asyncio
async def test_second_submission_appends_to_saved_order_and_readback_aggregates(clover) -> None:
    calls, state = clover
    session = await create()
    assert (await add(session["session_id"], "BIRYANI", 1, "first-key-123")).status_code == 200
    second = await add(session["session_id"], "LASSI", 2, "second-key-123")
    assert second.status_code == 200, second.text
    assert second.json()["clover_order_id"] == "ORDER1"
    assert second.json()["order"]["items"] == [
        {"item_id": "BIRYANI", "name": "Chicken Biryani", "quantity": 1,
         "unit_price": 16.99, "modifiers": []},
        {"item_id": "LASSI", "name": "Mango Lassi", "quantity": 2,
         "unit_price": 4.99, "modifiers": []},
    ]
    assert sum(call.url.path.endswith("/atomic_order/orders") for call in calls) == 1
    append_calls = [call for call in calls if call.method == "POST" and
                    call.url.path.endswith("/bulk_line_items")]
    assert len(append_calls) == 1
    assert append_calls[0].url.path == (
        "/v3/merchants/synthetic-merchant/orders/ORDER1/bulk_line_items"
    )
    assert json.loads(append_calls[0].content) == {"items": [
        {"item": {"id": "LASSI"}, "price": 499},
        {"item": {"id": "LASSI"}, "price": 499},
    ]}
    assert state["lines"] == ["BIRYANI", "LASSI", "LASSI"]


@pytest.mark.asyncio
async def test_modified_append_applies_clover_modifier_and_recalculates_total(clover) -> None:
    calls, state = clover
    session = await create()
    assert (await add(session["session_id"], "BIRYANI", 1, "first-key-123")).status_code == 200
    state["lassi_modifiers"] = True
    second = await request(
        "POST", f"/api/table-sessions/{session['session_id']}/items",
        json={"items": [{"item_id": "LASSI", "quantity": 1, "modifier_ids": ["SWEET"]}]},
        headers={"Idempotency-Key": "second-key-123"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["order"]["total"] == 22.23
    assert second.json()["order"]["items"][1]["modifiers"] == [
        {"id": "SWEET", "name": "Sweet", "price_delta": 0.25}]
    modifier_posts = [call for call in calls if call.method == "POST" and call.url.path.endswith("/modifications")]
    assert len(modifier_posts) == 1
    assert json.loads(modifier_posts[0].content) == {"modifier": {"id": "SWEET"}}
    total_posts = [call for call in calls if call.method == "POST" and call.url.path.endswith("/orders/ORDER1")]
    assert json.loads(total_posts[-1].content) == {"total": 2223}
    third = await add(session["session_id"], "BIRYANI", 1, "third-key-123")
    assert third.status_code == 200, third.text
    assert third.json()["order"]["total"] == 39.22
    assert third.json()["order"]["items"][1]["modifiers"][0]["id"] == "SWEET"


@pytest.mark.asyncio
async def test_modified_append_missing_clover_modification_requires_reconciliation(clover) -> None:
    calls, state = clover
    session = await create()
    assert (await add(session["session_id"], "BIRYANI", 1, "first-key-123")).status_code == 200
    state["lassi_modifiers"] = True
    state["suppress_modification_readback"] = True
    body = {"items": [{"item_id": "LASSI", "quantity": 1, "modifier_ids": ["SWEET"]}]}
    path = f"/api/table-sessions/{session['session_id']}/items"
    first = await request("POST", path, json=body, headers={"Idempotency-Key": "second-key-123"})
    replay = await request("POST", path, json=body, headers={"Idempotency-Key": "second-key-123"})
    assert (first.status_code, replay.status_code) == (502, 409)
    assert len([call for call in calls if call.method == "POST" and call.url.path.endswith("/modifications")]) == 1


@pytest.mark.asyncio
async def test_first_submission_with_multiple_items_uses_one_atomic_order(clover) -> None:
    calls, state = clover
    session = await create()
    response = await request(
        "POST", f"/api/table-sessions/{session['session_id']}/items",
        json={"items": [
            {"item_id": "BIRYANI", "quantity": 1},
            {"item_id": "NAAN", "quantity": 1},
        ]}, headers={"Idempotency-Key": "first-key-123"},
    )
    assert response.status_code == 200
    assert state["lines"] == ["BIRYANI", "NAAN"]
    assert sum(call.url.path.endswith("/atomic_order/orders") for call in calls) == 1


@pytest.mark.asyncio
async def test_single_append_uses_inventory_reference_without_price(clover) -> None:
    calls, _ = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    second = await add(session["session_id"], "NAAN", 1, "second-key-123")
    assert second.status_code == 200
    call = next(call for call in calls if call.method == "POST" and
                call.url.path.endswith("/orders/ORDER1/line_items"))
    assert json.loads(call.content) == {"item": {"id": "NAAN"}}


@pytest.mark.asyncio
async def test_missing_invalid_and_closed_sessions_reject_additions(clover) -> None:
    session = await create()
    missing = await add("0" * 32, "NAAN", 1, "missing-key-123")
    assert missing.status_code == 404
    service = get_table_session_service(type("Req", (), {"app": app})())
    closed = await service.close(Settings(_env_file=None, clover_merchant_id="synthetic-merchant"),
                                 session["session_id"])
    assert closed.status == "closed"
    assert closed.closed_at is not None
    rejected = await add(session["session_id"], "NAAN", 1, "closed-key-123")
    assert rejected.status_code == 409
    renewed = await create()
    assert renewed["session_id"] != session["session_id"]


@pytest.mark.asyncio
async def test_closed_session_replays_success_without_mutation(clover) -> None:
    calls, _ = clover
    session = await create()
    first = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    service = get_table_session_service(type("Req", (), {"app": app})())
    await service.close(Settings(_env_file=None, clover_merchant_id="synthetic-merchant"),
                        session["session_id"])
    replay = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    new_addition = await add(session["session_id"], "NAAN", 1, "second-key-123")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert new_addition.status_code == 409
    assert sum(call.url.path.endswith("/atomic_order/orders") for call in calls) == 1


@pytest.mark.asyncio
async def test_client_cannot_inject_clover_order_id(clover) -> None:
    session = await create()
    response = await request(
        "POST", f"/api/table-sessions/{session['session_id']}/items",
        json={"clover_order_id": "INJECTED", "items": [
            {"item_id": "NAAN", "quantity": 1}
        ]}, headers={"Idempotency-Key": "inject-key-123"},
    )
    assert response.status_code == 422
    assert (await request("GET", f"/api/table-sessions/{session['session_id']}")).json()[
        "clover_order_id"] is None


@pytest.mark.asyncio
async def test_invalid_inventory_rejected_without_clover_mutation(clover) -> None:
    calls, state = clover
    session = await create()
    state["inventory_missing"] = True
    first = await add(session["session_id"], "NAAN", 1, "first-key-123")
    assert first.status_code == 400
    state["inventory_missing"] = False
    assert (await add(session["session_id"], "NAAN", 1, "first-key-123")).status_code == 200
    state["inventory_missing"] = True
    second = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert second.status_code == 400
    assert sum(call.url.path.endswith("/atomic_order/orders") for call in calls) == 1
    assert not any(call.method == "POST" and call.url.path.endswith("/line_items") for call in calls)


@pytest.mark.asyncio
async def test_append_idempotency_replay_conflict_and_concurrency(clover) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["append_delay"] = True
    first, duplicate = await asyncio.gather(
        add(session["session_id"], "LASSI", 1, "second-key-123"),
        add(session["session_id"], "LASSI", 1, "second-key-123"),
    )
    assert first.status_code == duplicate.status_code == 200
    assert first.json() == duplicate.json()
    conflict = await add(session["session_id"], "NAAN", 1, "second-key-123")
    assert conflict.status_code == 409
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1/line_items") for call in calls) == 1


@pytest.mark.asyncio
async def test_distinct_concurrent_keys_keep_one_running_order(clover) -> None:
    calls, state = clover
    session = await create()
    state["atomic_delay"] = True
    first, second = await asyncio.gather(
        add(session["session_id"], "BIRYANI", 1, "first-key-123"),
        add(session["session_id"], "NAAN", 1, "second-key-123"),
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["clover_order_id"] == second.json()["clover_order_id"] == "ORDER1"
    assert sum(call.url.path.endswith("/atomic_order/orders") for call in calls) == 1
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1/line_items") for call in calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_append_is_not_retried(clover) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["append_result"] = httpx.ReadTimeout("private token-like text")
    first = await add(session["session_id"], "NAAN", 1, "second-key-123")
    repeated = await add(session["session_id"], "NAAN", 1, "second-key-123")
    different = await add(session["session_id"], "LASSI", 1, "third-key-123")
    assert (first.status_code, repeated.status_code, different.status_code) == (502, 409, 409)
    assert "private token-like text" not in first.text
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1/line_items") for call in calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_first_order_is_not_retried(clover) -> None:
    calls, state = clover
    session = await create()
    state["atomic_result"] = httpx.ReadTimeout("private")
    first = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    retry = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    assert (first.status_code, retry.status_code) == (502, 409)
    assert sum(call.url.path.endswith("/atomic_order/orders") for call in calls) == 1


@pytest.mark.asyncio
async def test_readback_failure_retries_get_only_with_same_key(clover) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["readback_fail_once"] = True
    failed = await add(session["session_id"], "NAAN", 1, "second-key-123")
    replay = await add(session["session_id"], "NAAN", 1, "second-key-123")
    third = await add(session["session_id"], "NAAN", 1, "second-key-123")
    assert failed.status_code == 502
    assert replay.status_code == 200
    assert third.json() == replay.json()
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1/line_items") for call in calls) == 1
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1") for call in calls) == 1


@pytest.mark.asyncio
async def test_append_final_readback_pending_retries_get_only(clover) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["final_readback_fail_once"] = True
    failed = await add(session["session_id"], "NAAN", 1, "second-key-123")
    replay = await add(session["session_id"], "NAAN", 1, "second-key-123")
    third = await add(session["session_id"], "NAAN", 1, "second-key-123")
    assert failed.status_code == 503
    assert failed.json() == {"detail": "Order sent; confirmation pending", "code": "ORDER_CONFIRMATION_PENDING"}
    assert replay.status_code == 200
    assert third.json() == replay.json()
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1/line_items") for call in calls) == 1
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1") for call in calls) == 1


@pytest.mark.asyncio
async def test_confirmed_modified_append_reconciles_missing_empty_wrapper_get_only(clover) -> None:
    calls, state = clover
    session = await create("T19")
    assert (await add(session["session_id"], "BIRYANI", 1, "first-key-123")).status_code == 200
    state["lassi_modifiers"] = True
    state["final_readback_fail_once"] = True
    path = f"/api/table-sessions/{session['session_id']}/items"
    body = {"items": [{"item_id": "LASSI", "quantity": 1,
                       "modifier_ids": ["SWEET"]}]}
    headers = {"Idempotency-Key": "second-key-123"}
    first = await request("POST", path, json=body, headers=headers)
    assert first.status_code == 503
    assert first.json()["code"] == "ORDER_CONFIRMATION_PENDING"
    # Clover can omit an empty modifications wrapper on a plain prior line.
    state["line_overrides"][0] = {"modifications": None}
    before = len(calls)
    replay = await request("POST", path, json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["order"]["total"] == 22.23
    assert replay.json()["order"]["items"][1]["modifiers"][0]["id"] == "SWEET"
    assert calls[before:] and all(call.method == "GET" for call in calls[before:])
    with sqlite3.connect(app.state.table_session_service.repository.path) as db:
        mutation = db.execute(
            "SELECT state,response_json FROM table_session_mutations "
            "WHERE session_id=? AND key_hash=?",
            (session["session_id"], hashlib.sha256(b"second-key-123").hexdigest()),
        ).fetchone()
        operation = db.execute(
            "SELECT operation_state FROM table_sessions WHERE session_id=?",
            (session["session_id"],),
        ).fetchone()
    assert mutation[0] == "succeeded"
    assert json.loads(mutation[1])["order"]["total"] == 22.23
    assert operation == ("idle",)


@pytest.mark.asyncio
async def test_confirmed_modified_append_mismatch_stays_pending_with_safe_reason(
    clover, caplog,
) -> None:
    calls, state = clover
    session = await create("T19")
    assert (await add(session["session_id"], "BIRYANI", 1, "first-key-123")).status_code == 200
    state["lassi_modifiers"] = True
    state["final_readback_fail_once"] = True
    path = f"/api/table-sessions/{session['session_id']}/items"
    body = {"items": [{"item_id": "LASSI", "quantity": 1,
                       "modifier_ids": ["SWEET"]}]}
    headers = {"Idempotency-Key": "second-key-123"}
    assert (await request("POST", path, json=body, headers=headers)).status_code == 503
    state["suppress_modification_readback"] = True
    before = len(calls)
    with caplog.at_level(logging.WARNING):
        replay = await request("POST", path, json=body, headers=headers)
    assert replay.status_code == 503
    assert replay.json() == {
        "detail": "Order sent; confirmation pending", "code": "ORDER_CONFIRMATION_PENDING",
    }
    assert "check=modifier_count_mismatch" in caplog.text
    assert "expected_modifier_counts={'SWEET': 1}" in caplog.text
    assert "actual_modifier_counts={}" in caplog.text
    assert "second-key-123" not in caplog.text
    assert "synthetic-token" not in caplog.text
    assert calls[before:] and all(call.method == "GET" for call in calls[before:])
    with sqlite3.connect(app.state.table_session_service.repository.path) as db:
        metadata = db.execute(
            "SELECT state,response_json FROM table_session_mutations "
            "WHERE session_id=? AND key_hash=?",
            (session["session_id"], hashlib.sha256(b"second-key-123").hexdigest()),
        ).fetchone()
        operation = db.execute(
            "SELECT operation_state FROM table_sessions WHERE session_id=?",
            (session["session_id"],),
        ).fetchone()
    assert metadata[0] == "applied"
    assert json.loads(metadata[1])["phase"] == "readback_pending"
    assert operation == ("applied",)
    state["suppress_modification_readback"] = False
    assert (await request("POST", path, json=body, headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_first_order_readback_failure_recovers_same_key_without_duplicate(clover) -> None:
    calls, state = clover
    session = await create("T20")
    state["readback_fail_once"] = True
    first = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    assert first.status_code == 503
    assert first.json() == {"detail": "Order sent; confirmation pending", "code": "ORDER_CONFIRMATION_PENDING"}
    persisted = await request("GET", f"/api/table-sessions/{session['session_id']}")
    assert persisted.json()["clover_order_id"] == "ORDER1"
    changed = await add(session["session_id"], "NAAN", 1, "first-key-123")
    assert changed.status_code == 409
    replay = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    assert replay.status_code == 200
    assert replay.json()["order"]["id"] == "ORDER1"
    third = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    assert third.json() == replay.json()
    assert sum(call.method == "POST" and call.url.path.endswith("/atomic_order/orders") for call in calls) == 1
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1/line_items") for call in calls) == 0
    assert sum(call.method == "GET" and call.url.path.endswith("/orders/ORDER1") for call in calls) == 2
    assert sum(call.method == "GET" and call.url.path.endswith("/orders/ORDER1/line_items") for call in calls) == 0


@pytest.mark.asyncio
async def test_first_order_repeated_readback_failure_stays_pending_then_recovers(clover) -> None:
    calls, state = clover
    session = await create("T21")
    state["readback_fail_once"] = True
    first = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["readback_fail_once"] = True
    second = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    third = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    assert (first.status_code, second.status_code, third.status_code) == (503, 503, 200)
    assert first.json()["code"] == second.json()["code"] == "ORDER_CONFIRMATION_PENDING"
    assert sum(call.method == "POST" and call.url.path.endswith("/atomic_order/orders") for call in calls) == 1


@pytest.mark.asyncio
async def test_public_same_key_confirmation_pending_recovers_without_new_order(clover) -> None:
    calls, state = clover
    settings = Settings(
        _env_file=None, clover_merchant_id="synthetic-merchant",
        clover_order_type_id="TYPE1", table_token_signing_key=Fernet.generate_key().decode(),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    token = TableTokenService(settings).issue("T20")
    boot = await request("POST", "/api/public/table-sessions", json={"table_token": token})
    assert boot.status_code == 200
    path = f"/api/public/table-sessions/{boot.json()['session_id']}/orders"
    body = {"table_token": token, "items": [{"item_id": "BIRYANI", "quantity": 1}]}
    headers = {"Idempotency-Key": "public-key-123"}
    state["readback_fail_once"] = True
    first = await request("POST", path, json=body, headers=headers)
    second = await request("POST", path, json=body, headers=headers)
    third = await request("POST", path, json=body, headers=headers)
    assert first.status_code == 503 and first.json()["code"] == "ORDER_CONFIRMATION_PENDING"
    assert "ORDER1" not in first.text
    assert second.status_code == third.status_code == 200
    assert second.json() == third.json()
    assert sum(call.method == "POST" and call.url.path.endswith("/atomic_order/orders") for call in calls) == 1


@pytest.mark.asyncio
async def test_new_key_reconciles_confirmed_first_order_then_appends(clover) -> None:
    calls, state = clover
    session = await create("T20")
    state["readback_fail_once"] = True
    first = await add(session["session_id"], "BIRYANI", 1, "goat-key-123")
    assert first.status_code == 503
    assert (await request("GET", f"/api/table-sessions/{session['session_id']}"))\
        .json()["clover_order_id"] == "ORDER1"
    second = await add(session["session_id"], "LASSI", 1, "lassi-key-123")
    assert second.status_code == 200, second.text
    assert second.json()["order"]["total"] == 21.98
    count = len(calls)
    old_replay = await add(session["session_id"], "BIRYANI", 1, "goat-key-123")
    assert old_replay.status_code == 200
    assert len(calls) == count  # the prior mutation is now SUCCESS, not merely unlocked
    assert state["lines"] == ["BIRYANI", "LASSI"]
    assert sum(c.method == "POST" and c.url.path.endswith("/atomic_order/orders") for c in calls) == 1
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1/line_items") for c in calls) == 1
    with sqlite3.connect(app.state.table_session_service.repository.path) as db:
        old = db.execute(
            "SELECT state FROM table_session_mutations WHERE session_id=? AND key_hash=?",
            (session["session_id"], hashlib.sha256(b"goat-key-123").hexdigest()),
        ).fetchone()
        operation = db.execute(
            "SELECT operation_state FROM table_sessions WHERE session_id=?",
            (session["session_id"],),
        ).fetchone()
    assert old == ("succeeded",)
    assert operation == ("idle",)


@pytest.mark.asyncio
async def test_new_key_reconciles_legacy_applied_phase(clover) -> None:
    calls, state = clover
    session = await create("T25")
    state["readback_fail_once"] = True
    assert (await add(session["session_id"], "BIRYANI", 1, "old-key-123")).status_code == 503
    # Simulate a confirmed row left by the earlier code, before readback_pending existed.
    with sqlite3.connect(app.state.table_session_service.repository.path) as db:
        db.execute(
            "UPDATE table_session_mutations SET response_json=? WHERE session_id=? AND key_hash=?",
            (json.dumps({"phase": "atomic_created"}), session["session_id"],
             hashlib.sha256(b"old-key-123").hexdigest()),
        )
    next_order = await add(session["session_id"], "LASSI", 1, "new-key-123")
    assert next_order.status_code == 200, next_order.text
    assert state["lines"] == ["BIRYANI", "LASSI"]
    assert sum(c.method == "POST" and c.url.path.endswith("/atomic_order/orders") for c in calls) == 1


@pytest.mark.asyncio
async def test_new_key_does_not_pass_old_block_until_get_recovers(clover) -> None:
    calls, state = clover
    session = await create("T21")
    state["readback_fail_once"] = True
    assert (await add(session["session_id"], "BIRYANI", 1, "first-key-123")).status_code == 503
    state["readback_fail_once"] = True
    blocked = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert blocked.status_code == 503
    assert state["lines"] == ["BIRYANI"]
    assert sum(c.method == "POST" and c.url.path.endswith("/atomic_order/orders") for c in calls) == 1
    assert not any(c.method == "POST" and c.url.path.endswith("/orders/ORDER1/line_items") for c in calls)
    recovered = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert recovered.status_code == 200, recovered.text
    assert state["lines"] == ["BIRYANI", "LASSI"]


@pytest.mark.asyncio
async def test_new_key_reconciles_prior_append_without_repeating_it(clover) -> None:
    calls, state = clover
    session = await create("T22")
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["final_readback_fail_once"] = True
    assert (await add(session["session_id"], "NAAN", 1, "second-key-123")).status_code == 503
    later = await add(session["session_id"], "LASSI", 1, "third-key-123")
    assert later.status_code == 200, later.text
    assert state["lines"] == ["BIRYANI", "NAAN", "LASSI"]
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1/line_items") for c in calls) == 2
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1") for c in calls) == 2
    count = len(calls)
    old_replay = await add(session["session_id"], "NAAN", 1, "second-key-123")
    assert old_replay.status_code == 200
    assert len(calls) == count


@pytest.mark.asyncio
async def test_two_confirmed_readbacks_clear_before_new_mutation(clover) -> None:
    calls, state = clover
    session = await create("T26")
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    await add(session["session_id"], "LASSI", 1, "second-key-123")
    with sqlite3.connect(app.state.table_session_service.repository.path) as db:
        db.execute(
            "UPDATE table_session_mutations SET state='applied',response_json=? "
            "WHERE session_id=? AND key_hash=?",
            (json.dumps({"phase": "readback_pending", "resume_phase": "atomic_created"}),
             session["session_id"], hashlib.sha256(b"first-key-123").hexdigest()),
        )
        db.execute(
            "UPDATE table_session_mutations SET state='applied',response_json=? "
            "WHERE session_id=? AND key_hash=?",
            (json.dumps({"phase": "readback_pending", "resume_phase": "total_confirmed",
                         "target_total_cents": 2198}),
             session["session_id"], hashlib.sha256(b"second-key-123").hexdigest()),
        )
        db.execute(
            "UPDATE table_sessions SET operation_state='applied' WHERE session_id=?",
            (session["session_id"],),
        )
    before = len(calls)
    new = await add(session["session_id"], "NAAN", 1, "third-key-123")
    assert new.status_code == 200, new.text
    old_calls = calls[before:]
    assert sum(c.method == "GET" and c.url.path.endswith("/orders/ORDER1") for c in old_calls) >= 2
    assert not any(c.method == "POST" and c.url.path.endswith("/atomic_order/orders") for c in old_calls)
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1/line_items") for c in old_calls) == 1
    with sqlite3.connect(app.state.table_session_service.repository.path) as db:
        states = db.execute(
            "SELECT state FROM table_session_mutations WHERE session_id=? ORDER BY rowid",
            (session["session_id"],),
        ).fetchall()
        operation = db.execute(
            "SELECT operation_state FROM table_sessions WHERE session_id=?",
            (session["session_id"],),
        ).fetchone()
    assert states == [("succeeded",), ("succeeded",), ("succeeded",)]
    assert operation == ("idle",)


@pytest.mark.asyncio
async def test_confirmed_pending_can_clear_without_unblocking_ambiguous_mutation(clover) -> None:
    calls, state = clover
    session = await create("T28")
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["append_result"] = 400
    assert (await add(session["session_id"], "LASSI", 2, "second-key-123")).status_code == 502
    with sqlite3.connect(app.state.table_session_service.repository.path) as db:
        db.execute(
            "UPDATE table_session_mutations SET state='applied',response_json=? "
            "WHERE session_id=? AND key_hash=?",
            (json.dumps({"phase": "readback_pending", "resume_phase": "atomic_created"}),
             session["session_id"], hashlib.sha256(b"first-key-123").hexdigest()),
        )
    before = len(calls)
    blocked = await add(session["session_id"], "NAAN", 1, "third-key-123")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "Previous order outcome is unknown; manual reconciliation required"
    assert not any(c.method == "POST" for c in calls[before:])
    with sqlite3.connect(app.state.table_session_service.repository.path) as db:
        states = db.execute(
            "SELECT state FROM table_session_mutations WHERE session_id=? ORDER BY rowid",
            (session["session_id"],),
        ).fetchall()
        operation = db.execute(
            "SELECT operation_state FROM table_sessions WHERE session_id=?",
            (session["session_id"],),
        ).fetchone()
    assert states == [("succeeded",), ("uncertain",)]
    assert operation == ("uncertain",)


@pytest.mark.asyncio
async def test_same_key_proves_legacy_modified_append_by_get_before_total_update(clover) -> None:
    calls, state = clover
    session = await create("T29")
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["lassi_modifiers"] = True
    state["verify_line_fail_once"] = "LINE2"
    path = f"/api/table-sessions/{session['session_id']}/items"
    body = {"items": [
        {"item_id": "LASSI", "quantity": 1, "modifier_ids": ["SWEET"]},
        {"item_id": "TEA", "quantity": 1},
    ]}
    headers = {"Idempotency-Key": "second-key-123"}
    first = await request("POST", path, json=body, headers=headers)
    assert first.status_code == 502
    assert state["lines"] == ["BIRYANI", "LASSI", "TEA"]
    assert state["modifications_by_line"]["LINE1"][0]["modifier"]["id"] == "SWEET"
    state["suppress_modification_readback"] = True
    assert (await add(session["session_id"], "NAAN", 1, "third-key-123")).status_code == 409
    unproven = await request("POST", path, json=body, headers=headers)
    assert unproven.status_code == 409
    assert not any(c.method == "POST" and c.url.path.endswith("/orders/ORDER1") for c in calls)
    state["suppress_modification_readback"] = False
    recovered = await request("POST", path, json=body, headers=headers)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["order"]["total"] == 25.22
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1/line_items") for c in calls) == 2
    assert sum(c.method == "POST" and c.url.path.endswith("/modifications") for c in calls) == 1
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1") for c in calls) == 1
    next_item = await add(session["session_id"], "NAAN", 1, "third-key-123")
    assert next_item.status_code == 200, next_item.text


@pytest.mark.asyncio
async def test_new_key_proves_legacy_modified_append_from_saved_hash(clover) -> None:
    calls, state = clover
    session = await create("T31")
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["lassi_modifiers"] = True
    state["verify_line_fail_once"] = "LINE2"
    body = {"items": [
        {"item_id": "LASSI", "quantity": 1, "modifier_ids": ["SWEET"]},
        {"item_id": "TEA", "quantity": 1},
    ]}
    path = f"/api/table-sessions/{session['session_id']}/items"
    assert (await request("POST", path, json=body,
                          headers={"Idempotency-Key": "second-key-123"})).status_code == 502
    state["suppress_modification_readback"] = True
    before = len(calls)
    unproven = await add(session["session_id"], "NAAN", 1, "third-key-123")
    assert unproven.status_code == 409
    assert not any(c.method == "POST" for c in calls[before:])
    state["suppress_modification_readback"] = False
    recovered = await add(session["session_id"], "NAAN", 1, "third-key-123")
    assert recovered.status_code == 200, recovered.text
    assert state["lines"] == ["BIRYANI", "LASSI", "TEA", "NAAN"]
    assert sum(c.method == "POST" and c.url.path.endswith("/atomic_order/orders") for c in calls) == 1
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1/line_items") for c in calls) == 3
    assert sum(c.method == "POST" and c.url.path.endswith("/modifications") for c in calls) == 1
    count = len(calls)
    old = await request("POST", path, json=body, headers={"Idempotency-Key": "second-key-123"})
    assert old.status_code == 200
    assert len(calls) == count


@pytest.mark.asyncio
async def test_confirmed_total_from_get_unblocks_new_key_without_reposting_total(clover) -> None:
    calls, state = clover
    session = await create("T30")
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["total_update_result"] = httpx.ReadTimeout("private")
    failed = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert failed.status_code == 502
    state["total"] = 2198  # Simulate Clover applying the timed-out POST.
    state["total_update_result"] = 200
    before = len(calls)
    new = await add(session["session_id"], "TEA", 1, "third-key-123")
    assert new.status_code == 200, new.text
    assert state["lines"] == ["BIRYANI", "LASSI", "TEA"]
    recent = calls[before:]
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1") for c in recent) == 1
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1/line_items") for c in recent) == 1
    assert sum(c.method == "POST" and c.url.path.endswith("/atomic_order/orders") for c in calls) == 1
    count = len(calls)
    old_replay = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert old_replay.status_code == 200
    assert len(calls) == count


@pytest.mark.asyncio
async def test_confirmed_append_skips_total_post_when_clover_already_has_target(clover) -> None:
    calls, state = clover
    session = await create("T32")
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["readback_fail_once"] = True
    assert (await add(session["session_id"], "LASSI", 1, "second-key-123")).status_code == 502
    state["total"] = 2198
    before = len(calls)
    new = await add(session["session_id"], "TEA", 1, "third-key-123")
    assert new.status_code == 200, new.text
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1") for c in calls[before:]) == 1
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1/line_items") for c in calls) == 2


@pytest.mark.asyncio
async def test_ambiguous_prior_create_still_blocks_new_key(clover) -> None:
    calls, state = clover
    session = await create("T23")
    state["atomic_result"] = httpx.ReadTimeout("private")
    assert (await add(session["session_id"], "BIRYANI", 1, "first-key-123")).status_code == 502
    blocked = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert blocked.status_code == 409
    assert sum(c.method == "POST" and c.url.path.endswith("/atomic_order/orders") for c in calls) == 1


@pytest.mark.asyncio
async def test_public_new_modified_order_after_prior_confirmation_recovery(clover) -> None:
    calls, state = clover
    state["lassi_modifiers"] = True
    settings = Settings(
        _env_file=None, clover_merchant_id="synthetic-merchant",
        clover_order_type_id="TYPE1", table_token_signing_key=Fernet.generate_key().decode(),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    token = TableTokenService(settings).issue("T24")
    boot = await request("POST", "/api/public/table-sessions", json={"table_token": token})
    assert boot.status_code == 200
    path = f"/api/public/table-sessions/{boot.json()['session_id']}/orders"
    state["readback_fail_once"] = True
    first = await request("POST", path, json={
        "table_token": token, "items": [{"item_id": "BIRYANI", "quantity": 1}],
    }, headers={"Idempotency-Key": "first-public-key-123"})
    assert first.status_code == 503
    second = await request("POST", path, json={
        "table_token": token, "items": [
            {"item_id": "LASSI", "quantity": 1, "modifier_ids": ["SWEET"]}
        ],
    }, headers={"Idempotency-Key": "second-public-key-123"})
    assert second.status_code == 200, second.text
    assert state["lines"] == ["BIRYANI", "LASSI"]
    assert state["modifications_by_line"]["LINE1"][0]["modifier"]["id"] == "SWEET"
    assert sum(c.method == "POST" and c.url.path.endswith("/atomic_order/orders") for c in calls) == 1
    assert sum(c.method == "POST" and c.url.path.endswith("/orders/ORDER1/line_items") for c in calls) == 1


@pytest.mark.asyncio
async def test_definite_append_rejection_allows_new_key(clover) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["append_result"] = 400
    failed = await add(session["session_id"], "NAAN", 1, "second-key-123")
    state["append_result"] = 200
    succeeded = await add(session["session_id"], "NAAN", 1, "third-key-123")
    assert failed.status_code == 502
    assert succeeded.status_code == 200
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1/line_items") for call in calls) == 2


@pytest.mark.asyncio
async def test_bulk_rejection_is_conservatively_uncertain(clover) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["append_result"] = 400
    first = await add(session["session_id"], "NAAN", 2, "second-key-123")
    retry = await add(session["session_id"], "NAAN", 2, "second-key-123")
    assert (first.status_code, retry.status_code) == (502, 409)
    assert sum(call.url.path.endswith("/orders/ORDER1/bulk_line_items") for call in calls) == 1


@pytest.mark.asyncio
async def test_session_and_success_survive_service_restart(clover) -> None:
    calls, _ = clover
    session = await create()
    first = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    app.state.table_session_service = None
    app.state.order_service = None
    resumed = await create()
    replay = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    assert resumed["session_id"] == session["session_id"]
    assert replay.json() == first.json()
    assert sum(call.url.path.endswith("/atomic_order/orders") for call in calls) == 1


def test_readback_only_aggregates_identical_fixed_price_lines() -> None:
    order = CloverOrderReadback.model_validate({
        "id": "ORDER1", "total": 3198,
        "lineItems": {"elements": [
            {"id": "LINE1", "item": {"id": "BIRYANI"}, "name": "Chicken Biryani",
             "price": 1699},
            {"id": "LINE2", "item": {"id": "BIRYANI"}, "name": "Chicken Biryani",
             "price": 1699},
            {"id": "LINE3", "item": {"id": "BIRYANI"}, "name": "Chicken Biryani",
             "price": 1799},
            {"id": "LINE4", "item": {"id": "BIRYANI"}, "name": "Chicken Biryani",
             "price": 1699, "unitQty": 2500},
        ]},
    })
    normalized = normalize_readback(order)
    assert [line.quantity for line in normalized.items] == [2, 1, 1]
    assert normalized.total == Decimal("31.98")


def test_readback_names_prefer_clover_then_catalog_and_preserve_modifiers() -> None:
    order = CloverOrderReadback.model_validate({
        "id": "ORDER1", "total": 1748,
        "lineItems": {"elements": [
            {"id": "LINE1", "item": {"id": "LASSI"}, "name": "Clover Lassi",
             "price": 499},
            {"id": "LINE1B", "item": {"id": "LASSI"}, "price": 499},
            {"id": "LINE2", "item": {"id": "BIRYANI"}, "price": 1699},
            {"id": "LINE3", "item": {"id": "BIRYANI"}, "price": 1699},
            {"id": "LINE4", "item": {"id": "LASSI", "name": "Reference Lassi"},
             "price": 499, "modifications": {"elements": [
                 {"modifier": {"id": "SWEET"}, "name": "Sweet", "amount": 25},
             ]}},
            {"id": "LINE5", "item": {"id": "UNKNOWN"}, "price": 300},
        ]},
    })
    normalized = normalize_readback(order, {
        "BIRYANI": "Chicken Biryani", "LASSI": "Catalog Lassi",
    })
    assert [(line.item_id, line.name, line.quantity) for line in normalized.items] == [
        ("LASSI", "Clover Lassi", 2),
        ("BIRYANI", "Chicken Biryani", 2),
        ("LASSI", "Reference Lassi", 1),
        ("UNKNOWN", "Item", 1),
    ]
    assert normalized.items[2].modifiers[0].id == "SWEET"


@pytest.mark.asyncio
async def test_nameless_append_resolves_repeated_lines_with_one_bulk_lookup(clover) -> None:
    calls, state = clover
    session = await create()
    assert (await add(session["session_id"], "BIRYANI", 1, "first-key-123")).status_code == 200
    state["line_overrides"][0] = {"name": None}
    state["line_overrides"][1] = {"name": None}
    state["line_overrides"][2] = {"name": None}
    before = len(calls)
    response = await add(session["session_id"], "LASSI", 2, "second-key-123")
    assert response.status_code == 200, response.text
    assert [(line["item_id"], line["name"], line["quantity"]) for line in
            response.json()["order"]["items"]] == [
                ("BIRYANI", "Chicken Biryani", 1), ("LASSI", "Mango Lassi", 2),
            ]
    inventory_gets = [call for call in calls[before:] if call.method == "GET" and
                      call.url.path.endswith("/items")]
    assert len(inventory_gets) == 2  # One append validation, one batched name lookup.


@pytest.mark.asyncio
async def test_nameless_first_order_uses_existing_normalized_catalog_cache(clover) -> None:
    calls, state = clover
    session = await create()
    cache = app.state.menu_cache
    cache._value = MenuResponse.model_validate({"categories": [{
        "id": "C1", "name": "Mains", "display_order": 1,
        "items": [{"id": "BIRYANI", "name": "Cached Chicken Biryani",
                   "price": 16.99}],
    }]})
    cache._scope = ("synthetic-merchant", "sandbox_token")
    cache._expires_at = time.monotonic() + 60
    state["order_overrides"]["lineItems"] = {"elements": [
        {"id": "LINE0", "item": {"id": "BIRYANI"}, "price": 1699},
    ]}
    response = await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    assert response.status_code == 200, response.text
    assert response.json()["order"]["items"][0]["name"] == "Cached Chicken Biryani"
    inventory_gets = [call for call in calls if call.method == "GET" and
                      call.url.path.endswith("/items")]
    assert len(inventory_gets) == 1  # Atomic validation only; cache supplies readback name.


@pytest.mark.asyncio
async def test_confirmed_modified_readback_resolves_name_without_reposting(clover) -> None:
    calls, state = clover
    session = await create()
    assert (await add(session["session_id"], "BIRYANI", 1, "first-key-123")).status_code == 200
    state["lassi_modifiers"] = True
    state["final_readback_fail_once"] = True
    path = f"/api/table-sessions/{session['session_id']}/items"
    body = {"items": [{"item_id": "LASSI", "quantity": 1,
                       "modifier_ids": ["SWEET"]}]}
    headers = {"Idempotency-Key": "second-key-123"}
    assert (await request("POST", path, json=body, headers=headers)).status_code == 503
    state["line_overrides"][1] = {"name": None}
    before = len(calls)
    response = await request("POST", path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    line = response.json()["order"]["items"][1]
    assert (line["item_id"], line["name"], line["quantity"], line["unit_price"]) == (
        "LASSI", "Mango Lassi", 1, 4.99,
    )
    assert line["modifiers"] == [{"id": "SWEET", "name": "Sweet", "price_delta": 0.25}]
    assert calls[before:] and all(call.method == "GET" for call in calls[before:])


@pytest.mark.asyncio
async def test_oauth_reconnect_and_no_secret_logging(clover, caplog) -> None:
    calls, state = clover
    class Provider:
        invalidated = False

        async def get_access_token(self):
            return "synthetic-oauth-secret"

        async def invalidate(self):
            self.invalidated = True

    provider = Provider()
    state["provider"] = provider
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, clover_merchant_id="synthetic-merchant",
        clover_order_type_id="TYPE1", clover_auth_mode="oauth",
        clover_app_id="synthetic-app",
    )
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["append_result"] = 401
    with caplog.at_level(logging.INFO):
        response = await add(session["session_id"], "NAAN", 1, "second-key-123")
    assert response.status_code == 503
    assert provider.invalidated is True
    assert "synthetic-oauth-secret" not in caplog.text
    assert "private provider detail" not in caplog.text
    assert "second-key-123" not in caplog.text


@pytest.mark.asyncio
async def test_append_recalculates_1699_plus_499_and_replay_is_cached(clover) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    first = await add(session["session_id"], "LASSI", 1, "second-key-123")
    count = len(calls)
    replay = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert len(calls) == count
    assert state["total"] == 2198
    assert first.json()["order"]["total"] == 21.98
    update = next(call for call in calls if call.method == "POST" and
                  call.url.path.endswith("/orders/ORDER1"))
    assert json.loads(update.content) == {"total": 2198}
    assert update.url.path == "/v3/merchants/synthetic-merchant/orders/ORDER1"
    assert update.headers["content-type"] == "application/json"
    assert [call.method for call in calls if call.url.path.endswith("/orders/ORDER1")][-2:] == [
        "POST", "GET"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("update_response,expected_response_total", [
    ({"id": "ORDER1", "total": 2198}, "2198"),
    ({"id": "ORDER1", "total": 1699}, "1699"),
    ("empty", "None"),
    ({"id": "ORDER1"}, "None"),
])
async def test_update_response_variants_followed_by_final_readback(
    clover, caplog, update_response, expected_response_total,
) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["total"] = 1699
    state["total_update_response"] = update_response
    with caplog.at_level(logging.INFO):
        result = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert result.status_code == 200
    assert result.json()["order"]["total"] == 21.98
    assert "calculated_total_cents=2198" in caplog.text
    assert "http_status=200" in caplog.text
    assert f"response_total_cents={expected_response_total}" in caplog.text
    assert "final_total_cents=2198" in caplog.text
    assert "synthetic-token" not in caplog.text
    assert [call.method for call in calls if call.url.path.endswith("/orders/ORDER1")][-2:] == [
        "POST", "GET"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [400, httpx.ReadTimeout("private token-like text")])
async def test_total_update_failure_never_reappends(clover, failure, caplog) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["total_update_result"] = failure
    with caplog.at_level(logging.INFO):
        first = await add(session["session_id"], "LASSI", 1, "second-key-123")
        replay = await add(session["session_id"], "LASSI", 1, "second-key-123")
        new_key = await add(session["session_id"], "TEA", 1, "third-key-123")
    assert (first.status_code, replay.status_code, new_key.status_code) == (502, 409, 409)
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1/line_items")
               for call in calls) == 1
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1")
               for call in calls) == 1
    assert "private" not in caplog.text + first.text
    assert "synthetic-token" not in caplog.text


@pytest.mark.asyncio
async def test_append_modifier_tax_and_service_charge_use_full_order(clover) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["line_overrides"] = {
        1: {"modifications": {"elements": [{"modifier": {"id": "EXTRA"}, "name": "Extra", "amount": 100}]},
            "taxRates": {"elements": [{"id": "TAX", "rate": 1000000}]}}
    }
    state["order_overrides"] = {"serviceCharge": {"percentageDecimal": 100000}}
    second = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert second.status_code == 200
    # 1699 + (499 + 100) = 2298; 10% tax on Lassi = 60;
    # 10% service charge on discounted subtotal = 230.
    assert state["total"] == 2588
    update = next(call for call in calls if call.method == "POST" and
                  call.url.path.endswith("/orders/ORDER1"))
    assert json.loads(update.content) == {"total": 2588}


@pytest.mark.asyncio
async def test_missing_tax_details_never_posts_total(clover) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["order_overrides"] = {"totalTaxAmount": 100}
    first = await add(session["session_id"], "LASSI", 1, "second-key-123")
    replay = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert (first.status_code, replay.status_code) == (409, 409)
    assert not any(call.method == "POST" and call.url.path.endswith("/orders/ORDER1")
                   for call in calls)
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1/line_items")
               for call in calls) == 1


@pytest.mark.asyncio
async def test_mismatched_final_clover_total_requires_reconciliation(clover, caplog) -> None:
    calls, state = clover
    session = await create()
    await add(session["session_id"], "BIRYANI", 1, "first-key-123")
    state["total_update_response"] = {"id": "ORDER1", "total": 1699}
    state["readback_total_override"] = 1699
    with caplog.at_level(logging.INFO):
        first = await add(session["session_id"], "LASSI", 1, "second-key-123")
    replay = await add(session["session_id"], "LASSI", 1, "second-key-123")
    assert (first.status_code, replay.status_code) == (503, 503)
    assert first.json()["code"] == replay.json()["code"] == "ORDER_CONFIRMATION_PENDING"
    assert "check=total_mismatch" in caplog.text
    assert "response_total_cents=1699" in caplog.text
    assert "final_total_cents=1699" in caplog.text
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1")
               for call in calls) == 1
    with sqlite3.connect(app.state.table_session_service.repository.path) as db:
        mutation = db.execute(
            "SELECT state,response_json FROM table_session_mutations "
            "WHERE session_id=? AND key_hash=?",
            (session["session_id"], hashlib.sha256(b"second-key-123").hexdigest()),
        ).fetchone()
    assert mutation[0] == "applied"
    assert json.loads(mutation[1])["phase"] == "readback_pending"


def test_total_calculator_components_and_rounding() -> None:
    order = {"currency": "USD", "discounts": {"elements": [{"percentage": 10}]},
             "serviceCharge": {"percentageDecimal": 100000}}
    lines = [{
        "price": 1000, "modifications": {"elements": [{"amount": 200}]},
        "discounts": {"elements": [{"percentage": 10}, {"amount": -80}]},
        "taxRates": {"elements": [{"id": "TAX", "rate": 1000000}]},
    }]
    assert calculate_order_total(order, lines) == 1080
    grouped = {"currency": "USD"}
    lines = [{"price": 5, "taxRates": {"elements": [{"id": "TAX", "rate": 500000}]}}]
    assert calculate_order_total(grouped, lines * 2) == 11
    assert calculate_order_total({"currency": "USD", "isVat": True}, lines * 2) == 10


@pytest.mark.parametrize("order,lines", [
    ({"currency": "USD"}, [{"name": "missing price"}]),
    ({"currency": "USD", "discounts": {"elements": [{"amount": -100}]}},
     [{"price": 500}, {"price": 500}]),
    ({"currency": "USD", "serviceCharge": {}}, [{"price": 500}]),
    ({"currency": "EUR"}, [{"price": 500}]),
    ({"currency": "USD"}, [{"price": 500, "taxRates": {"elements": [{"rate": 1000000}]}}]),
])
def test_total_calculator_fails_closed(order, lines) -> None:
    with pytest.raises(UnsupportedOrderCalculationError):
        calculate_order_total(order, lines)


@pytest.mark.asyncio
async def test_public_customer_replay_and_order_more_use_one_clover_order(clover) -> None:
    calls, state = clover
    settings = Settings(
        _env_file=None, clover_merchant_id="synthetic-merchant",
        clover_order_type_id="TYPE1",
        table_token_signing_key=Fernet.generate_key().decode(),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    token = TableTokenService(settings).issue("T12")
    boot = await request("POST", "/api/public/table-sessions",
                         json={"table_token": token})
    assert boot.status_code == 200
    session_id = boot.json()["session_id"]
    path = f"/api/public/table-sessions/{session_id}/orders"
    first_body = {"table_token": token, "items": [{"item_id": "BIRYANI", "quantity": 1}]}
    headers = {"Idempotency-Key": "first-public-key-123"}
    first = await request("POST", path, json=first_body, headers=headers)
    replay = await request("POST", path, json=first_body, headers=headers)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    state["line_overrides"][1] = {"name": None}
    second = await request("POST", path, json={"table_token": token,
                                               "items": [{"item_id": "LASSI", "quantity": 1}]},
                           headers={"Idempotency-Key": "second-public-key-123"})
    assert second.status_code == 200
    assert second.json()["order"]["total"] == 21.98
    assert [(line["item_id"], line["name"]) for line in second.json()["order"]["items"]] == [
        ("BIRYANI", "Chicken Biryani"), ("LASSI", "Mango Lassi"),
    ]
    assert "ORDER1" not in second.text
    assert state["lines"] == ["BIRYANI", "LASSI"]
    assert sum(call.method == "POST" and call.url.path.endswith("/atomic_order/orders")
               for call in calls) == 1
    assert sum(call.method == "POST" and call.url.path.endswith("/orders/ORDER1/line_items")
               for call in calls) == 1
