"""Isolate settings before test collection imports the application."""

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.core import config as config_module


def pytest_configure(config: pytest.Config) -> None:
    # An autouse fixture is too late: app.main loads settings during collection.
    isolation = pytest.MonkeyPatch()
    config.add_cleanup(isolation.undo)
    config.add_cleanup(get_settings.cache_clear)
    isolation.setitem(Settings.model_config, "env_file", None)
    settings_names = {name.upper() for name in Settings.model_fields}
    for name in tuple(os.environ):
        if name.upper() in settings_names:
            isolation.delenv(name)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_test(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Prevent network access and cached settings from leaking between tests."""

    async def reject_network(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        raise AssertionError("Tests must mock external HTTP requests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)
    # Even tests using the default dependency must never open real runtime data.
    monkeypatch.setattr(config_module, "LOCAL_DATABASE", tmp_path / ".local" / "oauth.sqlite3")
    monkeypatch.setattr(config_module, "LOCAL_ORDER_DATABASE", tmp_path / ".local" / "orders.sqlite3")
    monkeypatch.setattr(config_module, "LOCAL_TABLE_SESSION_DATABASE", tmp_path / ".local" / "table-sessions.sqlite3")
    from app.main import app

    monkeypatch.setattr(app.state, "clover_oauth_service", None, raising=False)
    monkeypatch.setattr(app.state, "order_service", None, raising=False)
    monkeypatch.setattr(app.state, "table_session_service", None, raising=False)
    monkeypatch.setattr(app.state, "menu_cache", None, raising=False)
    get_settings.cache_clear()
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()

