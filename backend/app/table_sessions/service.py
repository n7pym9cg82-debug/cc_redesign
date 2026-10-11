"""Create/resume table checks and append to their single Clover order."""

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from contextlib import asynccontextmanager
from itertools import permutations
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import HTTPException

from app.core.config import SANDBOX_API_URL, Settings
from app.integrations.clover.client import CloverClient
from app.integrations.clover.exceptions import (
    CloverAuthenticationError,
    CloverOrderRejectedError,
    CloverReadbackFailure,
    CloverReconnectionRequiredError,
    CloverUnavailableError,
)
from app.integrations.clover.models import CloverOrderReadback
from app.orders.models import OrderRequest, OrderModifierResponse, cents_to_major
from app.orders.service import OrderService, validate_order_inventory, validate_order_modifiers
from app.orders.total_calculator import (
    UnsupportedOrderCalculationError, calculate_order_total,
)
from app.table_sessions.models import (
    AddSessionItemsRequest,
    SessionItemsResponse,
    SessionLineResponse,
    SessionOrderResponse,
    TableSessionResponse,
)
from app.table_sessions.store import TableSession, TableSessionRepository, UnsafeSessionClosure

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.api.menu import MenuCache


class OrderConfirmationPending(Exception):
    """Clover mutation is confirmed; retry this exact key for GET-only completion."""


def normalize_readback(
    order: CloverOrderReadback, item_names: dict[str, str] | None = None,
) -> SessionOrderResponse:
    """Aggregate identical fixed-price inventory lines without deriving totals."""
    lines: list[SessionLineResponse] = []
    positions: dict[tuple, int] = {}
    item_names = item_names or {}
    direct_names: dict[str, str] = {}
    for line in order.lineItems.elements:
        if line.item is not None:
            direct = next((value.strip() for value in (
                line.name, line.item.name,
            ) if isinstance(value, str) and value.strip()), None)
            if direct is not None:
                direct_names.setdefault(line.item.id, direct)
    for line in order.lineItems.elements:
        item_id = line.item.id if line.item else None
        name = next((value.strip() for value in (
            line.name, line.item.name if line.item else None,
            direct_names.get(item_id) if item_id else None,
            item_names.get(item_id) if item_id else None,
        ) if isinstance(value, str) and value.strip()), "Item")
        modifiers: list[OrderModifierResponse] = []
        if line.modifications is not None:
            for modification in line.modifications.elements:
                if (modification.modifier is None or modification.name is None
                    or modification.amount is None):
                    raise ValueError("Invalid Clover modification readback")
                modifiers.append(OrderModifierResponse(
                    id=modification.modifier.id, name=modification.name,
                    price_delta=cents_to_major(modification.amount),
                ))
        modifier_identity = tuple(sorted((entry.id, entry.price_delta) for entry in modifiers))
        key = (
            (item_id, name, line.price, modifier_identity)
            if item_id and line.price is not None
            and line.unitQty in (None, 1)
            else None
        )
        if key is not None and key in positions:
            current = lines[positions[key]]
            lines[positions[key]] = current.model_copy(update={"quantity": current.quantity + 1})
        else:
            if key is not None:
                positions[key] = len(lines)
            lines.append(SessionLineResponse(
                item_id=item_id, name=name, quantity=1,
                unit_price=cents_to_major(line.price), modifiers=modifiers,
            ))
    return SessionOrderResponse(
        id=order.id, currency=order.currency,
        subtotal=cents_to_major(order.subtotal),
        tax=cents_to_major(order.totalTaxAmount),
        total=cents_to_major(order.total), items=lines,
    )


class TableSessionService:
    def __init__(
        self, repository: TableSessionRepository, order_service: OrderService,
        menu_cache: "MenuCache | None" = None,
    ) -> None:
        self.repository = repository
        self.order_service = order_service
        self.menu_cache = menu_cache
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._guard = asyncio.Lock()

    @staticmethod
    def scope(settings: Settings) -> str:
        if (
            settings.app_env != "development"
            or settings.clover_env != "sandbox"
            or str(settings.clover_base_url).rstrip("/") != SANDBOX_API_URL
            or not settings.clover_merchant_id
            or not re.fullmatch(r"[A-Za-z0-9_-]+", settings.clover_merchant_id)
        ):
            raise HTTPException(503, "Table sessions require configured Sandbox Clover")
        return f"{settings.clover_env}:{settings.clover_merchant_id}"

    async def _readback_item_names(
        self, order: CloverOrderReadback, client: CloverClient, settings: Settings,
    ) -> dict[str, str]:
        """Resolve missing names by ID without one inventory request per line."""
        names = (self.menu_cache.cached_item_names(client, settings)
                 if self.menu_cache is not None else {})
        directly_named_ids = {
            line.item.id for line in order.lineItems.elements
            if line.item is not None and (
                (line.name and line.name.strip()) or
                (line.item.name and line.item.name.strip())
            )
        }
        missing = sorted({
            line.item.id for line in order.lineItems.elements
            if line.item is not None and
            not (line.name and line.name.strip()) and
            not (line.item.name and line.item.name.strip()) and
            re.fullmatch(r"[A-Za-z0-9_-]{1,64}", line.item.id) is not None and
            line.item.id not in directly_named_ids and
            line.item.id not in names
        })
        for offset in range(0, len(missing), 100):
            batch = missing[offset:offset + 100]
            try:
                inventory = await client.get_inventory_items(limit=len(batch), item_ids=batch)
            except Exception:
                # A name lookup must not turn a confirmed order into a pending mutation.
                break
            names.update({
                item.id: item.name for item in inventory.elements
                if item.id in batch and item.name.strip()
            })
        return names

    @asynccontextmanager
    async def _session_lock(self, session_id: str) -> AsyncIterator[None]:
        async with self._guard:
            lock, count = self._locks.get(session_id, (asyncio.Lock(), 0))
            self._locks[session_id] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            async with self._guard:
                _, count = self._locks[session_id]
                if count == 1:
                    del self._locks[session_id]
                else:
                    self._locks[session_id] = (lock, count - 1)

    async def create_or_resume(self, settings: Settings, table: str) -> TableSessionResponse:
        scope = self.scope(settings)
        try:
            session = await self.repository.get_or_create(scope, table)
        except Exception:
            raise HTTPException(503, "Table-session storage is unavailable") from None
        return TableSessionResponse.from_record(session)

    async def get(self, settings: Settings, session_id: str) -> TableSessionResponse:
        scope = self.scope(settings)
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise HTTPException(404, "Table session not found")
        try:
            session = await self.repository.get(scope, session_id)
        except Exception:
            raise HTTPException(503, "Table-session storage is unavailable") from None
        if session is None:
            raise HTTPException(404, "Table session not found")
        return TableSessionResponse.from_record(session)

    async def close(self, settings: Settings, session_id: str) -> TableSessionResponse:
        """Close the local visit only after all mutations are settled."""
        scope = self.scope(settings)
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise HTTPException(404, "Table session not found")
        try:
            session = await self.repository.close(scope, session_id)
        except UnsafeSessionClosure as exc:
            detail = ("Table has undelivered kitchen items" if "undelivered" in str(exc)
                      else "Clover payment is not confirmed" if "payment" in str(exc)
                      else "Table session has an unresolved operation")
            raise HTTPException(409, detail) from None
        except Exception:
            raise HTTPException(503, "Table-session storage is unavailable") from None
        if session is None:
            raise HTTPException(404, "Table session not found")
        return TableSessionResponse.from_record(session)

    async def add_items(
        self, *, settings: Settings, session_id: str, body: AddSessionItemsRequest,
        key: str, client: CloverClient,
    ) -> SessionItemsResponse:
        scope = self.scope(settings)
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise HTTPException(404, "Table session not found")
        if client.merchant_id != settings.clover_merchant_id:
            raise HTTPException(503, "Clover merchant configuration is inconsistent")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", key):
            raise HTTPException(400, "Invalid Idempotency-Key")
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps(
            body.model_dump(), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        async with self._session_lock(session_id):
            try:
                state, session, cached = await self.repository.claim(
                    scope, session_id, key_hash, fingerprint
                )
            except Exception:
                raise HTTPException(503, "Table-session storage is unavailable") from None
            if state == "busy":
                while True:
                    try:
                        prior = await self.repository.confirmed_unresolved(scope, session_id)
                    except Exception:
                        raise HTTPException(503, "Table-session storage is unavailable") from None
                    if session is None:
                        break
                    if prior is not None:
                        prior_key_hash, metadata = prior
                        prior_phase = (metadata.get("resume_phase") if
                                       metadata.get("phase") == "readback_pending" else
                                       metadata.get("phase"))
                        try:
                            if prior_phase == "append_applied":
                                await self._finish_applied_append(
                                    scope=scope, session_id=session_id,
                                    key_hash=prior_key_hash, order_id=session.clover_order_id,
                                    client=client, settings=settings,
                                )
                            else:
                                await self._complete_confirmed_readback(
                                    scope=scope, session_id=session_id, key_hash=prior_key_hash,
                                    order_id=session.clover_order_id, phase=prior_phase,
                                    metadata=metadata, client=client, settings=settings,
                                )
                        except OrderConfirmationPending:
                            raise HTTPException(
                                503, "Previous order confirmation is pending; retry this order with the same key"
                            ) from None
                    else:
                        try:
                            candidate = await self.repository.uncertain_total_candidate(
                                scope, session_id,
                            )
                        except Exception:
                            raise HTTPException(503, "Table-session storage is unavailable") from None
                        if candidate is not None:
                            try:
                                recovered = await self._recover_uncertain_total(
                                    scope=scope, session=session, key_hash=candidate[0],
                                    target_total=candidate[1], client=client, settings=settings,
                                )
                            except OrderConfirmationPending:
                                raise HTTPException(
                                    503, "Previous order confirmation is pending; retry this order with the same key"
                                ) from None
                            if recovered is None:
                                break
                        else:
                            try:
                                legacy = await self.repository.legacy_uncertain_candidate(
                                    scope, session_id,
                                )
                            except Exception:
                                raise HTTPException(503, "Table-session storage is unavailable") from None
                            if legacy is None:
                                break
                            recovered_body = await self._infer_uncertain_append_body(
                                session=session, fingerprint=legacy[1],
                                prior_json=legacy[2], client=client,
                            )
                            if recovered_body is None:
                                break
                            try:
                                await self.repository.confirm_recovered_append(
                                    scope, session_id, legacy[0],
                                )
                            except Exception:
                                raise HTTPException(503, "Table-session storage is unavailable") from None
                            try:
                                await self._finish_applied_append(
                                    scope=scope, session_id=session_id,
                                    key_hash=legacy[0], order_id=session.clover_order_id,
                                    client=client, settings=settings,
                                )
                            except OrderConfirmationPending:
                                raise HTTPException(
                                    503, "Previous order confirmation is pending; retry this order with the same key"
                                ) from None
                    try:
                        state, session, cached = await self.repository.claim(
                            scope, session_id, key_hash, fingerprint
                        )
                    except Exception:
                        raise HTTPException(503, "Table-session storage is unavailable") from None
                    if state != "busy":
                        break
            if state == "missing":
                raise HTTPException(404, "Table session not found")
            if state == "closed":
                raise HTTPException(409, "Table session is closed")
            if state == "conflict":
                raise HTTPException(409, "Idempotency-Key was used for different items")
            if state == "succeeded" and cached is not None:
                try:
                    return SessionItemsResponse.model_validate(cached)
                except Exception:
                    raise HTTPException(503, "Table-session storage is unavailable") from None
            if state == "uncertain" and session is not None and session.clover_order_id:
                try:
                    candidate = await self.repository.uncertain_total_candidate(
                        scope, session_id,
                    )
                except Exception:
                    raise HTTPException(503, "Table-session storage is unavailable") from None
                if candidate is not None and candidate[0] == key_hash:
                    recovered = await self._recover_uncertain_total(
                        scope=scope, session=session, key_hash=key_hash,
                        target_total=candidate[1], client=client, settings=settings,
                    )
                    if recovered is not None:
                        return recovered
                if await self._recover_uncertain_append(
                    scope=scope, session=session, key_hash=key_hash,
                    body=body, client=client,
                ):
                    state, cached = "applied", {"phase": "append_applied"}
            if state in ("uncertain", "busy"):
                try:
                    if await self.repository.has_uncertain(scope, session_id):
                        raise HTTPException(
                            409, "Previous order outcome is unknown; manual reconciliation required"
                        )
                except HTTPException:
                    raise
                except Exception:
                    raise HTTPException(503, "Table-session storage is unavailable") from None
            if state in ("pending", "uncertain", "busy"):
                raise HTTPException(409, "Table session operation needs reconciliation")
            if state not in ("claimed", "applied", "reconciliation_required"):
                raise HTTPException(503, "Table-session storage is unavailable")

            if state == "claimed":
                if session.clover_order_id is None:
                    atomic_key = hashlib.sha256(
                        f"session-create:{session_id}:{key_hash}".encode()
                    ).hexdigest()
                    try:
                        created = await self.order_service.create(
                            request=OrderRequest(table=session.table, items=body.items),
                            key=atomic_key, client=client, settings=settings,
                        )
                    except HTTPException as exc:
                        if exc.status_code == 400 or exc.detail in (
                            "Clover rejected the order", "Clover rejected the credential",
                            "Clover reconnection required", "Clover is temporarily unavailable",
                            "Clover order type is not configured",
                        ):
                            await self._release(scope, session_id, key_hash)
                        else:
                            await self._uncertain(scope, session_id, key_hash)
                        raise
                    order_id = created.id
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", order_id):
                        await self._uncertain(scope, session_id, key_hash)
                        raise HTTPException(502, "Order outcome needs reconciliation")
                else:
                    order_id = session.clover_order_id
                    request = OrderRequest(table=session.table, items=body.items)
                    try:
                        inventory = await validate_order_inventory(client, request)
                        await validate_order_modifiers(client, request, inventory)
                    except HTTPException:
                        await self._release(scope, session_id, key_hash)
                        raise
                    except CloverReconnectionRequiredError:
                        await self._release(scope, session_id, key_hash)
                        raise HTTPException(503, "Clover reconnection required") from None
                    except CloverUnavailableError:
                        await self._release(scope, session_id, key_hash)
                        raise HTTPException(502, "Clover is temporarily unavailable") from None
                    except Exception:
                        await self._release(scope, session_id, key_hash)
                        raise HTTPException(502, "Order inventory is unavailable") from None
                    lines = [
                        (line.item_id, inventory[line.item_id].price)
                        for line in body.items for _ in range(line.quantity)
                    ]
                    try:
                        if any(line.modifier_ids for line in body.items):
                            modified_lines = [
                                (line.item_id, line.modifier_ids)
                                for line in body.items for _ in range(line.quantity)
                            ]
                            await client.append_modified_line_items(order_id=order_id, lines=modified_lines)
                        else:
                            await client.append_inventory_line_items(order_id=order_id, lines=lines)
                    except (CloverReconnectionRequiredError, CloverAuthenticationError):
                        await self._release(scope, session_id, key_hash)
                        raise HTTPException(503, "Clover reconnection required") from None
                    except CloverOrderRejectedError:
                        await self._release(scope, session_id, key_hash)
                        raise HTTPException(502, "Clover rejected the line items") from None
                    except Exception:
                        await self._uncertain(scope, session_id, key_hash)
                        raise HTTPException(
                            502, "Append outcome is unknown; reconciliation required"
                        ) from None
                try:
                    await self.repository.applied(
                        scope, session_id, key_hash, order_id,
                        has_modifiers=any(line.modifier_ids for line in body.items),
                        requested_items=[line.model_dump() for line in body.items],
                    )
                except Exception:
                    raise HTTPException(503, "Order outcome needs reconciliation; do not retry") from None
            else:
                order_id = session.clover_order_id
                if not order_id:
                    raise HTTPException(503, "Table-session storage is inconsistent")

            if state == "claimed":
                phase = "atomic_created" if not session.clover_order_id else "append_applied"
            else:
                phase = cached.get("phase") if isinstance(cached, dict) else None
                if state == "reconciliation_required":
                    phase = cached.get("resume_phase")
                    if phase not in ("atomic_created", "total_confirmed"):
                        raise HTTPException(503, "Table-session storage is inconsistent")

            if phase == "append_applied":
                return await self._finish_applied_append(
                    scope=scope, session_id=session_id, key_hash=key_hash,
                    order_id=order_id, client=client, settings=settings,
                    expected_body=body,
                )
            elif phase in ("total_pending", None):
                raise HTTPException(409, "Order total needs manual reconciliation")
            elif phase not in ("atomic_created", "total_confirmed"):
                raise HTTPException(503, "Table-session storage is inconsistent")

            return await self._complete_confirmed_readback(
                scope=scope, session_id=session_id, key_hash=key_hash,
                order_id=order_id, phase=phase, metadata=cached,
                client=client, settings=settings,
                has_modifiers=any(line.modifier_ids for line in body.items),
                expected_body=body,
            )

    async def _finish_applied_append(
        self, *, scope: str, session_id: str, key_hash: str, order_id: str,
        client: CloverClient, settings: Settings,
        expected_body: AddSessionItemsRequest | None = None,
    ) -> SessionItemsResponse:
        try:
            order_snapshot, line_snapshot = await client.get_order_calculation_snapshot(order_id)
        except CloverReconnectionRequiredError:
            raise HTTPException(
                503, "Clover reconnection required; retry total with the same key"
            ) from None
        except Exception:
            raise HTTPException(
                502, "Order updated but calculation readback unavailable; retry with the same key"
            ) from None
        try:
            target_total = calculate_order_total(order_snapshot, line_snapshot)
        except UnsupportedOrderCalculationError:
            await self._uncertain(scope, session_id, key_hash)
            raise HTTPException(409, "Order total needs manual reconciliation") from None
        if type(order_snapshot.get("total")) is int and order_snapshot["total"] == target_total:
            try:
                await self.repository.confirm_existing_total(
                    scope, session_id, key_hash, target_total,
                )
            except Exception:
                raise HTTPException(503, "Order outcome needs reconciliation; do not retry") from None
            return await self._complete_confirmed_readback(
                scope=scope, session_id=session_id, key_hash=key_hash,
                order_id=order_id, phase="total_confirmed",
                metadata={"target_total_cents": target_total},
                client=client, settings=settings,
                expected_body=expected_body,
            )
        try:
            await self.repository.prepare_total_update(
                scope, session_id, key_hash, target_total,
            )
        except Exception:
            raise HTTPException(503, "Order outcome needs reconciliation; do not retry") from None
        try:
            await client.update_order_total(
                order_id, target_total, diagnostic=settings.app_env == "development",
            )
        except Exception:
            await self._uncertain(scope, session_id, key_hash)
            raise HTTPException(502, "Order total update needs reconciliation") from None
        try:
            await self.repository.confirm_total_update(scope, session_id, key_hash)
        except Exception:
            raise HTTPException(503, "Order outcome needs reconciliation; do not retry") from None
        return await self._complete_confirmed_readback(
            scope=scope, session_id=session_id, key_hash=key_hash,
            order_id=order_id, phase="total_confirmed",
            metadata={"target_total_cents": target_total},
            client=client, settings=settings,
            expected_body=expected_body,
        )

    async def _recover_uncertain_append(
        self, *, scope: str, session: TableSession, key_hash: str,
        body: AddSessionItemsRequest, client: CloverClient,
    ) -> bool:
        """Prove a legacy uncertain append from the original fingerprinted request.

        An empty mutation metadata field proves the total-update phase was never
        entered. No line/modifier POST is repeated; GET must show the requested
        additions on top of the last successful order snapshot.
        """
        try:
            prior_json = await self.repository.prior_success_for_uncertain(
                scope, session.session_id, key_hash,
            )
            if prior_json is None:
                return False
            prior = SessionItemsResponse.model_validate(prior_json)
            if prior.order.id != session.clover_order_id:
                return False
            order = await client.get_order_readback(
                session.clover_order_id, include_modifications=True,
                allow_missing_modifications=True,
            )
            required: Counter[tuple[str, tuple[str, ...]]] = Counter()
            for line in prior.order.items:
                if line.item_id:
                    required[(line.item_id, tuple(sorted(m.id for m in line.modifiers)))] += line.quantity
            for line in body.items:
                required[(line.item_id, tuple(sorted(line.modifier_ids)))] += line.quantity
            actual: Counter[tuple[str, tuple[str, ...]]] = Counter()
            for line in order.lineItems.elements:
                if not line.item:
                    continue
                modifiers = line.modifications.elements if line.modifications else []
                if any(modifier.modifier is None for modifier in modifiers):
                    return False
                actual[(line.item.id, tuple(sorted(
                    modifier.modifier.id for modifier in modifiers if modifier.modifier
                )))] += 1
            if not all(actual[identity] >= count for identity, count in required.items()):
                return False
        except Exception:
            return False
        try:
            await self.repository.confirm_recovered_append(
                scope, session.session_id, key_hash,
            )
        except Exception:
            raise HTTPException(503, "Table-session storage is unavailable") from None
        return True

    async def _infer_uncertain_append_body(
        self, *, session: TableSession, fingerprint: str,
        prior_json: dict, client: CloverClient,
    ) -> AddSessionItemsRequest | None:
        """Use the saved request hash to prove an exact order-line delta by GET.

        This is only for legacy uncertain rows with no mutation metadata. The
        bounded candidate search fails closed if there are unrelated changes,
        unsupported lines, or too many identities to establish exact intent.
        """
        try:
            prior = SessionItemsResponse.model_validate(prior_json)
            if prior.order.id != session.clover_order_id:
                return None
            baseline: Counter[tuple[str, tuple[str, ...]]] = Counter()
            for line in prior.order.items:
                if not line.item_id:
                    return None
                baseline[(line.item_id, tuple(sorted(m.id for m in line.modifiers)))] += line.quantity
            order = await client.get_order_readback(
                session.clover_order_id, include_modifications=True,
                allow_missing_modifications=True,
            )
            actual: Counter[tuple[str, tuple[str, ...]]] = Counter()
            for line in order.lineItems.elements:
                if not line.item or line.unitQty not in (None, 1):
                    return None
                modifiers = line.modifications.elements if line.modifications else []
                if any(modifier.modifier is None for modifier in modifiers):
                    return None
                actual[(line.item.id, tuple(sorted(
                    modifier.modifier.id for modifier in modifiers if modifier.modifier
                )))] += 1
            if any(actual[identity] < count for identity, count in baseline.items()):
                return None
            delta = {identity: count - baseline[identity] for identity, count in actual.items()
                     if count > baseline[identity]}
            if not 1 <= len(delta) <= 6 or any(count > 10 for count in delta.values()):
                return None
            for ordering in permutations(delta):
                candidate = AddSessionItemsRequest.model_validate({"items": [
                    {"item_id": item_id, "quantity": delta[(item_id, modifier_ids)],
                     "modifier_ids": list(modifier_ids)}
                    for item_id, modifier_ids in ordering
                ]})
                candidate_hash = hashlib.sha256(json.dumps(
                    candidate.model_dump(), sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                if candidate_hash == fingerprint:
                    return candidate
        except Exception:
            return None
        return None

    async def _recover_uncertain_total(
        self, *, scope: str, session: TableSession, key_hash: str,
        target_total: int, client: CloverClient, settings: Settings,
    ) -> SessionItemsResponse | None:
        """A persisted Clover total equal to the prepared target proves the POST result."""
        try:
            order = await client.get_order_readback(session.clover_order_id)
            if order.total != target_total:
                return None
        except Exception:
            return None
        try:
            await self.repository.confirm_total_from_readback(
                scope, session.session_id, key_hash, target_total,
            )
        except Exception:
            raise HTTPException(503, "Table-session storage is unavailable") from None
        return await self._complete_confirmed_readback(
            scope=scope, session_id=session.session_id, key_hash=key_hash,
            order_id=session.clover_order_id, phase="total_confirmed",
            metadata={"target_total_cents": target_total},
            client=client, settings=settings,
        )

    async def _complete_confirmed_readback(
        self, *, scope: str, session_id: str, key_hash: str, order_id: str,
        phase: str, metadata: dict | None, client: CloverClient,
        settings: Settings, has_modifiers: bool = False,
        expected_body: AddSessionItemsRequest | None = None,
    ) -> SessionItemsResponse:
        expected_items: Counter[str] = Counter()
        expected_modifiers: Counter[str] = Counter()
        actual_items: Counter[str] = Counter()
        actual_modifiers: Counter[str] = Counter()
        actual_total: int | None = None
        expected_total = metadata.get("target_total_cents") if isinstance(metadata, dict) else None
        logged_failure = False

        def log_check(check: str) -> None:
            nonlocal logged_failure
            if check != "passed":
                logged_failure = True
            if settings.app_env != "development":
                return
            logger.warning(
                "DEV Clover reconciliation order_suffix=%s check=%s "
                "expected_item_counts=%s actual_item_counts=%s "
                "expected_modifier_counts=%s actual_modifier_counts=%s "
                "expected_total_cents=%s actual_total_cents=%s",
                order_id[-4:] if len(order_id) > 4 else "****", check,
                dict(sorted(expected_items.items())), dict(sorted(actual_items.items())),
                dict(sorted(expected_modifiers.items())), dict(sorted(actual_modifiers.items())),
                expected_total, actual_total,
            )

        try:
            # Existing checks may contain modifiers even when the new item is plain.
            # Plain first orders need no strict modifier expansion.
            include_modifications = phase != "atomic_created" or has_modifiers or bool(
                metadata and metadata.get("has_modifiers")
            )
            order = await client.get_order_readback(
                order_id, include_modifications=include_modifications,
                allow_missing_modifications=(
                    phase == "total_confirmed" and expected_body is not None
                ),
            )
            actual_total = order.total
            if phase == "total_confirmed" and expected_body is not None:
                prior_json = await self.repository.prior_success_for_applied(
                    scope, session_id, key_hash,
                )
                if prior_json is None:
                    log_check("prior_snapshot_missing")
                    raise ValueError("Prior order snapshot is unavailable")
                prior = SessionItemsResponse.model_validate(prior_json)
                if prior.order.id != order_id:
                    log_check("prior_order_id_mismatch")
                    raise ValueError("Prior order changed")
                required: Counter[tuple[str, tuple[str, ...]]] = Counter()
                for line in prior.order.items:
                    if line.item_id is None:
                        log_check("prior_item_identity_missing")
                        raise ValueError("Prior item identity is unavailable")
                    signature = (line.item_id, tuple(sorted(mod.id for mod in line.modifiers)))
                    required[signature] += line.quantity
                    expected_items[line.item_id] += line.quantity
                    for modifier in line.modifiers:
                        expected_modifiers[modifier.id] += line.quantity
                for line in expected_body.items:
                    signature = (line.item_id, tuple(sorted(line.modifier_ids)))
                    required[signature] += line.quantity
                    expected_items[line.item_id] += line.quantity
                    for modifier_id in line.modifier_ids:
                        expected_modifiers[modifier_id] += line.quantity
                observed: Counter[tuple[str, tuple[str, ...]]] = Counter()
                for line in order.lineItems.elements:
                    if line.item is None:
                        log_check("actual_item_identity_missing")
                        raise ValueError("Current item identity is unavailable")
                    modifiers = line.modifications.elements if line.modifications else []
                    if any(mod.modifier is None for mod in modifiers):
                        log_check("actual_modifier_identity_missing")
                        raise ValueError("Current modifier identity is unavailable")
                    modifier_ids = tuple(sorted(
                        mod.modifier.id for mod in modifiers if mod.modifier
                    ))
                    observed[(line.item.id, modifier_ids)] += 1
                    actual_items[line.item.id] += 1
                    for modifier_id in modifier_ids:
                        actual_modifiers[modifier_id] += 1
                if any(actual_items[item_id] < count for item_id, count in expected_items.items()):
                    log_check("item_count_mismatch")
                    raise ValueError("Order item counts do not match")
                if any(actual_modifiers[modifier_id] < count
                       for modifier_id, count in expected_modifiers.items()):
                    log_check("modifier_count_mismatch")
                    raise ValueError("Order modifier counts do not match")
                remaining = observed.copy()
                for (item_id, modifier_ids), count in sorted(
                    required.items(), key=lambda entry: len(entry[0][1]), reverse=True,
                ):
                    needed = count
                    for (actual_item, actual_modifier_ids), available in list(remaining.items()):
                        if (actual_item != item_id or
                                not set(modifier_ids).issubset(actual_modifier_ids)):
                            continue
                        used = min(needed, available)
                        remaining[(actual_item, actual_modifier_ids)] -= used
                        needed -= used
                        if needed == 0:
                            break
                    if needed:
                        log_check("line_modifier_pair_mismatch")
                        raise ValueError("Order line modifiers do not match")
            if phase == "total_confirmed" and settings.app_env == "development":
                logger.warning(
                    "DEV Clover final order readback order_suffix=%s final_total_cents=%s",
                    order_id[-4:] if len(order_id) > 4 else "****", order.total,
                )
            if phase == "total_confirmed" and (
                not isinstance(metadata, dict) or
                order.total != metadata.get("target_total_cents")
            ):
                log_check("total_mismatch")
                raise ValueError("Confirmed order total is not yet readable")
            current = await self.repository.get(scope, session_id)
            if current is None or current.clover_order_id != order.id:
                raise ValueError("Table session order changed")
            item_names = await self._readback_item_names(order, client, settings)
            response = SessionItemsResponse(
                **TableSessionResponse.from_record(current).model_dump(),
                order=normalize_readback(order, item_names),
            )
            await self.repository.finish(
                scope, session_id, key_hash, response.model_dump(mode="json"),
                readback_lines=[{
                    "id": line.id,
                    "item_id": line.item.id if line.item else None,
                    "modifiers": [{"id": mod.modifier.id} for mod in
                                  (line.modifications.elements if line.modifications else [])
                                  if mod.modifier],
                } for line in order.lineItems.elements],
            )
            log_check("passed")
            return response
        except CloverReadbackFailure as exc:
            log_check(exc.reason)
            await self._confirmation_pending(scope, session_id, key_hash)
            raise OrderConfirmationPending() from None
        except CloverReconnectionRequiredError:
            log_check("authentication_reconnect")
            await self._confirmation_pending(scope, session_id, key_hash)
            raise OrderConfirmationPending() from None
        except HTTPException:
            raise
        except Exception:
            if not logged_failure:
                log_check("normalization_or_storage_failure")
            await self._confirmation_pending(scope, session_id, key_hash)
            raise OrderConfirmationPending() from None

    async def _release(self, scope: str, session_id: str, key_hash: str) -> None:
        try:
            await self.repository.release(scope, session_id, key_hash)
        except Exception:
            raise HTTPException(503, "Table-session storage is unavailable") from None

    async def _uncertain(self, scope: str, session_id: str, key_hash: str) -> None:
        try:
            await self.repository.uncertain(scope, session_id, key_hash)
        except Exception:
            raise HTTPException(503, "Order outcome needs reconciliation; do not retry") from None

    async def _confirmation_pending(self, scope: str, session_id: str,
                                    key_hash: str) -> None:
        try:
            await self.repository.confirmation_pending(scope, session_id, key_hash)
        except Exception:
            raise HTTPException(503, "Order storage is unavailable; keep the same key") from None
