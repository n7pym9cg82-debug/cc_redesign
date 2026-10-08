import base64

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import (
    SANDBOX_AUTHORIZE_URL,
    SANDBOX_REFRESH_URL,
    SANDBOX_TOKEN_URL,
    Settings,
)
from app.integrations.clover.exceptions import CloverConfigurationError


def configured_settings(**changes: object) -> Settings:
    values = {
        "clover_merchant_id": "synthetic-merchant",
        "clover_app_id": "synthetic-app",
        "clover_app_secret": "synthetic-secret",
        "clover_token_encryption_key": base64.urlsafe_b64encode(b"x" * 32).decode(),
        "clover_oauth_authorize_url": SANDBOX_AUTHORIZE_URL,
        "clover_oauth_token_url": SANDBOX_TOKEN_URL,
        "clover_oauth_refresh_url": SANDBOX_REFRESH_URL,
        "public_base_url": "https://restaurant.example.test",
    }
    values.update(changes)
    return Settings(**values)


def test_default_mode_preserves_sandbox_token_with_blank_oauth() -> None:
    settings = Settings(
        clover_merchant_id="synthetic-merchant",
        clover_api_token="synthetic-token",
        clover_oauth_authorize_url="",
        clover_oauth_token_url="",
        clover_oauth_refresh_url="",
        public_base_url="",
    )
    assert settings.clover_auth_mode == "sandbox_token"
    assert settings.clover_oauth_enabled is False
    assert settings.require_inventory_credentials() == ("synthetic-merchant", "synthetic-token")


def test_oauth_configuration_builds_only_fixed_callback_and_completion() -> None:
    config = configured_settings().require_oauth_configuration()
    assert config.callback_url == "https://restaurant.example.test/clover/callback"
    assert config.completion_url == "https://restaurant.example.test/clover/complete"
    assert config.identity.environment == "sandbox"
    assert "synthetic-secret" not in repr(config)


@pytest.mark.parametrize(
    "field",
    ["clover_merchant_id", "clover_app_id", "clover_app_secret", "clover_token_encryption_key",
     "clover_oauth_authorize_url", "clover_oauth_token_url", "clover_oauth_refresh_url", "public_base_url"],
)
def test_missing_required_oauth_configuration_is_rejected(field: str) -> None:
    with pytest.raises(CloverConfigurationError):
        configured_settings(**{field: None}).require_oauth_configuration()


@pytest.mark.parametrize(
    "url",
    ["http://example.test", "https://user:synthetic-secret@example.test", "https://example.test/path",
     "https://example.test?code=synthetic-code", "https://example.test#fragment"],
)
def test_public_base_must_be_https_origin(url: str) -> None:
    with pytest.raises(CloverConfigurationError):
        configured_settings(public_base_url=url).require_oauth_configuration()


@pytest.mark.parametrize(
    ("field", "value"),
    [("clover_oauth_authorize_url", "https://evil.example.test/oauth/v2/authorize"),
     ("clover_oauth_token_url", "https://api.clover.com/oauth/v2/token"),
     ("clover_oauth_refresh_url", "https://apisandbox.dev.clover.com/oauth/v2/refresh?extra=value"),
     ("clover_base_url", "https://api.clover.com"),
     ("clover_merchant_id", "../other-merchant"),
     ("clover_app_id", " "),
     ("clover_app_secret", " "),
     ("clover_token_encryption_key", "synthetic-invalid-key")],
)
def test_unsafe_oauth_configuration_is_rejected(field: str, value: str) -> None:
    with pytest.raises(CloverConfigurationError) as error:
        configured_settings(**{field: value}).require_oauth_configuration()
    if value.strip():
        assert value not in str(error.value)


@pytest.mark.parametrize("mode", ["sandbox_token", "oauth"])
@pytest.mark.parametrize(("app_env", "clover_env"), [("production", "sandbox"), ("development", "production")])
def test_current_auth_modes_reject_production(mode: str, app_env: str, clover_env: str) -> None:
    settings = configured_settings(
        clover_auth_mode=mode, app_env=app_env, clover_env=clover_env, clover_api_token="synthetic-token"
    )
    with pytest.raises(CloverConfigurationError):
        if mode == "oauth":
            settings.require_oauth_configuration()
        else:
            settings.require_inventory_credentials()


def test_oauth_mode_cannot_request_sandbox_token_fallback() -> None:
    settings = configured_settings(clover_auth_mode="oauth", clover_api_token="synthetic-token")
    with pytest.raises(CloverConfigurationError):
        settings.require_inventory_credentials()


def test_secret_settings_are_redacted_and_validation_hides_inputs() -> None:
    settings = configured_settings(clover_api_token="synthetic-token")
    assert isinstance(settings.clover_api_token, SecretStr)
    assert isinstance(settings.clover_app_secret, SecretStr)
    assert "synthetic-token" not in repr(settings)
    assert "synthetic-secret" not in settings.model_dump_json()
    with pytest.raises(ValidationError) as error:
        configured_settings(clover_app_secret={"secret": "synthetic-sensitive-input"})
    assert "synthetic-sensitive-input" not in str(error.value)
