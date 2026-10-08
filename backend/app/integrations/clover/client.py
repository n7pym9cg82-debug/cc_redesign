import logging
import re
from typing import Protocol

import httpx
from pydantic import ValidationError

from app.integrations.clover.exceptions import (
    CloverAuthenticationError,
    CloverOrderRejectedError,
    CloverOrderUncertainError,
    CloverReadbackFailure,
    CloverReconnectionRequiredError,
    CloverUnavailableError,
)
from app.integrations.clover.models import (
    CloverInventoryResponse,
    CloverOrderReadback,
    CloverReadLineItems,
    CloverOrderType,
    CloverOrderTypeListing,
    CloverOrderTypesCollection,
)

logger = logging.getLogger(__name__)


class AccessTokenProvider(Protocol):
    async def get_access_token(self) -> str: ...

    async def invalidate(self) -> None: ...


class CloverClient:
    """Reusable asynchronous client for Clover merchant APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        merchant_id: str,
        api_token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        token_provider: AccessTokenProvider | None = None,
    ) -> None:
        self._merchant_id = merchant_id
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._token_provider = token_provider
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> "CloverClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def merchant_id(self) -> str:
        return self._merchant_id

    async def inventory_collection(
        self, resource: str, *, parent_id: str | None = None,
        limit: int = 100, offset: int = 0, expand: tuple[str, ...] = (),
    ) -> dict[str, object]:
        """One paged inventory read for the trusted merchant."""
        if resource not in {"categories", "items", "modifier_groups", "modifiers", "category_items", "item_modifier_groups"}:
            raise ValueError("Unsupported inventory resource")
        if (resource == "modifiers") != (parent_id is not None) or (
            parent_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parent_id)
        ) or not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("Invalid inventory read")
        if expand and (resource != "items" or set(expand) != {"categories", "modifierGroups"}):
            raise ValueError("Unsupported inventory expansion")
        token = await self._access_token()
        prefix = f"/v3/merchants/{self._merchant_id}"
        path = f"{prefix}/modifier_groups/{parent_id}/modifiers" if parent_id else f"{prefix}/{resource}"
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {token}", "User-Agent": "RestaurantPlatform/0.1 (development)"},
                params=[("limit", limit), ("offset", offset), *[("expand", field) for field in expand]],
                timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False,
            )
            if response.status_code != 200:
                response.raise_for_status()
                raise ValueError("Unexpected inventory status")
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
                raise ValueError("Invalid inventory collection")
            return payload
        except httpx.HTTPStatusError as exc:
            await self._handle_read_status(exc.response.status_code)
            raise AssertionError("unreachable")
        except (httpx.HTTPError, ValueError):
            raise CloverUnavailableError("Clover inventory is unavailable") from None

    async def inventory_item(self, item_id: str) -> dict[str, object]:
        """Read one item's category and modifier-group expansions."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item_id):
            raise ValueError("Invalid inventory item ID")
        token = await self._access_token()
        try:
            response = await self._client.get(
                f"{self._base_url}/v3/merchants/{self._merchant_id}/items/{item_id}",
                headers={"Authorization": f"Bearer {token}", "User-Agent": "RestaurantPlatform/0.1 (development)"},
                params=[("expand", "categories"), ("expand", "modifierGroups")],
                timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False,
            )
            if response.status_code != 200:
                response.raise_for_status()
                raise ValueError("Unexpected item status")
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("id") != item_id:
                raise ValueError("Invalid item readback")
            return payload
        except httpx.HTTPStatusError as exc:
            await self._handle_read_status(exc.response.status_code)
            raise AssertionError("unreachable")
        except (httpx.HTTPError, ValueError):
            raise CloverUnavailableError("Clover inventory is unavailable") from None

    async def inventory_create(self, resource: str, body: dict[str, object]) -> None:
        """One mutation, never retried; caller must re-read after success or ambiguity."""
        if resource not in {"categories", "items", "modifier_groups", "category_items", "item_modifier_groups"}:
            raise ValueError("Unsupported inventory resource")
        await self._inventory_post(resource, body)

    async def inventory_create_modifier(self, group_id: str, body: dict[str, object]) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", group_id):
            raise ValueError("Invalid modifier group ID")
        await self._inventory_post(f"modifier_groups/{group_id}/modifiers", body)

    async def _inventory_post(self, path: str, body: dict[str, object]) -> None:
        token = await self._access_token()
        try:
            response = await self._client.post(
                f"{self._base_url}/v3/merchants/{self._merchant_id}/{path}",
                headers={"Authorization": f"Bearer {token}", "User-Agent": "RestaurantPlatform/0.1 (development)"}, json=body,
                timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False,
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise CloverOrderUncertainError("Clover inventory mutation outcome requires reconciliation")
        except httpx.HTTPError:
            raise CloverOrderUncertainError("Clover inventory mutation outcome requires reconciliation") from None

    async def get_inventory_items(
        self, *, limit: int | None = None, item_ids: list[str] | None = None
    ) -> CloverInventoryResponse:
        path = f"/v3/merchants/{self._merchant_id}/items"
        token = (
            await self._token_provider.get_access_token()
            if self._token_provider is not None
            else self._api_token
        )
        if not token:
            raise CloverAuthenticationError("Clover credentials are missing")
        logger.info("Requesting Clover inventory items")
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    **({"limit": limit} if limit is not None else {}),
                    **({"filter": "id in (" + ",".join(f"'{item_id}'" for item_id in item_ids) + ")"}
                       if item_ids is not None else {}),
                    **({"expand": "modifierGroups"} if item_ids is not None else {}),
                } or None,
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or "elements" not in payload:
                raise ValueError("Missing inventory collection")
            inventory = CloverInventoryResponse.model_validate(payload)
        except httpx.HTTPStatusError as exc:
            logger.warning("Clover inventory HTTP failure: %d", exc.response.status_code)
            if exc.response.status_code in (401, 403):
                if self._token_provider is not None:
                    await self._token_provider.invalidate()
                    raise CloverReconnectionRequiredError("Clover reconnection required") from None
                raise CloverAuthenticationError("Clover credentials were rejected") from None
            raise CloverUnavailableError("Clover inventory is unavailable") from None
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            logger.warning("Clover inventory request failed: %s", type(exc).__name__)
            raise CloverUnavailableError("Clover inventory is unavailable") from None
        logger.info("Received %d Clover inventory items", len(inventory.elements))
        return inventory

    async def get_order_type(self, order_type_id: str) -> CloverOrderType:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", order_type_id):
            raise CloverUnavailableError("Clover order type is not configured")
        token = await self._access_token()
        path = f"/v3/merchants/{self._merchant_id}/order_types/{order_type_id}"
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
            )
            response.raise_for_status()
            order_type = CloverOrderType.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            await self._handle_read_status(exc.response.status_code)
            raise AssertionError("unreachable")
        except (httpx.HTTPError, ValueError, ValidationError):
            raise CloverUnavailableError("Clover order type is unavailable") from None
        if order_type.id != order_type_id or order_type.isDeleted or order_type.isHidden:
            raise CloverUnavailableError("Clover order type is unavailable")
        return order_type

    async def get_order_types(self) -> list[CloverOrderTypeListing]:
        """List merchant order types using the configured credential and merchant."""
        token = await self._access_token()
        path = f"/v3/merchants/{self._merchant_id}/order_types"
        results: list[CloverOrderTypeListing] = []
        page_size = 1000
        for offset in range(0, 10000, page_size):
            try:
                response = await self._client.get(
                    f"{self._base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "RestaurantPlatform/0.1 (development)",
                    },
                    params={"limit": page_size, "offset": offset},
                    timeout=httpx.Timeout(10.0, connect=5.0),
                    follow_redirects=False,
                )
                if response.status_code != 200:
                    response.raise_for_status()
                    raise ValueError("Unexpected order type status")
                collection = CloverOrderTypesCollection.model_validate(response.json())
            except httpx.HTTPStatusError as exc:
                await self._handle_read_status(exc.response.status_code)
                raise AssertionError("unreachable")
            except (httpx.HTTPError, ValueError, ValidationError):
                raise CloverUnavailableError("Clover order types are unavailable") from None
            results.extend(collection.elements)
            if len(collection.elements) < page_size:
                logger.info("Received %d Clover order types", len(results))
                return results
        raise CloverUnavailableError("Clover order types exceed the supported limit")

    async def append_inventory_line_items(
        self, *, order_id: str, lines: list[tuple[str, int]]
    ) -> None:
        """Append inventory-backed lines; one HTTP mutation, never retried."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", order_id) or not 1 <= len(lines) <= 100:
            raise ValueError("Invalid existing order append")
        if any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item_id)
            or isinstance(price, bool) or not isinstance(price, int) or price < 0
            for item_id, price in lines
        ):
            raise ValueError("Invalid existing order line")
        token = await self._access_token()
        base_path = f"/v3/merchants/{self._merchant_id}/orders/{order_id}"
        if len(lines) == 1:
            path = f"{base_path}/line_items"
            body = {"item": {"id": lines[0][0]}}
        else:
            path = f"{base_path}/bulk_line_items"
            body = {"items": [
                {"item": {"id": item_id}, "price": price}
                for item_id, price in lines
            ]}
        try:
            response = await self._client.post(
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "RestaurantPlatform/0.1 (development)",
                },
                json=body,
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise CloverOrderUncertainError("Clover append outcome is unknown")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            logger.warning("Clover line-item append HTTP failure: %d", code)
            if code in (401, 403):
                if self._token_provider is not None:
                    await self._token_provider.invalidate()
                    raise CloverReconnectionRequiredError("Clover reconnection required") from None
                raise CloverAuthenticationError("Clover credentials were rejected") from None
            if len(lines) == 1 and code in (400, 404, 422):
                raise CloverOrderRejectedError("Clover rejected the line items") from None
            raise CloverOrderUncertainError("Clover append outcome is unknown") from None
        except httpx.HTTPError:
            raise CloverOrderUncertainError("Clover append outcome is unknown") from None

    async def append_modified_line_items(
        self, *, order_id: str, lines: list[tuple[str, list[str]]],
    ) -> None:
        """Append each inventory line, then apply its modifiers by returned line ID.

        Any failure after the first POST is uncertain; the caller's durable
        reconciliation gate prevents replaying a partially applied batch.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", order_id) or not 1 <= len(lines) <= 100 or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item_id)
            or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", modifier_id) for modifier_id in modifiers)
            for item_id, modifiers in lines
        ):
            raise ValueError("Invalid modified append")
        token = await self._access_token()
        base = f"{self._base_url}/v3/merchants/{self._merchant_id}/orders/{order_id}/line_items"
        headers = {"Authorization": f"Bearer {token}", "User-Agent": "RestaurantPlatform/0.1 (development)"}
        try:
            for item_id, modifiers in lines:
                response = await self._client.post(
                    base, headers=headers, json={"item": {"id": item_id}},
                    timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False,
                )
                if not 200 <= response.status_code < 300:
                    raise CloverOrderUncertainError("Clover modified append needs reconciliation")
                payload = response.json()
                line_id = payload.get("id") if isinstance(payload, dict) else None
                if not isinstance(line_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", line_id):
                    raise CloverOrderUncertainError("Clover modified append needs reconciliation")
                for modifier_id in modifiers:
                    response = await self._client.post(
                        f"{base}/{line_id}/modifications", headers=headers,
                        json={"modifier": {"id": modifier_id}},
                        timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False,
                    )
                    if not 200 <= response.status_code < 300:
                        raise CloverOrderUncertainError("Clover modified append needs reconciliation")
                verification = await self._client.get(
                    f"{base}/{line_id}", headers=headers,
                    params={"expand": "modifications"},
                    timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False,
                )
                if verification.status_code != 200:
                    raise CloverOrderUncertainError("Clover modified append needs reconciliation")
                readback = verification.json()
                found = readback.get("modifications") if isinstance(readback, dict) else None
                elements = found.get("elements") if isinstance(found, dict) else None
                actual = [element.get("modifier", {}).get("id") for element in elements] if isinstance(elements, list) and all(isinstance(element, dict) and isinstance(element.get("modifier"), dict) for element in elements) else None
                if (not isinstance(readback, dict) or readback.get("id") != line_id
                    or not isinstance(readback.get("item"), dict)
                    or readback["item"].get("id") != item_id
                    or actual is None or sorted(actual) != sorted(modifiers)):
                    raise CloverOrderUncertainError("Clover modified append needs reconciliation")
        except (httpx.HTTPError, ValueError):
            raise CloverOrderUncertainError("Clover modified append needs reconciliation") from None

    async def get_order_readback(
        self, order_id: str, *, include_modifications: bool = False,
        allow_missing_modifications: bool = False,
    ) -> CloverOrderReadback:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", order_id):
            raise CloverUnavailableError("Clover order is unavailable")
        token = await self._access_token()
        path = f"/v3/merchants/{self._merchant_id}/orders/{order_id}"
        stage = "order"
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "RestaurantPlatform/0.1 (development)",
                },
                params={"expand": "lineItems"},
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
            )
            if response.status_code != 200:
                if response.status_code in (401, 403):
                    await self._handle_read_status(response.status_code)
                raise CloverReadbackFailure("order_http_status", response.status_code)
            order = CloverOrderReadback.model_validate(response.json())
            if include_modifications:
                expanded_lines: list[dict[str, object]] = []
                for offset in range(0, 3000, 100):
                    stage = "line_items"
                    lines_response = await self._client.get(
                        f"{self._base_url}{path}/line_items",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"expand": "modifications", "limit": 100, "offset": offset},
                        timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False,
                    )
                    if lines_response.status_code != 200:
                        if lines_response.status_code in (401, 403):
                            await self._handle_read_status(lines_response.status_code)
                        raise CloverReadbackFailure(
                            "line_items_http_status", lines_response.status_code,
                        )
                    collection = lines_response.json()
                    rows = collection.get("elements") if isinstance(collection, dict) else None
                    if not isinstance(rows, list) or len(rows) > 100:
                        raise CloverReadbackFailure("line_items_schema")
                    for row in rows:
                        if not isinstance(row, dict):
                            raise CloverReadbackFailure("line_items_schema")
                        modifications = row.get("modifications")
                        if modifications is None and allow_missing_modifications:
                            row = {**row, "modifications": {"elements": []}}
                            modifications = row["modifications"]
                        elif modifications is None:
                            raise CloverReadbackFailure("missing_modifications_wrapper")
                        if (not isinstance(modifications, dict) or
                                not isinstance(modifications.get("elements"), list)):
                            raise CloverReadbackFailure("modifications_schema")
                        expanded_lines.append(row)
                    if len(rows) < 100:
                        break
                order = order.model_copy(update={"lineItems": CloverReadLineItems.model_validate({"elements": expanded_lines})})
        except httpx.HTTPError:
            raise CloverReadbackFailure(f"{stage}_transport") from None
        except (ValueError, ValidationError):
            raise CloverReadbackFailure(f"{stage}_schema") from None
        if order.id != order_id:
            raise CloverReadbackFailure("order_id_mismatch")
        return order

    async def get_order_calculation_snapshot(
        self, order_id: str
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Read order charges and every line with its calculation expansions."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", order_id):
            raise CloverUnavailableError("Clover order is unavailable")
        token = await self._access_token()
        path = f"/v3/merchants/{self._merchant_id}/orders/{order_id}"
        headers = {"Authorization": f"Bearer {token}"}

        async def get(path: str, params: list[tuple[str, str | int]]) -> dict:
            try:
                response = await self._client.get(
                    f"{self._base_url}{path}", headers=headers, params=params,
                    timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False,
                )
                if response.status_code != 200:
                    response.raise_for_status()
                    raise ValueError("Unexpected order status")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Invalid order payload")
                return payload
            except httpx.HTTPStatusError as exc:
                await self._handle_read_status(exc.response.status_code)
                raise AssertionError("unreachable")
            except (httpx.HTTPError, ValueError):
                raise CloverUnavailableError("Clover order is unavailable") from None

        order = await get(path, [("expand", "discounts"), ("expand", "serviceCharge")])
        if order.get("id") != order_id:
            raise CloverUnavailableError("Clover order is unavailable")
        lines: list[dict[str, object]] = []
        page_size = 100
        for offset in range(0, 3000, page_size):
            page = await get(f"{path}/line_items", [
                ("expand", "taxRates"), ("expand", "modifications"),
                ("expand", "discounts"), ("limit", page_size), ("offset", offset),
            ])
            elements = page.get("elements")
            if not isinstance(elements, list) or any(
                not isinstance(element, dict) for element in elements
            ) or len(elements) > page_size:
                raise CloverUnavailableError("Clover line items are unavailable")
            lines.extend(elements)
            if len(elements) < page_size:
                return order, lines
        raise CloverUnavailableError("Clover line items exceed supported limit")

    async def update_order_total(
        self, order_id: str, total_cents: int, *, diagnostic: bool = False,
    ) -> None:
        """One non-retried total-only update on the trusted merchant order."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", order_id):
            raise ValueError("Invalid order ID")
        if isinstance(total_cents, bool) or not isinstance(total_cents, int) or total_cents < 0:
            raise ValueError("Invalid order total")
        token = await self._access_token()
        path = f"/v3/merchants/{self._merchant_id}/orders/{order_id}"
        if diagnostic:
            logger.warning(
                "DEV Clover total update sending order_suffix=%s calculated_total_cents=%d",
                order_id[-4:] if len(order_id) > 4 else "****", total_cents,
            )
        try:
            response = await self._client.post(
                f"{self._base_url}{path}", headers={"Authorization": f"Bearer {token}"},
                json={"total": total_cents}, timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=False,
            )
            if diagnostic:
                response_total = None
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        value = payload.get("total")
                        if isinstance(value, int) and not isinstance(value, bool):
                            response_total = value
                except ValueError:
                    pass
                logger.warning(
                    "DEV Clover total update result order_suffix=%s http_status=%d "
                    "response_total_cents=%s",
                    order_id[-4:] if len(order_id) > 4 else "****",
                    response.status_code, response_total,
                )
            if 300 <= response.status_code < 400:
                raise CloverOrderUncertainError("Clover total update outcome is unknown")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("Clover total update HTTP failure: %d", exc.response.status_code)
            if exc.response.status_code in (401, 403):
                if self._token_provider is not None:
                    await self._token_provider.invalidate()
                    raise CloverReconnectionRequiredError("Clover reconnection required") from None
                raise CloverAuthenticationError("Clover credentials were rejected") from None
            if exc.response.status_code in (400, 404, 422):
                raise CloverOrderRejectedError("Clover rejected the total update") from None
            raise CloverOrderUncertainError("Clover total update outcome is unknown") from None
        except httpx.HTTPError:
            raise CloverOrderUncertainError("Clover total update outcome is unknown") from None

    async def create_atomic_order(
        self, *, item_ids: list[str], order_type_id: str,
        line_modifiers: list[list[str]] | None = None,
    ) -> dict[str, object]:
        """One non-retried POST. Each repeated line represents one fixed-price unit."""
        token = await self._access_token()
        path = f"/v3/merchants/{self._merchant_id}/atomic_order/orders"
        if line_modifiers is not None and len(line_modifiers) != len(item_ids):
            raise ValueError("Invalid atomic modifier selection")
        if line_modifiers is not None and any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", modifier_id)
            for modifiers in line_modifiers for modifier_id in modifiers
        ):
            raise ValueError("Invalid atomic modifier ID")
        body = {
            "orderCart": {
                "lineItems": [
                    {"item": {"id": item_id}, **(
                        {"modifications": [{"modifier": {"id": modifier_id}} for modifier_id in line_modifiers[index]]}
                        if line_modifiers and line_modifiers[index] else {}
                    )}
                    for index, item_id in enumerate(item_ids)
                ],
                "orderType": {"id": order_type_id},
            }
        }
        try:
            response = await self._client.post(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise CloverOrderUncertainError("Clover order outcome is unknown")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid atomic order response")
            return payload
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            logger.warning("Clover atomic order HTTP failure: %d", code)
            if code in (401, 403):
                if self._token_provider is not None:
                    await self._token_provider.invalidate()
                    raise CloverReconnectionRequiredError("Clover reconnection required") from None
                raise CloverAuthenticationError("Clover credentials were rejected") from None
            if code in (400, 404, 422):
                raise CloverOrderRejectedError("Clover rejected the order") from None
            raise CloverOrderUncertainError("Clover order outcome is unknown") from None
        except (httpx.HTTPError, ValueError):
            raise CloverOrderUncertainError("Clover order outcome is unknown") from None

    async def _access_token(self) -> str:
        token = (
            await self._token_provider.get_access_token()
            if self._token_provider is not None
            else self._api_token
        )
        if not token:
            raise CloverAuthenticationError("Clover credentials are missing")
        return token

    async def _handle_read_status(self, code: int) -> None:
        logger.warning("Clover read HTTP failure: %d", code)
        if code in (401, 403):
            if self._token_provider is not None:
                await self._token_provider.invalidate()
                raise CloverReconnectionRequiredError("Clover reconnection required") from None
            raise CloverAuthenticationError("Clover credentials were rejected") from None
        raise CloverUnavailableError("Clover order type is unavailable")

