import logging

import pytest

from app.core.logging import configure_logging


def test_unsafe_access_and_http_debug_log_channels_are_suppressed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging()
    caplog.set_level(logging.DEBUG)
    for name in ("uvicorn.access", "httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
        logging.getLogger(name).error(
            "/clover/callback?code=synthetic-code&state=synthetic-state "
            "Authorization: Bearer synthetic-access Cookie: synthetic-browser"
        )

    assert "synthetic-" not in caplog.text
    assert "/clover/callback?" not in caplog.text
    assert logging.getLogger("uvicorn.access").disabled
