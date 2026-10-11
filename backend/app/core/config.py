import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import AnyHttpUrl, Field, SecretStr, TypeAdapter, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.integrations.clover.exceptions import CloverConfigurationError
from app.integrations.clover.oauth_models import OAuthIdentity

SANDBOX_API_URL = "https://apisandbox.dev.clover.com"
SANDBOX_AUTHORIZE_URL = "https://sandbox.dev.clover.com/oauth/v2/authorize"
SANDBOX_TOKEN_URL = f"{SANDBOX_API_URL}/oauth/v2/token"
SANDBOX_REFRESH_URL = f"{SANDBOX_API_URL}/oauth/v2/refresh"
LOCAL_DATABASE = Path(__file__).resolve().parents[2] / ".local" / "clover-oauth.sqlite3"
LOCAL_ORDER_DATABASE = Path(__file__).resolve().parents[2] / ".local" / "orders.sqlite3"
LOCAL_TABLE_SESSION_DATABASE = Path(__file__).resolve().parents[2] / ".local" / "table-sessions.sqlite3"


@dataclass(frozen=True)
class OAuthConfiguration:
    identity: OAuthIdentity
    app_secret: SecretStr = field(repr=False)
    authorize_url: str
    token_url: str
    refresh_url: str
    callback_url: str
    completion_url: str
    encryption_key: SecretStr = field(repr=False)
    database_path: Path


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: str = "development"
    app_name: str = "Restaurant Platform API"
    table_token_signing_key: SecretStr | None = None
    table_token_previous_keys: SecretStr | None = None
    table_token_version: int = Field(default=1, ge=1)
    frontend_origin: AnyHttpUrl | None = None
    frontend_origins: str | None = None
    frontend_public_url: AnyHttpUrl | None = None
    internal_api_key: SecretStr | None = None
    admin_username: str | None = None
    admin_password_hash: SecretStr | None = None
    admin_session_secret: SecretStr | None = None
    admin_session_hours: int = Field(default=8, ge=1, le=24)

    clover_env: str = "sandbox"
    clover_auth_mode: Literal["sandbox_token", "oauth"] = "sandbox_token"
    clover_oauth_enabled: bool = False
    clover_base_url: AnyHttpUrl = AnyHttpUrl("https://apisandbox.dev.clover.com")
    clover_merchant_id: str | None = None
    clover_api_token: SecretStr | None = None
    clover_order_type_id: str | None = None
    menu_cache_ttl_seconds: int = Field(default=60, ge=0, le=3600)

    clover_app_id: str | None = None
    clover_app_secret: SecretStr | None = None
    clover_oauth_authorize_url: AnyHttpUrl | None = None
    clover_oauth_token_url: AnyHttpUrl | None = None
    clover_oauth_refresh_url: AnyHttpUrl | None = None
    clover_token_encryption_key: SecretStr | None = None
    public_base_url: AnyHttpUrl | None = None

    @field_validator(
        "clover_oauth_authorize_url",
        "clover_oauth_token_url",
        "clover_oauth_refresh_url",
        "public_base_url",
        "frontend_origin",
        "frontend_public_url",
        mode="before",
    )
    @classmethod
    def normalize_optional_url(cls, value: object) -> object:
        """Treat blank optional URLs as unconfigured; validate all other values."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("frontend_origin", "frontend_public_url")
    @classmethod
    def validate_frontend_origin(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and (
            value.username is not None or value.password is not None
            or value.query is not None or value.fragment is not None
            or value.path not in (None, "", "/")
            or (value.host is not None and "*" in value.host)
            or (value.scheme == "http" and value.host not in ("localhost", "127.0.0.1"))
        ):
            raise ValueError("Frontend URL must be an exact HTTP(S) origin")
        return value

    @field_validator("frontend_origins")
    @classmethod
    def validate_frontend_origins(cls, value: str | None) -> str | None:
        if value is None:
            return None
        origins = []
        adapter = TypeAdapter(AnyHttpUrl)
        for entry in value.split(","):
            entry = entry.strip()
            if entry:
                origin = cls.validate_frontend_origin(adapter.validate_python(entry))
                origins.append(str(origin).rstrip("/"))
        return ",".join(dict.fromkeys(origins)) if origins else None

    @property
    def allowed_frontend_origins(self) -> tuple[str, ...]:
        if self.frontend_origins is not None:
            return tuple(self.frontend_origins.split(","))
        if self.frontend_origin is not None:
            return (str(self.frontend_origin).rstrip("/"),)
        return ()

    def require_local_sandbox(self) -> None:
        if self.app_env != "development" or self.clover_env != "sandbox":
            raise CloverConfigurationError(
                "This Clover integration supports development with Sandbox only"
            )

    def require_inventory_credentials(self) -> tuple[str, str]:
        """Return inventory credentials or fail without revealing secret values."""
        self.require_local_sandbox()
        if self.clover_auth_mode != "sandbox_token":
            raise CloverConfigurationError("Sandbox token mode is not selected")
        if not self.clover_merchant_id or not self.clover_api_token:
            raise ValueError("Clover inventory credentials are not configured")
        token = self.clover_api_token.get_secret_value()
        if not token.strip() or not re.fullmatch(r"[A-Za-z0-9_-]+", self.clover_merchant_id):
            raise ValueError("Clover inventory credentials are not configured")
        return self.clover_merchant_id, token

    def require_oauth_configuration(self) -> OAuthConfiguration:
        """Validate only when OAuth is used; never include configuration values."""
        self.require_local_sandbox()
        valid_ids = all(
            value and re.fullmatch(r"[A-Za-z0-9_-]+", value)
            for value in (self.clover_merchant_id, self.clover_app_id)
        )
        if (
            not valid_ids
            or not self.clover_app_secret
            or not self.clover_app_secret.get_secret_value().strip()
            or not self.clover_token_encryption_key
        ):
            raise CloverConfigurationError("Clover OAuth is not configured")
        urls = (
            (self.clover_oauth_authorize_url, SANDBOX_AUTHORIZE_URL),
            (self.clover_oauth_token_url, SANDBOX_TOKEN_URL),
            (self.clover_oauth_refresh_url, SANDBOX_REFRESH_URL),
        )
        if any(str(value) != expected for value, expected in urls):
            raise CloverConfigurationError("Clover OAuth requires Sandbox endpoints")
        if str(self.clover_base_url).rstrip("/") != SANDBOX_API_URL:
            raise CloverConfigurationError("Clover OAuth requires the Sandbox API")
        public = self.public_base_url
        if (
            public is None
            or public.scheme != "https"
            or public.username is not None
            or public.password is not None
            or public.query is not None
            or public.fragment is not None
            or public.path not in (None, "", "/")
        ):
            raise CloverConfigurationError("PUBLIC_BASE_URL must be an HTTPS origin")
        try:
            Fernet(self.clover_token_encryption_key.get_secret_value().encode("ascii"))
        except (ValueError, TypeError, UnicodeError):
            raise CloverConfigurationError("OAuth encryption key is invalid") from None
        origin = str(public).rstrip("/")
        return OAuthConfiguration(
            identity=OAuthIdentity(
                merchant_id=self.clover_merchant_id,
                app_id=self.clover_app_id,
                environment=self.clover_env,
            ),
            app_secret=self.clover_app_secret,
            authorize_url=str(self.clover_oauth_authorize_url),
            token_url=str(self.clover_oauth_token_url),
            refresh_url=str(self.clover_oauth_refresh_url),
            callback_url=f"{origin}/clover/callback",
            completion_url=f"{origin}/clover/complete",
            encryption_key=self.clover_token_encryption_key,
            database_path=LOCAL_DATABASE,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

