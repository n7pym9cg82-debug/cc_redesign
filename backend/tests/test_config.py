from pathlib import Path

import pytest
from pydantic import AnyHttpUrl, ValidationError

from app.core.config import Settings

OPTIONAL_URL_FIELDS = (
    "clover_oauth_authorize_url",
    "clover_oauth_token_url",
    "clover_oauth_refresh_url",
    "public_base_url",
)


def test_optional_urls_can_be_omitted() -> None:
    settings = Settings()

    assert all(getattr(settings, field) is None for field in OPTIONAL_URL_FIELDS)
    assert settings.clover_merchant_id is None
    assert settings.clover_api_token is None


@pytest.mark.parametrize("value", [None, "", " ", "\t\r\n "])
def test_blank_optional_urls_become_none(value: str | None) -> None:
    settings = Settings(**{field: value for field in OPTIONAL_URL_FIELDS})

    assert all(getattr(settings, field) is None for field in OPTIONAL_URL_FIELDS)


@pytest.mark.parametrize("field", OPTIONAL_URL_FIELDS)
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_valid_optional_urls_are_validated(field: str, scheme: str) -> None:
    url = f"{scheme}://example.test/oauth"
    settings = Settings(**{field: url})

    assert isinstance(getattr(settings, field), AnyHttpUrl)
    assert str(getattr(settings, field)) == url


@pytest.mark.parametrize("field", OPTIONAL_URL_FIELDS)
@pytest.mark.parametrize("value", ["not-a-url", "ftp://example.test/oauth", "https://"])
def test_invalid_nonblank_optional_urls_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(**{field: value})

    assert [issue["loc"] for issue in error.value.errors()] == [(field,)]


@pytest.mark.parametrize("field", OPTIONAL_URL_FIELDS)
@pytest.mark.parametrize("value", ["", " \t "])
def test_blank_optional_urls_from_environment(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    monkeypatch.setenv(field.upper(), value)

    assert getattr(Settings(), field) is None


def test_temporary_dotenv_with_blank_optional_urls(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "CLOVER_MERCHANT_ID=synthetic-merchant\n"
        "CLOVER_API_TOKEN=synthetic-token\n"
        "CLOVER_OAUTH_AUTHORIZE_URL=\n"
        "CLOVER_OAUTH_TOKEN_URL=\n"
        'PUBLIC_BASE_URL="   "\n',
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv)

    assert all(getattr(settings, field) is None for field in OPTIONAL_URL_FIELDS)
    assert settings.require_inventory_credentials() == (
        "synthetic-merchant",
        "synthetic-token",
    )


@pytest.mark.parametrize("value", [None, "", " \t ", "not-a-url", "ftp://example.test"])
def test_clover_base_url_validation_is_unchanged(value: str | None) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(clover_base_url=value)

    assert [issue["loc"] for issue in error.value.errors()] == [("clover_base_url",)]
