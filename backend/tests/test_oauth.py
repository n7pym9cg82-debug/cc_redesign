"""OAuth routes exercise only synthetic credentials and temporary storage."""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.integrations.clover.dependencies import get_oauth_service
from app.integrations.clover.oauth_service import OAuthService
from app.integrations.clover.token_store import SQLiteTokenStore
from app.main import app


ORIGIN = "https://restaurant.example.test"
COOKIE_NAME = "__Host-clover-oauth"
MERCHANT = "synthetic-merchant"
APP_ID = "synthetic-app"
APP_SECRET = "synthetic-app-secret-not-a-credential"
ACCESS_TOKEN = "synthetic-returned-access-token"
REFRESH_TOKEN = "synthetic-returned-refresh-token"
CODE = "synthetic-authorization-code"
NOW = 2_000_000_000
KEY = base64.urlsafe_b64encode(b"x" * 32).decode()


@dataclass
class OAuthHarness:
    settings: Settings
    store: SQLiteTokenStore
    clock: list[float] = field(default_factory=lambda: [float(NOW)])
    requests: list[httpx.Request] = field(default_factory=list)
    token_status: int = 200
    verification_status: int = 200
    fail_exchange: bool = False
    fail_verification: bool = False
    token_response: object = field(
        default_factory=lambda: {
            "access_token": ACCESS_TOKEN,
            "access_token_expiration": NOW + 3600,
            "refresh_token": REFRESH_TOKEN,
            "refresh_token_expiration": NOW + 86400,
        }
    )
    verification_response: object = field(default_factory=lambda: {"elements": []})

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "apisandbox.dev.clover.com"
        if request.method == "POST" and request.url.path == "/oauth/v2/token":
            assert request.url.query == b""
            assert request.headers["content-type"] == "application/json"
            assert all(0 < value <= 10 for value in request.extensions["timeout"].values())
            assert json.loads(request.content) == {
                "client_id": APP_ID,
                "client_secret": APP_SECRET,
                "code": CODE,
            }
            # Give competing callback tasks a chance to reach atomic consumption.
            await asyncio.sleep(0)
            if self.fail_exchange:
                raise httpx.ReadTimeout("synthetic timeout " + CODE, request=request)
            return httpx.Response(self.token_status, json=self.token_response)
        if request.method == "GET" and request.url.path == f"/v3/merchants/{MERCHANT}/items":
            assert request.url.params["limit"] == "1"
            assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
            if self.fail_verification:
                raise httpx.ReadTimeout("synthetic timeout " + ACCESS_TOKEN, request=request)
            return httpx.Response(self.verification_status, json=self.verification_response)
        raise AssertionError("Unexpected mocked OAuth request")

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


@pytest.fixture
def oauth(tmp_path: Path) -> Iterator[OAuthHarness]:
    settings = Settings(
        _env_file=None,
        clover_auth_mode="oauth",
        clover_oauth_enabled=True,
        clover_merchant_id=MERCHANT,
        clover_app_id=APP_ID,
        clover_app_secret=APP_SECRET,
        clover_api_token="synthetic-forbidden-fallback-token",
        clover_oauth_authorize_url="https://sandbox.dev.clover.com/oauth/v2/authorize",
        clover_oauth_token_url="https://apisandbox.dev.clover.com/oauth/v2/token",
        clover_oauth_refresh_url="https://apisandbox.dev.clover.com/oauth/v2/refresh",
        clover_token_encryption_key=KEY,
        public_base_url=ORIGIN,
    )
    store = SQLiteTokenStore(
        tmp_path / "oauth.sqlite3",
        SecretStr(KEY),
        app_env="development",
        environment="sandbox",
    )
    harness = OAuthHarness(settings=settings, store=store)
    service = OAuthService(
        settings.require_oauth_configuration(),
        store,
        http_client_factory=harness.http_client,
        clock=lambda: harness.clock[0],
    )
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oauth_service] = lambda: service
    try:
        yield harness
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest_asyncio.fixture
async def browser(oauth: OAuthHarness) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, follow_redirects=False
    ) as client:
        yield client


async def launch(browser: httpx.AsyncClient) -> tuple[str, str]:
    response = await browser.get("/clover/launch")
    assert response.status_code == 302
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    nonce = browser.cookies[COOKIE_NAME]
    assert state and nonce and state != nonce
    return state, nonce


def callback_params(state: str, **changes: str) -> dict[str, str]:
    return {"state": state, "code": CODE, "merchant_id": MERCHANT, "client_id": APP_ID, **changes}


def assert_oauth_headers(response: httpx.Response, *, cleared: bool = False) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    if cleared:
        assert "max-age=0" in response.headers["set-cookie"].lower()
        assert COOKIE_NAME in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_launch_uses_only_trusted_urls_and_secure_cookie(
    browser: httpx.AsyncClient, oauth: OAuthHarness
) -> None:
    response = await browser.get(
        "/clover/launch",
        params={"merchant_id": "attacker", "redirect_uri": "https://attacker.test/", "next": "https://attacker.test/"},
        headers={"host": "attacker.test", "x-forwarded-host": "attacker.test", "x-forwarded-proto": "http"},
    )
    assert response.status_code == 302
    target = urlsplit(response.headers["location"])
    assert (target.scheme, target.netloc, target.path) == (
        "https", "sandbox.dev.clover.com", "/oauth/v2/authorize"
    )
    params = parse_qs(target.query)
    assert set(params) == {"client_id", "response_type", "redirect_uri", "state"}
    assert params["client_id"] == [APP_ID]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == [ORIGIN + "/clover/callback"]
    assert len(params["state"][0]) >= 32
    cookie = response.headers["set-cookie"]
    assert COOKIE_NAME in cookie
    assert "httponly" in cookie.lower()
    assert "secure" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert "path=/" in cookie.lower()
    assert "domain=" not in cookie.lower()
    max_age = next(part for part in cookie.lower().split(";") if "max-age=" in part)
    assert 0 < int(max_age.split("=", 1)[1]) <= 600
    assert APP_SECRET not in response.headers["location"]
    assert_oauth_headers(response)
    assert oauth.requests == []


@pytest.mark.asyncio
async def test_launch_rejects_unsolicited_authorization_code(browser: httpx.AsyncClient, oauth: OAuthHarness) -> None:
    response = await browser.get("/clover/launch", params={"code": CODE, "merchant_id": MERCHANT})
    assert response.status_code == 400
    assert "location" not in response.headers
    assert oauth.requests == []
    assert CODE not in response.text
    assert_oauth_headers(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_state", "wrong_state", "missing_cookie", "wrong_cookie", "expired_state", "missing_code", "missing_merchant", "wrong_merchant", "wrong_app"])
async def test_callback_rejects_invalid_authorization(
    browser: httpx.AsyncClient, oauth: OAuthHarness, case: str
) -> None:
    state, _ = await launch(browser)
    params = callback_params(state)
    headers = {}
    if case == "missing_state":
        params.pop("state")
    elif case == "wrong_state":
        params["state"] = "synthetic-unknown-state"
    elif case == "missing_cookie":
        browser.cookies.clear()
    elif case == "wrong_cookie":
        headers["cookie"] = COOKIE_NAME + "=synthetic-wrong-browser"
    elif case == "expired_state":
        oauth.clock[0] += 601
    elif case == "missing_code":
        params.pop("code")
    elif case == "missing_merchant":
        params.pop("merchant_id")
    elif case == "wrong_merchant":
        params["merchant_id"] = "synthetic-wrong-merchant"
    elif case == "wrong_app":
        params["client_id"] = "synthetic-wrong-app"
    response = await browser.get("/clover/callback", params=params, headers=headers)
    assert response.status_code == 400
    assert "location" not in response.headers
    assert oauth.requests == []
    assert CODE not in response.text
    assert state not in response.text
    assert_oauth_headers(response, cleared=True)


@pytest.mark.asyncio
async def test_merchant_denial_is_sanitized_and_consumes_state(browser: httpx.AsyncClient, oauth: OAuthHarness) -> None:
    state, nonce = await launch(browser)
    response = await browser.get(
        "/clover/callback",
        params={"state": state, "error": "synthetic-provider-error", "error_description": "synthetic-private-description"},
    )
    assert response.status_code == 400
    assert "synthetic-provider-error" not in response.text
    assert "synthetic-private-description" not in response.text
    assert_oauth_headers(response, cleared=True)
    replay = await browser.get(
        "/clover/callback", params=callback_params(state), headers={"cookie": COOKIE_NAME + "=" + nonce}
    )
    assert replay.status_code == 400
    assert oauth.requests == []


@pytest.mark.asyncio
async def test_success_verifies_configured_merchant_then_redirects_query_free(
    browser: httpx.AsyncClient, oauth: OAuthHarness
) -> None:
    state, nonce = await launch(browser)
    response = await browser.get("/clover/callback", params=callback_params(state))
    assert response.status_code == 303
    assert response.headers["location"] == ORIGIN + "/clover/complete"
    assert [request.method for request in oauth.requests] == ["POST", "GET"]
    assert_oauth_headers(response, cleared=True)
    assert COOKIE_NAME not in browser.cookies
    record = await oauth.store.load_connection(oauth.settings.require_oauth_configuration().identity)
    assert record is not None
    assert record.tokens.access_token.get_secret_value() == ACCESS_TOKEN
    assert record.tokens.refresh_token.get_secret_value() == REFRESH_TOKEN

    replay = await browser.get(
        "/clover/callback", params=callback_params(state), headers={"cookie": COOKIE_NAME + "=" + nonce}
    )
    assert replay.status_code == 400
    assert len(oauth.requests) == 2

    completion = await browser.get("/clover/complete", params={"code": CODE, "token": ACCESS_TOKEN})
    assert completion.status_code == 200
    assert_oauth_headers(completion)
    assert all(secret not in completion.text for secret in (CODE, ACCESS_TOKEN, REFRESH_TOKEN, APP_SECRET, state, nonce))


@pytest.mark.asyncio
async def test_client_id_may_be_omitted_from_callback(browser: httpx.AsyncClient) -> None:
    state, _ = await launch(browser)
    params = callback_params(state)
    params.pop("client_id")
    response = await browser.get("/clover/callback", params=params)
    assert response.status_code == 303


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_response", [
    {},
    [],
    {"access_token": ACCESS_TOKEN},
    {"access_token": "", "access_token_expiration": NOW + 3600, "refresh_token": REFRESH_TOKEN, "refresh_token_expiration": NOW + 86400},
    {"access_token": ACCESS_TOKEN, "access_token_expiration": NOW - 1, "refresh_token": REFRESH_TOKEN, "refresh_token_expiration": NOW + 86400},
    {"access_token": ACCESS_TOKEN, "access_token_expiration": NOW + 3600, "refresh_token": REFRESH_TOKEN, "refresh_token_expiration": NOW - 1},
    {"access_token": ACCESS_TOKEN, "access_token_expiration": "not-a-timestamp", "refresh_token": REFRESH_TOKEN, "refresh_token_expiration": NOW + 86400},
    {"access_token": ACCESS_TOKEN, "access_token_expiration": True, "refresh_token": REFRESH_TOKEN, "refresh_token_expiration": NOW + 86400},
])
async def test_malformed_token_response_never_persists(
    browser: httpx.AsyncClient, oauth: OAuthHarness, bad_response: object
) -> None:
    oauth.token_response = bad_response
    state, _ = await launch(browser)
    response = await browser.get("/clover/callback", params=callback_params(state))
    assert response.status_code == 502
    assert len(oauth.requests) == 1
    assert await oauth.store.load_connection(oauth.settings.require_oauth_configuration().identity) is None
    assert all(secret not in response.text for secret in (CODE, ACCESS_TOKEN, REFRESH_TOKEN, APP_SECRET))
    assert_oauth_headers(response, cleared=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exchange_400", "exchange_500", "exchange_timeout", "verification_401", "verification_500", "verification_timeout", "verification_malformed"])
async def test_failed_exchange_or_merchant_verification_never_persists_or_retries(
    browser: httpx.AsyncClient, oauth: OAuthHarness, failure: str
) -> None:
    if failure.startswith("exchange_"):
        if failure == "exchange_timeout":
            oauth.fail_exchange = True
        else:
            oauth.token_status = int(failure.rsplit("_", 1)[1])
            oauth.token_response = {"error": APP_SECRET + CODE + ACCESS_TOKEN}
    elif failure == "verification_timeout":
        oauth.fail_verification = True
    elif failure == "verification_malformed":
        oauth.verification_response = {"unrelated": "not inventory"}
    else:
        oauth.verification_status = int(failure.rsplit("_", 1)[1])
        oauth.verification_response = {"error": ACCESS_TOKEN}
    state, nonce = await launch(browser)
    response = await browser.get("/clover/callback", params=callback_params(state))
    assert response.status_code == 502
    assert await oauth.store.load_connection(oauth.settings.require_oauth_configuration().identity) is None
    assert len([r for r in oauth.requests if r.method == "POST"]) == 1
    assert all(secret not in response.text for secret in (CODE, ACCESS_TOKEN, REFRESH_TOKEN, APP_SECRET))
    assert_oauth_headers(response, cleared=True)
    replay = await browser.get(
        "/clover/callback", params=callback_params(state), headers={"cookie": COOKIE_NAME + "=" + nonce}
    )
    assert replay.status_code == 400
    assert len([r for r in oauth.requests if r.method == "POST"]) == 1


@pytest.mark.asyncio
async def test_concurrent_callbacks_exchange_code_only_once(browser: httpx.AsyncClient, oauth: OAuthHarness) -> None:
    state, nonce = await launch(browser)
    responses = await asyncio.gather(*[
        browser.get("/clover/callback", params=callback_params(state), headers={"cookie": COOKIE_NAME + "=" + nonce})
        for _ in range(2)
    ])
    assert sorted(response.status_code for response in responses) == [303, 400]
    assert len([r for r in oauth.requests if r.method == "POST"]) == 1
    assert len([r for r in oauth.requests if r.method == "GET"]) == 1


@pytest.mark.asyncio
async def test_oauth_logs_omit_secrets_and_callback_query(
    browser: httpx.AsyncClient, oauth: OAuthHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    state, nonce = await launch(browser)
    await browser.get("/clover/callback", params=callback_params(state))
    # Error logging must not leak raw provider bodies or exception messages either.
    oauth.fail_exchange = True
    failed_state, failed_nonce = await launch(browser)
    await browser.get("/clover/callback", params=callback_params(failed_state))
    assert all(secret not in caplog.text for secret in (
        CODE, APP_SECRET, ACCESS_TOKEN, REFRESH_TOKEN, state, nonce, failed_state, failed_nonce,
        "synthetic-forbidden-fallback-token", "?state=", "&code=", "&merchant_id=", "&client_id=",
    ))
    assert "/clover/callback" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/clover/launch", "/clover/callback", "/clover/complete"])
async def test_disabled_oauth_routes_are_sanitized(path: str) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get(path, params={"code": CODE, "state": "synthetic-disabled-state"})
    assert response.status_code == 501
    assert "location" not in response.headers
    assert CODE not in response.text
    assert_oauth_headers(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/clover/launch", "/clover/callback"])
async def test_enabled_incomplete_configuration_returns_sanitized_503(path: str) -> None:
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, clover_oauth_enabled=True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            response = await client.get(path)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
    assert response.status_code == 503
    assert "location" not in response.headers
    assert response.json() == {"detail": "Clover OAuth is not configured"}
    assert_oauth_headers(response)
