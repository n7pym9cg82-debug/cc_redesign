"""Small signed-cookie admin session boundary for the single-staff MVP."""

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request

from app.core.config import Settings

COOKIE_NAME = "chennai_admin_session"
COOKIE_PATH = "/api/admin"
MIN_PBKDF2_ITERATIONS = 100_000
_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_login_guard = Lock()


def configured(settings: Settings) -> bool:
    return bool(
        settings.admin_username and settings.admin_username.strip()
        and settings.admin_password_hash
        and settings.admin_session_secret
        and len(settings.admin_session_secret.get_secret_value()) >= 32
        and bool(settings.admin_session_secret.get_secret_value().strip())
        and settings.admin_password_hash.get_secret_value().startswith("pbkdf2_sha256$")
        and bool(settings.allowed_frontend_origins)
    )


def require_configuration(settings: Settings) -> None:
    if not configured(settings):
        raise HTTPException(503, "Admin login is not configured")


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, expected = stored.split("$", 3)
        iterations = int(rounds)
        if (algorithm != "pbkdf2_sha256" or iterations < MIN_PBKDF2_ITERATIONS
                or iterations > 2_000_000 or len(salt) < 32):
            return False
        salt_bytes, expected_bytes = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(expected_bytes) != 32:
            return False
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
    return hmac.compare_digest(actual, expected_bytes)


def hash_password(password: str, *, iterations: int = 600_000) -> str:
    """Used by the local setup helper and synthetic tests, never by a request."""
    if len(password) < 12 or iterations < MIN_PBKDF2_ITERATIONS:
        raise ValueError("Password and sufficient iterations are required")
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def require_same_origin(request: Request, settings: Settings) -> None:
    require_configuration(settings)
    supplied = request.headers.get("Origin", "")
    if not any(hmac.compare_digest(supplied, expected)
               for expected in settings.allowed_frontend_origins):
        raise HTTPException(403, "Admin request origin is not allowed")


def check_login_rate(request: Request) -> None:
    """Single-process MVP guard; a shared edge limiter is still needed at scale."""
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _login_guard:
        attempts = _login_attempts[key]
        while attempts and attempts[0] < now - 300:
            attempts.popleft()
        if len(attempts) >= 20:
            raise HTTPException(429, "Too many sign-in attempts")
        attempts.append(now)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def issue_session(settings: Settings) -> str:
    require_configuration(settings)
    issued = int(time.time())
    payload = json.dumps({
        "u": settings.admin_username, "iat": issued,
        "exp": issued + settings.admin_session_hours * 3600,
        "sid": secrets.token_urlsafe(24),
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64(payload)
    signature = hmac.new(
        settings.admin_session_secret.get_secret_value().encode("utf-8"),
        encoded.encode("ascii"), hashlib.sha256,
    ).digest()
    return f"{encoded}.{_b64(signature)}"


def require_admin(request: Request, settings: Settings) -> str:
    require_configuration(settings)
    cookie = request.cookies.get(COOKIE_NAME, "")
    if len(cookie) > 2048 or cookie.count(".") != 1:
        raise HTTPException(401, "Admin authentication required")
    encoded, signature = cookie.split(".", 1)
    if (re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None
            or re.fullmatch(r"[A-Za-z0-9_-]+", signature) is None):
        raise HTTPException(401, "Admin authentication required")
    try:
        expected = hmac.new(
            settings.admin_session_secret.get_secret_value().encode("utf-8"),
            encoded.encode("ascii"), hashlib.sha256,
        ).digest()
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        payload = json.loads(base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ))
    except (ValueError, UnicodeError, TypeError, binascii.Error):
        raise HTTPException(401, "Admin authentication required") from None
    now = int(time.time())
    if (not hmac.compare_digest(actual, expected)
            or not isinstance(payload, dict)
            or payload.get("u") != settings.admin_username
            or not isinstance(payload.get("iat"), int)
            or not isinstance(payload.get("exp"), int)
            or not isinstance(payload.get("sid"), str)
            or len(payload["sid"]) < 24
            or payload["iat"] > now + 60
            or payload["exp"] <= now
            or payload["exp"] - payload["iat"] > settings.admin_session_hours * 3600):
        raise HTTPException(401, "Admin authentication required")
    return settings.admin_username
