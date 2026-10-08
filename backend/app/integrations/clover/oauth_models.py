"""Validated, secret-safe values shared by OAuth services and token stores."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


@dataclass(frozen=True)
class OAuthIdentity:
    merchant_id: str
    app_id: str
    environment: str


class TokenPair(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, hide_input_in_errors=True, extra="ignore"
    )

    access_token: SecretStr
    refresh_token: SecretStr
    access_token_expiration: int = Field(gt=0)
    refresh_token_expiration: int = Field(gt=0)

    @field_validator("access_token", "refresh_token", mode="before")
    @classmethod
    def require_nonblank_token(cls, value: object) -> object:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("Token must be a nonempty string")
        return value

    def validate_expirations(self, now: int) -> None:
        """Use the provider's timestamps, never an assumed token lifetime."""
        if (
            self.access_token_expiration <= now
            or self.refresh_token_expiration <= now
            or self.refresh_token_expiration < self.access_token_expiration
        ):
            raise ValueError("Clover returned invalid token expirations")


@dataclass(frozen=True)
class StoredConnection:
    identity: OAuthIdentity
    tokens: TokenPair
    status: Literal["active", "refreshing", "reconnect_required"]
    updated_at: int
    version: int
