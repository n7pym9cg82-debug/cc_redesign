"""Public QR boundary with synthetic keys and no real Clover transport."""

import sys
import importlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.api.menu import get_clover_client
from app.api.table_sessions import get_table_session_service
from app.core.config import Settings, get_settings
from app.main import app
from app.public.tokens import TableTokenError, TableTokenService
from scripts.generate_table_qr import main as generate_table_qr
from app.table_sessions.models import SessionItemsResponse, SessionOrderResponse, TableSessionResponse


def settings(**overrides) -> Settings:
    values = {"table_token_signing_key": Fernet.generate_key().decode(),
              "clover_merchant_id": "synthetic-merchant"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_valid_tampered_version_and_rotated_tokens() -> None:
    first_key = Fernet.generate_key().decode()
    second_key = Fernet.generate_key().decode()
    old = TableTokenService(settings(table_token_signing_key=first_key))
    token = old.issue(" t12 ")
    assert old.resolve(token) == "T12"
    with pytest.raises(TableTokenError):
        old.resolve(token[:-5] + "AAAAA")
    newer = TableTokenService(settings(
        table_token_signing_key=second_key,
        table_token_previous_keys=first_key,
    ))
    assert newer.resolve(token) == "T12"
    with pytest.raises(TableTokenError):
        TableTokenService(settings(
            table_token_signing_key=first_key, clover_merchant_id="other-merchant",
        )).resolve(token)
    with pytest.raises(TableTokenError):
        TableTokenService(settings(
            table_token_signing_key=second_key,
            table_token_previous_keys=first_key,
            table_token_version=2,
        )).resolve(token)


def test_frontend_origins_are_exact_and_nonlocal_http_is_rejected() -> None:
    assert str(settings(frontend_origin="http://localhost:3000").frontend_origin).rstrip("/") == (
        "http://localhost:3000"
    )
    for origin in ("http://example.com", "https://example.com/path", "https://*.example.com"):
        with pytest.raises(ValidationError):
            settings(frontend_origin=origin)


def test_qr_generator_uses_synthetic_settings_and_ignored_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["generate_table_qr", "T12"])
    config = settings(frontend_public_url="http://localhost:3000")
    output = generate_table_qr(settings=config, output_dir=tmp_path)
    assert output.name == "T12-v1.png"
    assert output.read_bytes().startswith(b"\x89PNG")
    printed = capsys.readouterr().out
    assert "http://localhost:3000/order/" in printed
    assert config.table_token_signing_key.get_secret_value() not in printed


@pytest.mark.asyncio
async def test_public_resolution_bootstrap_and_order_boundary() -> None:
    config = settings()
    token = TableTokenService(config).issue("T12")
    other_token = TableTokenService(config).issue("T13")
    session = TableSessionResponse(
        session_id="a" * 32, table="T12", status="open", clover_order_id="ORDERSECRET",
        created_at="now", updated_at="now", closed_at=None,
    )

    class FakeService:
        placed = 0

        async def create_or_resume(self, _settings, table):
            assert table == "T12"
            return session

        async def get(self, _settings, session_id):
            assert session_id == session.session_id
            return session

        async def add_items(self, **kwargs):
            self.placed += 1
            assert kwargs["body"].items[0].item_id == "BIRYANI"
            assert kwargs["key"] == "synthetic-key-123"
            return SessionItemsResponse(
                **session.model_dump(),
                order=SessionOrderResponse(
                    id="ORDERSECRET", currency="USD", subtotal=None, tax=None,
                    total="16.99", items=[],
                ),
            )

    fake = FakeService()
    app.dependency_overrides[get_settings] = lambda: config
    app.dependency_overrides[get_table_session_service] = lambda: fake
    app.dependency_overrides[get_clover_client] = lambda: SimpleNamespace()
    resolved = await request("GET", f"/api/public/table/{token}")
    assert resolved.status_code == 200
    assert resolved.json() == {"table": "T12", "display_name": "Table 12", "valid": True}
    assert (await request("GET", f"/api/public/table/{token[:-5]}AAAAA")).status_code == 404
    boot = await request("POST", "/api/public/table-sessions", json={"table_token": token})
    assert boot.status_code == 200
    assert boot.json() == {"session_id": "a" * 32, "table": {"display_name": "Table 12"},
                           "status": "open"}
    encoded_token = quote(token, safe="")
    assert encoded_token != token  # Fernet padding is escaped in a printed QR URL.
    encoded_get = await request("GET", f"/api/public/table/{encoded_token}")
    encoded_boot = await request("POST", "/api/public/table-sessions",
                                 json={"table_token": encoded_token})
    assert encoded_get.status_code == 200
    assert encoded_boot.status_code == 200
    assert encoded_get.json()["table"] == resolved.json()["table"]
    assert encoded_boot.json()["table"]["display_name"] == boot.json()["table"]["display_name"]
    assert (await request("POST", "/api/public/table-sessions",
                          json={"table_token": encoded_token[:-5] + "AAAAA"})).status_code == 404
    assert (await request("POST", "/api/public/table-sessions",
                          json={"tableToken": token})).status_code == 422
    assert (await request("POST", "/api/public/table-sessions",
                          json={"table_token": f" {token}"})).status_code == 404
    path = f"/api/public/table-sessions/{session.session_id}/orders"
    body = {"table_token": token, "items": [{"item_id": "BIRYANI", "quantity": 1}]}
    assert (await request("POST", path, json={**body, "table": "T13"})).status_code == 422
    assert (await request("POST", path, json={**body, "clover_order_id": "EVIL"})).status_code == 422
    assert (await request("POST", path, json={**body, "price": 1})).status_code == 422
    assert (await request("POST", path, json={"table_token": other_token,
                                             "items": body["items"]})).status_code == 404
    placed = await request("POST", path, json=body,
                           headers={"Idempotency-Key": "synthetic-key-123"})
    assert placed.status_code == 200
    assert placed.json()["order"]["total"] == 16.99
    assert "ORDERSECRET" not in placed.text
    assert fake.placed == 1


@pytest.mark.asyncio
async def test_get_and_bootstrap_reject_same_wrong_merchant_version_and_revoked_key() -> None:
    key = Fernet.generate_key().decode()
    config = settings(table_token_signing_key=key)
    token = TableTokenService(config).issue("T12")
    app.dependency_overrides[get_settings] = lambda: config
    accepted_get = await request("GET", f"/api/public/table/{token}")
    accepted_post = await request("POST", "/api/public/table-sessions",
                                  json={"table_token": token})
    assert accepted_get.status_code == accepted_post.status_code == 200
    assert accepted_get.json()["table"] == "T12"
    assert accepted_post.json()["table"]["display_name"] == "Table 12"
    for invalid in (
        settings(table_token_signing_key=key, clover_merchant_id="other-merchant"),
        settings(table_token_signing_key=key, table_token_version=2),
        settings(table_token_signing_key=Fernet.generate_key().decode()),
    ):
        app.dependency_overrides[get_settings] = lambda invalid=invalid: invalid
        get = await request("GET", f"/api/public/table/{token}")
        post = await request("POST", "/api/public/table-sessions",
                             json={"table_token": token})
        assert get.status_code == post.status_code == 404
        assert get.json() == post.json() == {
            "detail": "Table link is invalid or no longer active"
        }


@pytest.mark.asyncio
async def test_frontend_mode_gates_legacy_mutation_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("app.main")
    monkeypatch.setattr(main_module, "settings", settings(
        frontend_origin="http://localhost:3000", internal_api_key="synthetic-internal-key",
    ))
    blocked = await request("POST", "/api/table-sessions", json={"table": "T12"})
    assert blocked.status_code == 403
    assert "synthetic-internal-key" not in blocked.text
    blocked_order = await request("POST", "/api/orders", json={})
    assert blocked_order.status_code == 403
    allowed = await request("POST", "/api/table-sessions", json={},
                            headers={"X-Internal-API-Key": "synthetic-internal-key"})
    assert allowed.status_code == 422  # Reached request validation, not the gate.
