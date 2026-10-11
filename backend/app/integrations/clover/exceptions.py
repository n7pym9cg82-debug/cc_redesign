class CloverError(Exception):
    """Base exception for Clover integration failures."""


class CloverConfigurationError(CloverError):
    """Raised when required Clover configuration is missing."""


class CloverUnavailableError(CloverError):
    """Raised when Clover cannot provide a valid response."""


class CloverOrderNotFoundError(CloverUnavailableError):
    """The requested order does not exist in Clover."""


class CloverReadbackFailure(CloverUnavailableError):
    """Safe reason for an order GET/readback failure; never contains payloads."""

    def __init__(self, reason: str, http_status: int | None = None) -> None:
        super().__init__("Clover order readback is unavailable")
        self.reason = reason
        self.http_status = http_status


class CloverAuthenticationError(CloverUnavailableError):
    """Clover rejected the credential used for a merchant request."""


class CloverReconnectionRequiredError(CloverError):
    """Stored OAuth credentials cannot safely be reused; reconnect the app."""


class CloverOrderRejectedError(CloverError):
    """Clover definitely rejected an atomic order before creating it."""


class CloverOrderUncertainError(CloverError):
    """An atomic order may have been created; automatic retry is unsafe."""

