"""Explicit browser-origin allowlist; synthetic configuration and ASGI only."""

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import ValidationError

from app.admin import api as admin_api
from app.admin.auth import hash_password
from app.api import health
from app.core.config import Settings, get_settings
from app.main import configure_frontend_cors
from app.public import api as public_api

LOCAL = "http://localhost:3000"
CLOUDFLARE = "https://alter-engaged-indicated-kidney.trycloudflare.com"
OTHER = "https://other.trycloudflare.com"


def configured_app(settings: Settings) -> FastAPI:
    application = FastAPI()
    configure_frontend_cors(application, settings)
    application.include_router(health.router)
    application.include_router(public_api.router)
    application.include_router(admin_api.router)
    application.dependency_overrides[get_settings] = lambda: settings
    return application


def test_multi_origins_trim_empty_entries_and_override_legacy(monkeypatch) -> None:
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://legacy.example")
    monkeypatch.setenv("FRONTEND_ORIGINS", f" {LOCAL} , , {CLOUDFLARE} , {LOCAL} , ")
    settings = Settings(_env_file=None)
    assert settings.allowed_frontend_origins == (LOCAL, CLOUDFLARE)
    assert Settings(_env_file=None, frontend_origins=" , ").allowed_frontend_origins == (
        "https://legacy.example",
    )


def test_legacy_origin_and_invalid_entries() -> None:
    assert Settings(_env_file=None, frontend_origin=LOCAL).allowed_frontend_origins == (LOCAL,)
    for value in ("http://example.com", f"{LOCAL},https://*.trycloudflare.com",
                  f"{LOCAL},https://example.com/path", "not-an-origin"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, frontend_origins=value)


@pytest.mark.asyncio
async def test_cors_get_and_public_qr_preflight_are_exact_and_credentialed() -> None:
    settings = Settings(_env_file=None, frontend_origins=f"{LOCAL},{CLOUDFLARE}")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=configured_app(settings)), base_url="http://test",
    ) as client:
        for origin in (LOCAL, CLOUDFLARE):
            response = await client.get("/health", headers={"Origin": origin})
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == origin
            assert response.headers["access-control-allow-credentials"] == "true"
            preflight = await client.options("/api/public/table-sessions", headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers":
                    "content-type,idempotency-key,ngrok-skip-browser-warning",
            })
            assert preflight.status_code == 200
            assert preflight.headers["access-control-allow-origin"] == origin
            assert preflight.headers["access-control-allow-credentials"] == "true"
            allowed = preflight.headers["access-control-allow-headers"].lower()
            assert all(header in allowed for header in (
                "content-type", "idempotency-key", "ngrok-skip-browser-warning",
            ))
        for origin in (OTHER, f"{CLOUDFLARE}/"):
            response = await client.get("/health", headers={"Origin": origin})
            assert "access-control-allow-origin" not in response.headers
            preflight = await client.options("/api/public/table-sessions", headers={
                "Origin": origin, "Access-Control-Request-Method": "POST",
            })
            assert preflight.status_code == 400
            assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.asyncio
async def test_admin_login_checks_the_same_explicit_origins() -> None:
    settings = Settings(
        _env_file=None, app_env="development", frontend_origin="https://legacy.example",
        frontend_origins=f"{LOCAL},{CLOUDFLARE}",
        table_token_signing_key=Fernet.generate_key().decode(),
        admin_username="operator", admin_password_hash=hash_password(
            "synthetic-password", iterations=100_000,
        ), admin_session_secret="synthetic-admin-signing-secret-with-48-characters",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=configured_app(settings)), base_url="http://test",
    ) as client:
        for origin in (LOCAL, CLOUDFLARE):
            response = await client.post("/api/admin/login", headers={"Origin": origin},
                                         json={"username": "operator", "password": "synthetic-password"})
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == origin
            assert response.headers["access-control-allow-credentials"] == "true"
            assert "httponly" in response.headers["set-cookie"].lower()
        for origin in (OTHER, f"{CLOUDFLARE}/", "https://legacy.example"):
            response = await client.post("/api/admin/login", headers={"Origin": origin},
                                         json={"username": "operator", "password": "synthetic-password"})
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers
