"""Validated order orchestration; no client-supplied prices or totals."""

import asyncio
from collections import Counter
import hashlib
import json
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import SANDBOX_API_URL, Settings
from app.integrations.clover.client import CloverClient
from app.integrations.clover.models import CloverInventoryItem
from app.integrations.clover.exceptions import (
    CloverAuthenticationError,
    CloverOrderRejectedError,
    CloverReconnectionRequiredError,
    CloverUnavailableError,
)
from app.orders.idempotency import SQLiteOrderLedger
from app.orders.models import OrderLineResponse, OrderRequest, OrderResponse, cents_to_major
from app.orders.models import OrderModifierResponse


class AtomicOrderResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    currency: str | None = None
    subtotal: int | None = Field(default=None, ge=0)
    totalTaxAmount: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)


def verify_atomic_modifiers(payload: dict[str, object],
                            expected: list[tuple[str, list[str]]]) -> None:
    """Require Clover's created cart to confirm every selected modifier ID."""
    cart = payload.get("orderCart")
    lines = cart.get("lineItems") if isinstance(cart, dict) else None
    if not isinstance(lines, list):
        raise ValueError("Atomic modifier readback is unavailable")
    actual: list[tuple[str, tuple[str, ...]]] = []
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("item"), dict):
            raise ValueError("Atomic modifier readback is invalid")
        item_id = line["item"].get("id")
        mods = line.get("modifications", [])
        if not isinstance(item_id, str) or not isinstance(mods, list):
            raise ValueError("Atomic modifier readback is invalid")
        ids: list[str] = []
        for mod in mods:
            reference = mod.get("modifier") if isinstance(mod, dict) else None
            modifier_id = reference.get("id") if isinstance(reference, dict) else None
            if not isinstance(modifier_id, str):
                raise ValueError("Atomic modifier readback is invalid")
            ids.append(modifier_id)
        actual.append((item_id, tuple(sorted(ids))))
    if Counter(actual) != Counter((item_id, tuple(sorted(ids))) for item_id, ids in expected):
        raise ValueError("Atomic modifier readback does not match selection")


async def validate_order_inventory(
    client: CloverClient, request: OrderRequest
) -> dict[str, CloverInventoryItem]:
    """Read current Clover inventory and reject unsafe/unavailable selections."""
    requested_ids = list(dict.fromkeys(line.item_id for line in request.items))
    inventory = await client.get_inventory_items(
        limit=len(requested_ids), item_ids=requested_ids
    )
    by_id = {item.id: item for item in inventory.elements}
    if len(by_id) != len(requested_ids) or any(
        line.item_id not in by_id
        or by_id[line.item_id].available is False
        or by_id[line.item_id].hidden is True
        or by_id[line.item_id].deletedTime is not None
        or by_id[line.item_id].isAgeRestricted is True
        or by_id[line.item_id].priceType not in (None, "FIXED")
        for line in request.items
    ):
        raise HTTPException(400, "One or more items are unavailable")
    return by_id


async def validate_order_modifiers(
    client: CloverClient, request: OrderRequest,
    by_id: dict[str, CloverInventoryItem],
) -> dict[tuple[str, tuple[str, ...]], list[OrderModifierResponse]]:
    """Validate selected IDs and all required groups against fresh Clover relationships."""
    references: dict[str, list[str]] = {}
    for line in request.items:
        value = by_id[line.item_id].modifierGroups
        if not isinstance(value, dict) or not isinstance(value.get("elements"), list):
            raise CloverUnavailableError("Clover modifier relationships are unavailable")
        elements = value["elements"]
        if len(elements) >= 100 or any(
            not isinstance(row, dict) or not isinstance(row.get("id"), str)
            for row in elements
        ):
            raise CloverUnavailableError("Clover modifier relationships are unavailable")
        references[line.item_id] = [row["id"] for row in elements]
    group_ids = {group_id for values in references.values() for group_id in values}
    if not group_ids and any(line.modifier_ids for line in request.items):
        raise HTTPException(400, "Invalid modifier selection")
    if not group_ids:
        return {(line.item_id, ()): [] for line in request.items}
    groups: dict[str, tuple[int, int, dict[str, OrderModifierResponse]]] = {}
    for offset in range(0, 10000, 100):
        page = await client.inventory_collection("modifier_groups", limit=100, offset=offset)
        rows = page.get("elements")
        if not isinstance(rows, list) or len(rows) > 100:
            raise CloverUnavailableError("Clover modifier groups are unavailable")
        for group in rows:
            if not isinstance(group, dict) or not isinstance(group.get("id"), str):
                raise CloverUnavailableError("Clover modifier groups are unavailable")
            group_id = group["id"]
            if group_id not in group_ids:
                continue
            if group_id in groups:
                raise CloverUnavailableError("Clover modifier groups are ambiguous")
            minimum, maximum = group.get("minRequired"), group.get("maxAllowed")
            if type(minimum) is not int or type(maximum) is not int or minimum < 0 or maximum < max(1, minimum):
                raise CloverUnavailableError("Clover modifier rules are unavailable")
            groups[group_id] = (minimum, maximum, {})
        if len(rows) < 100:
            break
    if set(groups) != group_ids:
        raise CloverUnavailableError("Clover modifier groups are unavailable")
    for group_id, (_, _, options) in groups.items():
        for offset in range(0, 10000, 100):
            page = await client.inventory_collection("modifiers", parent_id=group_id, limit=100, offset=offset)
            rows = page.get("elements")
            if not isinstance(rows, list) or len(rows) > 100:
                raise CloverUnavailableError("Clover modifiers are unavailable")
            for option in rows:
                if not isinstance(option, dict) or not isinstance(option.get("id"), str) or not isinstance(option.get("name"), str) or type(option.get("price")) is not int:
                    raise CloverUnavailableError("Clover modifier schema is unavailable")
                if option["price"] < 0 or option["id"] in options:
                    raise CloverUnavailableError("Clover modifier schema is unavailable")
                if option.get("available") is not False and option.get("deletedTime") is None:
                    options[option["id"]] = OrderModifierResponse(
                        id=option["id"], name=option["name"], price_delta=cents_to_major(option["price"]),
                    )
            if len(rows) < 100:
                break
    selected: dict[tuple[str, tuple[str, ...]], list[OrderModifierResponse]] = {}
    for line in request.items:
        wanted = set(line.modifier_ids)
        chosen: list[OrderModifierResponse] = []
        for group_id in references[line.item_id]:
            minimum, maximum, options = groups[group_id]
            group_chosen = wanted & options.keys()
            if not minimum <= len(group_chosen) <= maximum:
                raise HTTPException(400, "Invalid modifier selection")
            chosen.extend(options[option_id] for option_id in line.modifier_ids if option_id in group_chosen)
            wanted -= group_chosen
        if wanted:
            raise HTTPException(400, "Invalid modifier selection")
        selected[(line.item_id, tuple(sorted(line.modifier_ids)))] = chosen
    return selected


class OrderService:
    def __init__(self, ledger: SQLiteOrderLedger) -> None:
        self.ledger = ledger
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def _key_lock(self, key: str) -> AsyncIterator[None]:
        async with self._locks_guard:
            lock, count = self._locks.get(key, (asyncio.Lock(), 0))
            self._locks[key] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            async with self._locks_guard:
                _, count = self._locks[key]
                if count == 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (lock, count - 1)

    async def create(
        self, *, request: OrderRequest, key: str, client: CloverClient, settings: Settings
    ) -> OrderResponse:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", key):
            raise HTTPException(400, "Invalid Idempotency-Key")
        if settings.app_env != "development" or settings.clover_env != "sandbox":
            raise HTTPException(503, "Clover orders require development Sandbox mode")
        if str(settings.clover_base_url).rstrip("/") != SANDBOX_API_URL:
            raise HTTPException(503, "Clover orders require the Sandbox API")
        order_type_id = settings.clover_order_type_id
        if not order_type_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", order_type_id):
            raise HTTPException(503, "Clover order type is not configured")
        merchant_id = client.merchant_id
        scope = (
            f"{settings.clover_env}:{settings.clover_auth_mode}:"
            f"{settings.clover_app_id or ''}:{merchant_id}"
        )
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(
                {"order": request.model_dump(), "order_type_id": order_type_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        lock_key = f"{scope}:{key_hash}"
        async with self._key_lock(lock_key):
            try:
                state, cached = await self.ledger.claim(scope, key_hash, fingerprint)
            except Exception:
                raise HTTPException(503, "Order storage is unavailable") from None
            if state == "conflict":
                raise HTTPException(409, "Idempotency-Key was used for a different order")
            if state == "succeeded" and cached is not None:
                return OrderResponse.model_validate(cached)
            if state in ("pending", "uncertain"):
                raise HTTPException(409, "Order outcome needs reconciliation before retry")
            if state != "claimed":
                raise HTTPException(503, "Order storage is unavailable")

            try:
                by_id = await validate_order_inventory(client, request)
                selected_modifiers = await validate_order_modifiers(client, request, by_id)
                await client.get_order_type(order_type_id)
            except HTTPException:
                await self._release(scope, key_hash, fingerprint)
                raise
            except CloverReconnectionRequiredError:
                await self._release(scope, key_hash, fingerprint)
                raise HTTPException(503, "Clover reconnection required") from None
            except CloverUnavailableError:
                await self._release(scope, key_hash, fingerprint)
                raise HTTPException(502, "Clover is temporarily unavailable") from None
            except Exception:
                await self._release(scope, key_hash, fingerprint)
                raise HTTPException(503, "Order preparation failed") from None

            item_ids = [line.item_id for line in request.items for _ in range(line.quantity)]
            line_modifiers = [line.modifier_ids for line in request.items for _ in range(line.quantity)]
            try:
                payload = await client.create_atomic_order(
                    item_ids=item_ids, order_type_id=order_type_id,
                    **({"line_modifiers": line_modifiers} if any(line_modifiers) else {}),
                )
            except CloverReconnectionRequiredError:
                await self._release(scope, key_hash, fingerprint)
                raise HTTPException(503, "Clover reconnection required") from None
            except CloverAuthenticationError:
                await self._release(scope, key_hash, fingerprint)
                raise HTTPException(502, "Clover rejected the credential") from None
            except CloverOrderRejectedError:
                await self._release(scope, key_hash, fingerprint)
                raise HTTPException(502, "Clover rejected the order") from None
            except Exception:
                await self._uncertain(scope, key_hash, fingerprint)
                raise HTTPException(502, "Order outcome is unknown; reconciliation required") from None

            try:
                if any(line_modifiers):
                    verify_atomic_modifiers(payload, list(zip(item_ids, line_modifiers)))
                result = AtomicOrderResult.model_validate(payload)
                normalized = OrderResponse(
                    id=result.id,
                    table=request.table,
                    items=[
                        OrderLineResponse(
                            item_id=line.item_id,
                            name=by_id[line.item_id].name,
                            quantity=line.quantity,
                            unit_price=cents_to_major(by_id[line.item_id].price),
                            modifiers=selected_modifiers[(line.item_id, tuple(sorted(line.modifier_ids)))],
                        )
                        for line in request.items
                    ],
                    currency=result.currency,
                    subtotal=cents_to_major(result.subtotal),
                    tax=cents_to_major(result.totalTaxAmount),
                    total=cents_to_major(result.total),
                )
                await self.ledger.finish(
                    scope, key_hash, fingerprint, "succeeded", normalized.model_dump(mode="json")
                )
            except Exception:
                await self._uncertain(scope, key_hash, fingerprint)
                raise HTTPException(502, "Order outcome is unknown; reconciliation required") from None
            return normalized

    async def _release(self, scope: str, key_hash: str, fingerprint: str) -> None:
        try:
            await self.ledger.release(scope, key_hash, fingerprint)
        except Exception:
            raise HTTPException(503, "Order storage is unavailable") from None

    async def _uncertain(self, scope: str, key_hash: str, fingerprint: str) -> None:
        try:
            await self.ledger.finish(scope, key_hash, fingerprint, "uncertain")
        except Exception:
            raise HTTPException(503, "Order storage is unavailable; do not retry") from None
