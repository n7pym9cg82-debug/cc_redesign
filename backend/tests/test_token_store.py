"""Only temporary databases and synthetic tokens/keys are used here."""

import asyncio
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from app.integrations.clover.oauth_models import OAuthIdentity, TokenPair
from app.integrations.clover.token_store import SQLiteTokenStore, TokenStoreError


NOW = 1_800_000_000
IDENTITY = OAuthIdentity("synthetic-merchant", "synthetic-app", "sandbox")
STATE_HASH = hashlib.sha256(b"synthetic-state").hexdigest()
NONCE_HASH = hashlib.sha256(b"synthetic-browser-nonce").hexdigest()


@pytest.fixture
def encryption_key() -> SecretStr:
    # Ephemeral test-only key; never persisted or obtained from the environment.
    return SecretStr(Fernet.generate_key().decode("ascii"))


@pytest.fixture
def store(tmp_path: Path, encryption_key: SecretStr) -> SQLiteTokenStore:
    return SQLiteTokenStore(
        tmp_path / "tokens.sqlite3", encryption_key,
        app_env="development", environment="sandbox",
    )


def token_pair(suffix: str = "initial") -> TokenPair:
    return TokenPair(
        access_token=f"synthetic-private-access-token-{suffix}",
        refresh_token=f"synthetic-private-refresh-token-{suffix}",
        access_token_expiration=NOW + 1200,
        refresh_token_expiration=NOW + 7200,
    )


async def add_pending(store: SQLiteTokenStore) -> None:
    await store.add_pending(
        state_hash=STATE_HASH, nonce_hash=NONCE_HASH, identity=IDENTITY,
        expires_at=NOW + 600, now=NOW,
    )


@pytest.mark.asyncio
async def test_encrypted_tokens_survive_new_store_instance(
    store: SQLiteTokenStore, tmp_path: Path, encryption_key: SecretStr,
) -> None:
    tokens = token_pair()
    assert await store.save_connection(IDENTITY, tokens, NOW)

    restarted = SQLiteTokenStore(
        tmp_path / "tokens.sqlite3", encryption_key,
        app_env="development", environment="sandbox",
    )
    connection = await restarted.load_connection(IDENTITY)
    assert connection is not None
    assert connection.tokens == tokens
    assert connection.status == "active"
    assert connection.version == 1
    assert connection.updated_at == NOW
    assert "synthetic-private" not in repr(connection)
    for artifact in tmp_path.iterdir():
        assert tokens.access_token.get_secret_value().encode() not in artifact.read_bytes()
        assert tokens.refresh_token.get_secret_value().encode() not in artifact.read_bytes()
    assert not (tmp_path / "tokens.sqlite3-wal").exists()


@pytest.mark.asyncio
async def test_pending_hashes_survive_restart_and_are_consumed_once(
    store: SQLiteTokenStore, tmp_path: Path, encryption_key: SecretStr,
) -> None:
    await add_pending(store)
    contents = (tmp_path / "tokens.sqlite3").read_bytes()
    assert b"synthetic-state" not in contents
    assert b"synthetic-browser-nonce" not in contents
    restarted = SQLiteTokenStore(
        tmp_path / "tokens.sqlite3", encryption_key,
        app_env="development", environment="sandbox",
    )
    assert await restarted.consume_pending(
        state_hash=STATE_HASH, nonce_hash=NONCE_HASH, identity=IDENTITY, now=NOW + 1,
    )
    assert not await store.consume_pending(
        state_hash=STATE_HASH, nonce_hash=NONCE_HASH, identity=IDENTITY, now=NOW + 2,
    )


@pytest.mark.asyncio
async def test_pending_concurrent_consumption_has_exactly_one_winner(store: SQLiteTokenStore) -> None:
    await add_pending(store)
    results = await asyncio.gather(*(
        store.consume_pending(
            state_hash=STATE_HASH, nonce_hash=NONCE_HASH, identity=IDENTITY, now=NOW + 1,
        )
        for _ in range(8)
    ))
    assert results.count(True) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state_hash", "nonce_hash", "identity", "now"),
    [
        ("f" * 64, NONCE_HASH, IDENTITY, NOW + 1),
        (STATE_HASH, "f" * 64, IDENTITY, NOW + 1),
        (STATE_HASH, NONCE_HASH, OAuthIdentity("wrong-merchant", "synthetic-app", "sandbox"), NOW + 1),
        (STATE_HASH, NONCE_HASH, OAuthIdentity("synthetic-merchant", "wrong-app", "sandbox"), NOW + 1),
        (STATE_HASH, NONCE_HASH, IDENTITY, NOW + 600),
        (STATE_HASH, NONCE_HASH, IDENTITY, NOW - 1),
    ],
)
async def test_pending_scope_binding_and_expiration(
    store: SQLiteTokenStore, state_hash: str, nonce_hash: str,
    identity: OAuthIdentity, now: int,
) -> None:
    await add_pending(store)
    assert not await store.consume_pending(
        state_hash=state_hash, nonce_hash=nonce_hash, identity=identity, now=now,
    )
    # Incorrect attempts do not consume a still-valid authorization.
    assert await store.consume_pending(
        state_hash=STATE_HASH, nonce_hash=NONCE_HASH, identity=IDENTITY, now=NOW + 1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [-1, 0, 601])
async def test_pending_window_is_at_most_ten_minutes(store: SQLiteTokenStore, window: int) -> None:
    with pytest.raises(TokenStoreError, match="pending authorization is invalid"):
        await store.add_pending(
            state_hash=STATE_HASH, nonce_hash=NONCE_HASH, identity=IDENTITY,
            expires_at=NOW + window, now=NOW,
        )


@pytest.mark.asyncio
async def test_pending_rejects_raw_values(store: SQLiteTokenStore) -> None:
    with pytest.raises(TokenStoreError):
        await store.add_pending(
            state_hash="raw-state-must-not-be-persisted", nonce_hash=NONCE_HASH,
            identity=IDENTITY, expires_at=NOW + 300, now=NOW,
        )


@pytest.mark.asyncio
async def test_refresh_compare_and_swap_replaces_both_tokens_and_expirations(
    store: SQLiteTokenStore,
) -> None:
    initial = token_pair()
    refreshed = TokenPair(
        access_token="synthetic-new-access", refresh_token="synthetic-new-refresh",
        access_token_expiration=NOW + 2400, refresh_token_expiration=NOW + 9000,
    )
    await store.save_connection(IDENTITY, initial, NOW)
    assert not await store.save_connection(IDENTITY, refreshed, NOW, expected_version=1)
    assert not await store.mark_refreshing(IDENTITY, 2, NOW)
    assert await store.mark_refreshing(IDENTITY, 1, NOW + 1)
    assert not await store.mark_refreshing(IDENTITY, 1, NOW + 1)
    assert await store.save_connection(IDENTITY, refreshed, NOW + 2, expected_version=1)
    connection = await store.load_connection(IDENTITY)
    assert connection is not None
    assert connection.tokens == refreshed
    assert connection.version == 2
    assert connection.status == "active"
    assert not await store.save_connection(IDENTITY, initial, NOW + 3, expected_version=1)
    assert not await store.mark_reconnect_required(IDENTITY, 1, NOW + 3)


@pytest.mark.asyncio
async def test_refreshing_status_is_durable_and_failure_preserves_tokens(
    store: SQLiteTokenStore, tmp_path: Path, encryption_key: SecretStr,
) -> None:
    initial = token_pair()
    await store.save_connection(IDENTITY, initial, NOW)
    assert await store.mark_refreshing(IDENTITY, 1, NOW + 1)
    restarted = SQLiteTokenStore(
        tmp_path / "tokens.sqlite3", encryption_key,
        app_env="development", environment="sandbox",
    )
    connection = await restarted.load_connection(IDENTITY)
    assert connection is not None and connection.status == "refreshing"
    assert connection.tokens == initial
    assert await restarted.mark_reconnect_required(IDENTITY, 1, NOW + 2)
    failed = await restarted.load_connection(IDENTITY)
    assert failed is not None and failed.status == "reconnect_required"
    assert failed.tokens == initial
    assert not await restarted.save_connection(IDENTITY, token_pair("late"), NOW + 3, expected_version=1)


@pytest.mark.asyncio
async def test_new_callback_connection_cannot_be_overwritten_by_older_refresh(
    store: SQLiteTokenStore,
) -> None:
    await store.save_connection(IDENTITY, token_pair(), NOW)
    await store.mark_refreshing(IDENTITY, 1, NOW + 1)
    callback_tokens = token_pair("reconnected")
    await store.save_connection(IDENTITY, callback_tokens, NOW + 2)
    assert not await store.save_connection(IDENTITY, token_pair("stale"), NOW + 3, expected_version=1)
    assert not await store.mark_reconnect_required(IDENTITY, 1, NOW + 4)
    connection = await store.load_connection(IDENTITY)
    assert connection is not None and connection.tokens == callback_tokens


@pytest.mark.asyncio
async def test_unknown_identity_cannot_load_connection(store: SQLiteTokenStore) -> None:
    await store.save_connection(IDENTITY, token_pair(), NOW)
    assert await store.load_connection(OAuthIdentity("another-merchant", "synthetic-app", "sandbox")) is None
    assert await store.load_connection(OAuthIdentity("synthetic-merchant", "another-app", "sandbox")) is None
    with pytest.raises(TokenStoreError):
        await store.load_connection(OAuthIdentity("synthetic-merchant", "synthetic-app", "production"))


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["ciphertext", "merchant", "expiration", "kind"])
async def test_corrupted_or_rebound_ciphertext_has_sanitized_error(
    store: SQLiteTokenStore, tmp_path: Path, tamper: str,
) -> None:
    await store.save_connection(IDENTITY, token_pair(), NOW)
    lookup = IDENTITY
    with closing(sqlite3.connect(tmp_path / "tokens.sqlite3")) as connection, connection:
        if tamper == "ciphertext":
            connection.execute("UPDATE oauth_connections SET encrypted_access_token = ?", (b"synthetic-secret-corruption",))
        elif tamper == "merchant":
            connection.execute("UPDATE oauth_connections SET merchant_id = ?", ("other-merchant",))
            lookup = OAuthIdentity("other-merchant", "synthetic-app", "sandbox")
        elif tamper == "expiration":
            connection.execute("UPDATE oauth_connections SET access_token_expiration = ?", (NOW + 2000,))
        else:
            connection.execute("UPDATE oauth_connections SET encrypted_access_token = encrypted_refresh_token")
    with pytest.raises(TokenStoreError) as error:
        await store.load_connection(lookup)
    assert str(error.value) == "OAuth storage is unavailable"
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_wrong_encryption_key_is_sanitized(
    store: SQLiteTokenStore, tmp_path: Path,
) -> None:
    await store.save_connection(IDENTITY, token_pair(), NOW)
    wrong_key_store = SQLiteTokenStore(
        tmp_path / "tokens.sqlite3", SecretStr(Fernet.generate_key().decode()),
        app_env="development", environment="sandbox",
    )
    with pytest.raises(TokenStoreError, match="^OAuth storage is unavailable$"):
        await wrong_key_store.load_connection(IDENTITY)


@pytest.mark.asyncio
async def test_unrepresentable_expiration_does_not_overwrite_connection(
    store: SQLiteTokenStore,
) -> None:
    original = token_pair()
    await store.save_connection(IDENTITY, original, NOW)
    oversized = TokenPair(
        access_token="synthetic-oversized-access", refresh_token="synthetic-oversized-refresh",
        access_token_expiration=2**100, refresh_token_expiration=2**101,
    )
    with pytest.raises(TokenStoreError, match="^OAuth storage is unavailable$"):
        await store.save_connection(IDENTITY, oversized, NOW)
    connection = await store.load_connection(IDENTITY)
    assert connection is not None and connection.tokens == original
    assert connection.version == 1


@pytest.mark.parametrize(
    ("app_env", "environment"),
    [("production", "sandbox"), ("development", "production"), ("production", "production")],
)
def test_local_adapter_rejects_production_before_creating_database(
    tmp_path: Path, encryption_key: SecretStr, app_env: str, environment: str,
) -> None:
    with pytest.raises(TokenStoreError, match="requires development sandbox"):
        SQLiteTokenStore(
            tmp_path / "tokens.sqlite3", encryption_key,
            app_env=app_env, environment=environment,
        )
    assert list(tmp_path.iterdir()) == []


def test_invalid_key_is_sanitized(tmp_path: Path) -> None:
    with pytest.raises(TokenStoreError) as error:
        SQLiteTokenStore(
            tmp_path / "tokens.sqlite3", SecretStr("synthetic-invalid-secret-key"),
            app_env="development", environment="sandbox",
        )
    assert str(error.value) == "OAuth storage encryption key is invalid"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token", ""), ("refresh_token", "  "), ("access_token", 123),
        ("refresh_token", None), ("access_token_expiration", "1800001200"),
        ("access_token_expiration", True), ("refresh_token_expiration", 1.5),
        ("refresh_token_expiration", 0), ("access_token_expiration", -1),
    ],
)
def test_token_values_and_expirations_are_strict(field: str, value: object) -> None:
    values = token_pair().model_dump()
    values[field] = value
    with pytest.raises(ValidationError) as error:
        TokenPair.model_validate(values)
    assert "input_value" not in str(error.value)
    assert "synthetic-private" not in str(error.value)


@pytest.mark.parametrize(
    ("access_expiry", "refresh_expiry"),
    [(NOW, NOW + 100), (NOW + 100, NOW), (NOW + 200, NOW + 100)],
)
def test_token_expiration_semantics(access_expiry: int, refresh_expiry: int) -> None:
    tokens = TokenPair(
        access_token="synthetic-access", refresh_token="synthetic-refresh",
        access_token_expiration=access_expiry, refresh_token_expiration=refresh_expiry,
    )
    with pytest.raises(ValueError, match="invalid token expirations"):
        tokens.validate_expirations(NOW)
