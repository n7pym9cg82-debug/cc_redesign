"""Read-only Clover payment-state checks with synthetic HTTP responses."""

import httpx
import pytest

from app.integrations.clover.client import CloverClient
from app.integrations.clover.exceptions import CloverOrderNotFoundError, CloverUnavailableError


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["OPEN", "PARTIALLY_PAID", "PAID"])
async def test_order_payment_state_is_one_trusted_get(state: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "ORDER1", "paymentState": state})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with CloverClient(base_url="https://apisandbox.dev.clover.com",
                                merchant_id="synthetic-merchant", api_token="synthetic-token",
                                http_client=http) as clover:
            assert await clover.get_order_payment_state("ORDER1") == state
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path == "/v3/merchants/synthetic-merchant/orders/ORDER1"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"id": "WRONG", "paymentState": "PAID"},
    {"id": "ORDER1"},
    {"id": "ORDER1", "paymentState": "SOMETHING_ELSE"},
    {"id": "ORDER1", "paymentState": ["PAID"]},
])
async def test_invalid_payment_readback_never_looks_paid(payload: dict) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload),
    )) as http:
        async with CloverClient(base_url="https://apisandbox.dev.clover.com",
                                merchant_id="synthetic-merchant", api_token="synthetic-token",
                                http_client=http) as clover:
            with pytest.raises(CloverUnavailableError):
                await clover.get_order_payment_state("ORDER1")


@pytest.mark.asyncio
async def test_deleted_order_raises_safe_specific_read_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, json={"message": "untrusted provider text"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with CloverClient(base_url="https://apisandbox.dev.clover.com",
                                merchant_id="synthetic-merchant", api_token="synthetic-token",
                                http_client=http) as clover:
            with pytest.raises(CloverOrderNotFoundError, match="Clover order could not be found"):
                await clover.get_order_payment_state("ORDER1")
    assert len(requests) == 1 and requests[0].method == "GET"
