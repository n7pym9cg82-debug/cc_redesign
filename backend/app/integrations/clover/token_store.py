"""Durable encrypted storage for the single-process development sandbox.

The interface separates token persistence from OAuth and inventory requests so a
production store and managed encryption keys can replace this local adapter.
"""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Protocol, TypeVar

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from app.integrations.clover.oauth_models import (
    OAuthIdentity,
    StoredConnection,
    TokenPair,
)


class TokenStoreError(Exception):
    """A deliberately sanitized local storage error."""


class TokenStore(Protocol):
    async def add_pending(
        self, *, state_hash: str, nonce_hash: str, identity: OAuthIdentity,
        expires_at: int, now: int,
    ) -> None: ...

    async def consume_pending(
        self, *, state_hash: str, nonce_hash: str, identity: OAuthIdentity, now: int,
    ) -> bool: ...

    async def load_connection(self, identity: OAuthIdentity) -> StoredConnection | None: ...

    async def save_connection(
        self, identity: OAuthIdentity, tokens: TokenPair, now: int,
        *, expected_version: int | None = None,
    ) -> bool: ...

    async def mark_refreshing(
        self, identity: OAuthIdentity, expected_version: int, now: int,
    ) -> bool: ...

    async def mark_reconnect_required(
        self, identity: OAuthIdentity, expected_version: int, now: int,
    ) -> bool: ...


_T = TypeVar("_T")
_IDENTITY_WHERE = "merchant_id = ? AND app_id = ? AND environment = ?"


class SQLiteTokenStore:
    def __init__(
        self, path: Path, encryption_key: SecretStr,
        *, app_env: str, environment: str,
    ) -> None:
        if app_env != "development" or environment != "sandbox":
            raise TokenStoreError("Local OAuth storage requires development sandbox mode")
        try:
            self._cipher = Fernet(encryption_key.get_secret_value().encode("ascii"))
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise TokenStoreError("OAuth storage encryption key is invalid") from None
        self._path = Path(path)
        self._environment = environment

    def _identity_values(self, identity: OAuthIdentity) -> tuple[str, str, str]:
        if (
            identity.environment != self._environment
            or not identity.merchant_id.strip()
            or not identity.app_id.strip()
        ):
            raise TokenStoreError("OAuth storage identity is invalid")
        return identity.merchant_id, identity.app_id, identity.environment

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            # SQLite's default rollback journal contains only encrypted token
            # values too. This adapter deliberately does not enable WAL.
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_pending (
                    state_hash TEXT PRIMARY KEY,
                    nonce_hash TEXT NOT NULL,
                    merchant_id TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    consumed_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS oauth_connections (
                    merchant_id TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    encrypted_access_token BLOB NOT NULL,
                    encrypted_refresh_token BLOB NOT NULL,
                    access_token_expiration INTEGER NOT NULL,
                    refresh_token_expiration INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('active', 'refreshing', 'reconnect_required')
                    ),
                    updated_at INTEGER NOT NULL,
                    version INTEGER NOT NULL CHECK (version > 0),
                    PRIMARY KEY (merchant_id, app_id, environment)
                );
                """
            )
            return connection
        except Exception:
            connection.close()
            raise

    async def _run(self, action: Callable[[], _T]) -> _T:
        try:
            return await asyncio.to_thread(action)
        except (sqlite3.Error, OSError, InvalidToken, ValueError, TypeError, KeyError, OverflowError):
            raise TokenStoreError("OAuth storage is unavailable") from None

    @staticmethod
    def _valid_hash(value: str) -> bool:
        return (
            len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    async def add_pending(
        self, *, state_hash: str, nonce_hash: str, identity: OAuthIdentity,
        expires_at: int, now: int,
    ) -> None:
        scope = self._identity_values(identity)
        if (
            not self._valid_hash(state_hash)
            or not self._valid_hash(nonce_hash)
            or not 0 < expires_at - now <= 600
        ):
            raise TokenStoreError("OAuth pending authorization is invalid")

        def add() -> None:
            with closing(self._connect()) as connection, connection:
                connection.execute("DELETE FROM oauth_pending WHERE expires_at <= ?", (now,))
                connection.execute(
                    """INSERT INTO oauth_pending
                    (state_hash, nonce_hash, merchant_id, app_id, environment,
                     expires_at, created_at, consumed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (state_hash, nonce_hash, *scope, expires_at, now),
                )

        await self._run(add)

    async def consume_pending(
        self, *, state_hash: str, nonce_hash: str, identity: OAuthIdentity, now: int,
    ) -> bool:
        scope = self._identity_values(identity)

        def consume() -> bool:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    f"""UPDATE oauth_pending SET consumed_at = ?
                    WHERE state_hash = ? AND nonce_hash = ? AND {_IDENTITY_WHERE}
                    AND expires_at > ? AND created_at <= ? AND consumed_at IS NULL""",
                    (now, state_hash, nonce_hash, *scope, now, now),
                )
                return cursor.rowcount == 1

        return await self._run(consume)

    def _encrypt_token(
        self, identity: OAuthIdentity, kind: str, token: SecretStr, expiration: int,
    ) -> bytes:
        payload = {
            "identity": list(self._identity_values(identity)),
            "kind": kind,
            "expiration": expiration,
            "token": token.get_secret_value(),
        }
        return self._cipher.encrypt(json.dumps(payload).encode("utf-8"))

    def _decrypt_token(
        self, identity: OAuthIdentity, kind: str, ciphertext: bytes, expiration: int,
    ) -> str:
        payload = json.loads(self._cipher.decrypt(ciphertext))
        if (
            not isinstance(payload, dict)
            or payload.get("identity") != list(self._identity_values(identity))
            or payload.get("kind") != kind
            or payload.get("expiration") != expiration
        ):
            raise TokenStoreError("OAuth storage is unavailable")
        return payload["token"]

    async def load_connection(self, identity: OAuthIdentity) -> StoredConnection | None:
        scope = self._identity_values(identity)

        def load() -> StoredConnection | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    f"SELECT * FROM oauth_connections WHERE {_IDENTITY_WHERE}", scope,
                ).fetchone()
            if row is None:
                return None
            tokens = TokenPair(
                access_token=self._decrypt_token(
                    identity, "access", row["encrypted_access_token"], row["access_token_expiration"]
                ),
                refresh_token=self._decrypt_token(
                    identity, "refresh", row["encrypted_refresh_token"], row["refresh_token_expiration"]
                ),
                access_token_expiration=row["access_token_expiration"],
                refresh_token_expiration=row["refresh_token_expiration"],
            )
            return StoredConnection(
                identity=identity, tokens=tokens, status=row["status"],
                updated_at=row["updated_at"], version=row["version"],
            )

        return await self._run(load)

    async def save_connection(
        self, identity: OAuthIdentity, tokens: TokenPair, now: int,
        *, expected_version: int | None = None,
    ) -> bool:
        scope = self._identity_values(identity)
        try:
            tokens.validate_expirations(now)
        except ValueError:
            raise TokenStoreError("OAuth token expirations are invalid") from None

        def save() -> bool:
            encrypted = (
                self._encrypt_token(identity, "access", tokens.access_token, tokens.access_token_expiration),
                self._encrypt_token(identity, "refresh", tokens.refresh_token, tokens.refresh_token_expiration),
            )
            values = (*encrypted, tokens.access_token_expiration, tokens.refresh_token_expiration, now)
            with closing(self._connect()) as connection, connection:
                if expected_version is not None:
                    cursor = connection.execute(
                        f"""UPDATE oauth_connections SET encrypted_access_token = ?,
                        encrypted_refresh_token = ?, access_token_expiration = ?,
                        refresh_token_expiration = ?, updated_at = ?, status = 'active',
                        version = version + 1 WHERE {_IDENTITY_WHERE}
                        AND version = ? AND status = 'refreshing'""",
                        (*values, *scope, expected_version),
                    )
                else:
                    cursor = connection.execute(
                        """INSERT INTO oauth_connections (merchant_id, app_id, environment,
                        encrypted_access_token, encrypted_refresh_token, access_token_expiration,
                        refresh_token_expiration, updated_at, status, version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 1)
                        ON CONFLICT (merchant_id, app_id, environment) DO UPDATE SET
                        encrypted_access_token = excluded.encrypted_access_token,
                        encrypted_refresh_token = excluded.encrypted_refresh_token,
                        access_token_expiration = excluded.access_token_expiration,
                        refresh_token_expiration = excluded.refresh_token_expiration,
                        updated_at = excluded.updated_at, status = 'active',
                        version = oauth_connections.version + 1""",
                        (*scope, *values),
                    )
                return cursor.rowcount == 1

        return await self._run(save)

    async def mark_refreshing(
        self, identity: OAuthIdentity, expected_version: int, now: int,
    ) -> bool:
        return await self._set_status(identity, expected_version, now, "refreshing")

    async def mark_reconnect_required(
        self, identity: OAuthIdentity, expected_version: int, now: int,
    ) -> bool:
        return await self._set_status(identity, expected_version, now, "reconnect_required")

    async def _set_status(
        self, identity: OAuthIdentity, expected_version: int, now: int, status: str,
    ) -> bool:
        scope = self._identity_values(identity)

        def update() -> bool:
            active_only = " AND status = 'active'" if status == "refreshing" else ""
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    f"""UPDATE oauth_connections SET status = ?, updated_at = ?
                    WHERE {_IDENTITY_WHERE} AND version = ?{active_only}""",
                    (status, now, *scope, expected_version),
                )
                return cursor.rowcount == 1

        return await self._run(update)
