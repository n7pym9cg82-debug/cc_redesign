import logging


def configure_logging() -> None:
    """Configure concise application logging without sensitive configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Uvicorn access messages and HTTPX URLs can contain authorization codes,
    # state, cookies, or headers. Suppress those channels; main logs route paths.
    # Parent sinks also catch httpcore child loggers even at debug level.
    for name in ("uvicorn.access", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)
    logging.getLogger("uvicorn.access").disabled = True

