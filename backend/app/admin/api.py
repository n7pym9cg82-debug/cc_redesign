"""Authenticated, read-mostly staff API for local table-session operations."""

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.admin.auth import (
    COOKIE_NAME, COOKIE_PATH, check_login_rate, issue_session, require_admin,
    require_same_origin, verify_password,
)
from app.admin.tables import PHYSICAL_TABLES
from app.api.table_sessions import get_table_session_service
from app.api.menu import get_clover_client
from app.core.config import Settings, get_settings
from app.integrations.clover.client import CloverClient
from app.integrations.clover.exceptions import (
    CloverAuthenticationError, CloverOrderNotFoundError,
    CloverReconnectionRequiredError, CloverUnavailableError,
)
from app.public.tokens import display_table
from app.table_sessions.models import SessionLineResponse, SessionOrderResponse
from app.table_sessions.service import TableSessionService
from app.table_sessions.store import (
    AdminSessionSnapshot, InvalidKitchenTransition, UnsafeSessionClosure,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class ForceCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AdminMe(BaseModel):
    username: str


class AdminOrder(BaseModel):
    currency: str | None
    total: float | None
    items: list[SessionLineResponse]


class AdminKitchenLine(BaseModel):
    kitchen_line_id: str
    item_id: str | None
    item_name: str
    quantity: int
    modifiers: list[dict]
    status: str
    created_at: str
    ready_at: str | None
    delivered_at: str | None


class AdminSession(BaseModel):
    session_id: str
    table: str
    display_name: str
    status: str
    opened_at: str
    last_activity_at: str
    closed_at: str | None
    clover_order_ref: str | None
    clover_order_status: str | None = None
    mutation_count: int
    reconciliation_pending: bool
    healthy: bool
    safe_to_close: bool
    safe_to_force_close: bool
    forced_close: bool
    forced_close_reason: str | None
    forced_close_at: str | None
    forced_close_by: str | None
    forced_close_payment_state: str | None
    forced_close_unresolved: bool
    order: AdminOrder | None
    kitchen_state: str
    new_count: int
    ready_count: int
    delivered_count: int
    open_count: int
    total_item_lines: int
    kitchen_lines: list[AdminKitchenLine]
    payment_state: str
    payment_checked_at: str | None
    payment_warning: str | None


class PrepTable(BaseModel):
    session_id: str
    table: str
    new_quantity: int
    ready_quantity: int
    remaining_quantity: int


class PrepItem(BaseModel):
    item_id: str
    name: str
    new_quantity: int
    ready_quantity: int
    remaining_quantity: int
    tables: list[PrepTable]


class PrepSummary(BaseModel):
    items: list[PrepItem]


class AdminSessionPage(BaseModel):
    sessions: list[AdminSession]
    next_offset: int | None


class AdminPhysicalTable(BaseModel):
    table_id: str
    display_name: str
    column: str
    position: int
    session: AdminSession | None
    kitchen_state: str
    new_count: int
    ready_count: int
    delivered_count: int
    open_count: int


class AdminPhysicalTablePage(BaseModel):
    tables: list[AdminPhysicalTable]


def _session_id(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise HTTPException(404, "Table session not found")
    return value


def _summary(snapshot: AdminSessionSnapshot) -> AdminSession:
    session = snapshot.session
    order = None
    if snapshot.latest_order:
        try:
            normalized = SessionOrderResponse.model_validate(snapshot.latest_order)
        except ValidationError:
            normalized = None
        if normalized is not None:
            order = AdminOrder(
                currency=normalized.currency,
                total=float(normalized.total) if normalized.total is not None else None,
                items=normalized.items,
            )
    lines = snapshot.kitchen_lines
    new_count = sum(line.status == "NEW" for line in lines)
    ready_count = sum(line.status == "READY" for line in lines)
    delivered_count = sum(line.status == "DELIVERED" for line in lines)
    kitchen_state = ("CLOSED" if session.status == "closed" else
                     "AWAITING_ORDER" if not lines else
                     "NEW_ORDER" if new_count > 0 else
                     "ALL_DELIVERED" if delivered_count == len(lines) else
                     "IN_PROGRESS")
    paid_recently = False
    if snapshot.payment_state == "PAID" and snapshot.payment_checked_at:
        try:
            paid_recently = (datetime.fromisoformat(snapshot.payment_checked_at) >=
                             datetime.now(timezone.utc) - timedelta(seconds=30))
        except ValueError:
            pass
    payment_state = {"OPEN": "UNPAID", "PARTIALLY_PAID": "PARTIALLY_PAID",
                     "PAID": "PAID"}.get(snapshot.payment_state,
                                        "UNAVAILABLE" if snapshot.payment_warning else "UNKNOWN")
    return AdminSession(
        session_id=session.session_id,
        table=session.table,
        display_name=display_table(session.table),
        status=session.status,
        opened_at=session.created_at,
        last_activity_at=session.updated_at,
        closed_at=session.closed_at,
        clover_order_ref=(f"…{session.clover_order_id[-4:]}"
                          if session.clover_order_id else None),
        mutation_count=snapshot.mutation_count,
        reconciliation_pending=snapshot.unresolved,
        healthy=not snapshot.unresolved,
        safe_to_close=session.status == "open" and not snapshot.unresolved
        and new_count + ready_count == 0 and bool(lines) and paid_recently,
        safe_to_force_close=session.status == "open" and bool(lines)
        and new_count + ready_count == 0,
        forced_close=session.forced_close,
        forced_close_reason=session.forced_close_reason,
        forced_close_at=session.forced_close_at,
        forced_close_by=session.forced_close_by,
        forced_close_payment_state=session.forced_close_payment_state,
        forced_close_unresolved=session.forced_close_unresolved,
        order=order,
        kitchen_state=kitchen_state,
        new_count=new_count,
        ready_count=ready_count,
        delivered_count=delivered_count,
        open_count=new_count + ready_count,
        total_item_lines=len(lines),
        kitchen_lines=[AdminKitchenLine(
            kitchen_line_id=line.kitchen_line_id, item_id=line.item_id,
            item_name=line.item_name, quantity=line.quantity,
            modifiers=line.modifiers, status=line.status,
            created_at=line.created_at, ready_at=line.ready_at,
            delivered_at=line.delivered_at,
        ) for line in lines],
        payment_state=payment_state,
        payment_checked_at=snapshot.payment_checked_at,
        payment_warning=snapshot.payment_warning,
    )


def admin_guard(
    request: Request, settings: Settings = Depends(get_settings),
) -> str:
    return require_admin(request, settings)


@router.post("/login", response_model=AdminMe)
async def login(
    body: LoginRequest, request: Request, response: Response,
    settings: Settings = Depends(get_settings),
) -> AdminMe:
    require_same_origin(request, settings)
    check_login_rate(request)
    stored = settings.admin_password_hash.get_secret_value()
    valid_password = verify_password(body.password, stored)
    if not valid_password or body.username != settings.admin_username:
        raise HTTPException(401, "Invalid admin credentials")
    response.set_cookie(
        COOKIE_NAME, issue_session(settings), max_age=settings.admin_session_hours * 3600,
        httponly=True, secure=settings.app_env != "development", samesite="lax",
        path=COOKIE_PATH,
    )
    return AdminMe(username=settings.admin_username)


@router.post("/logout", dependencies=[Depends(admin_guard)])
async def logout(
    request: Request, response: Response, settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    require_same_origin(request, settings)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH,
                           secure=settings.app_env != "development", httponly=True,
                           samesite="lax")
    return {"ok": True}


@router.get("/me", response_model=AdminMe)
async def me(username: str = Depends(admin_guard)) -> AdminMe:
    return AdminMe(username=username)


@router.get("/table-sessions", response_model=AdminSessionPage,
            dependencies=[Depends(admin_guard)])
async def active_sessions(
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> AdminSessionPage:
    scope = service.scope(settings)
    snapshots = await service.repository.admin_sessions(scope, "open", -1, 0)
    return AdminSessionPage(sessions=[_summary(row) for row in snapshots], next_offset=None)


@router.get("/tables", response_model=AdminPhysicalTablePage,
            dependencies=[Depends(admin_guard)])
async def physical_tables(
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> AdminPhysicalTablePage:
    snapshots = await service.repository.admin_sessions(service.scope(settings), "open", -1, 0)
    # A physical table is represented once, even when an old closed session exists.
    active = {row.session.table: _summary(row) for row in snapshots}
    return AdminPhysicalTablePage(tables=[AdminPhysicalTable(
        table_id=table.table_id, display_name=table.display_name,
        column=table.column, position=table.position,
        session=active.get(table.table_id),
        kitchen_state=(active[table.table_id].kitchen_state
                       if table.table_id in active and active[table.table_id].total_item_lines
                       else "AVAILABLE"),
        new_count=active[table.table_id].new_count if table.table_id in active else 0,
        ready_count=active[table.table_id].ready_count if table.table_id in active else 0,
        delivered_count=active[table.table_id].delivered_count if table.table_id in active else 0,
        open_count=active[table.table_id].open_count if table.table_id in active else 0,
    ) for table in PHYSICAL_TABLES])


@router.get("/prep-summary", response_model=PrepSummary,
            dependencies=[Depends(admin_guard)])
async def prep_summary(
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> PrepSummary:
    snapshots = await service.repository.admin_sessions(service.scope(settings), "open", -1, 0)
    grouped: dict[str, dict] = {}
    for snapshot in snapshots:
        for line in snapshot.kitchen_lines:
            if line.status == "DELIVERED":
                continue
            # Unknown/custom Clover lines remain distinct; never group by name.
            item_id = line.item_id or f"custom:{line.kitchen_line_id}"
            group = grouped.setdefault(item_id, {"name": line.item_name,
                                                  "new_quantity": 0, "ready_quantity": 0,
                                                  "tables": {}})
            field = "new_quantity" if line.status == "NEW" else "ready_quantity"
            group[field] += line.quantity
            table = group["tables"].setdefault(snapshot.session.session_id, {
                "session_id": snapshot.session.session_id, "table": snapshot.session.table,
                "new_quantity": 0, "ready_quantity": 0,
            })
            table[field] += line.quantity
    items = [PrepItem(
        item_id=item_id, name=group["name"],
        new_quantity=group["new_quantity"], ready_quantity=group["ready_quantity"],
        remaining_quantity=group["new_quantity"] + group["ready_quantity"],
        tables=[PrepTable(**table, remaining_quantity=table["new_quantity"] + table["ready_quantity"])
                for table in sorted(group["tables"].values(), key=lambda row: row["table"])],
    ) for item_id, group in grouped.items()]
    items.sort(key=lambda item: (-item.remaining_quantity, item.name, item.item_id))
    return PrepSummary(items=items)


@router.get("/table-sessions/history", response_model=AdminSessionPage,
            dependencies=[Depends(admin_guard)])
async def history(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> AdminSessionPage:
    scope = service.scope(settings)
    snapshots = await service.repository.admin_sessions(scope, "closed", limit + 1, offset)
    more = len(snapshots) > limit
    return AdminSessionPage(
        sessions=[_summary(row) for row in snapshots[:limit]],
        next_offset=offset + limit if more else None,
    )


@router.get("/table-sessions/{session_id}", response_model=AdminSession,
            dependencies=[Depends(admin_guard)])
async def session_detail(
    session_id: str,
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> AdminSession:
    scope = service.scope(settings)
    snapshot = await service.repository.admin_session(scope, _session_id(session_id))
    if snapshot is None:
        raise HTTPException(404, "Table session not found")
    return _summary(snapshot)


@router.post("/table-sessions/{session_id}/close", response_model=AdminSession,
             dependencies=[Depends(admin_guard)])
async def close_session(
    session_id: str, request: Request,
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
    client: CloverClient = Depends(get_clover_client),
) -> AdminSession:
    require_same_origin(request, settings)
    session_id = _session_id(session_id)
    snapshot = await service.repository.admin_session(service.scope(settings), session_id)
    if snapshot is None:
        raise HTTPException(404, "Table session not found")
    if snapshot.session.status == "open":
        if snapshot.unresolved:
            raise HTTPException(409, "Table session has an unresolved operation")
        if not snapshot.kitchen_lines or any(
            line.status != "DELIVERED" for line in snapshot.kitchen_lines
        ):
            raise HTTPException(409, "Table has undelivered kitchen items")
        payment = await _refresh_payment(session_id, settings, service, client)
        if payment.payment_warning:
            raise HTTPException(503, "Clover payment status is unavailable")
    await service.close(settings, _session_id(session_id))
    snapshot = await service.repository.admin_session(service.scope(settings), session_id)
    if snapshot is None:
        raise HTTPException(404, "Table session not found")
    return _summary(snapshot)


@router.post("/table-sessions/{session_id}/force-close", response_model=AdminSession)
async def force_close_session(
    session_id: str, body: ForceCloseRequest, request: Request,
    username: str = Depends(admin_guard),
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> AdminSession:
    require_same_origin(request, settings)
    session_id = _session_id(session_id)
    scope = service.scope(settings)
    try:
        closed = await service.repository.force_close(scope, session_id, body.reason, username)
    except UnsafeSessionClosure:
        raise HTTPException(409, "Table has undelivered kitchen items") from None
    if closed is None:
        raise HTTPException(404, "Table session not found")
    snapshot = await service.repository.admin_session(scope, session_id)
    if snapshot is None:
        raise HTTPException(404, "Table session not found")
    return _summary(snapshot)


async def _refresh_payment(
    session_id: str, settings: Settings, service: TableSessionService,
    client: CloverClient,
) -> AdminSession:
    scope = service.scope(settings)
    snapshot = await service.repository.admin_session(scope, session_id)
    if snapshot is None:
        raise HTTPException(404, "Table session not found")
    order_id = snapshot.session.clover_order_id
    if snapshot.session.status != "open" or not order_id:
        raise HTTPException(409, "No open Clover order is available for payment refresh")
    if snapshot.unresolved:
        raise HTTPException(409, "Order sync needs review before payment can be confirmed")
    if client.merchant_id != settings.clover_merchant_id:
        raise HTTPException(503, "Clover merchant configuration is inconsistent")
    try:
        state = await client.get_order_payment_state(order_id)
    except CloverOrderNotFoundError:
        warning = "Clover order could not be found"
    except (CloverAuthenticationError, CloverReconnectionRequiredError, CloverUnavailableError):
        warning = "Clover payment could not be confirmed"
    else:
        warning = None
    if warning is not None:
        await service.repository.clear_payment_state(scope, session_id, order_id, warning)
        refreshed = await service.repository.admin_session(scope, session_id)
        if refreshed is None:
            raise HTTPException(404, "Table session not found")
        return _summary(refreshed)
    if not await service.repository.update_payment_state(
        scope, session_id, order_id, state, snapshot.mutation_count,
    ):
        raise HTTPException(409, "Order sync needs review before payment can be confirmed")
    refreshed = await service.repository.admin_session(scope, session_id)
    if refreshed is None:
        raise HTTPException(404, "Table session not found")
    return _summary(refreshed)


@router.post("/table-sessions/{session_id}/refresh-payment", response_model=AdminSession,
             dependencies=[Depends(admin_guard)])
async def refresh_payment(
    session_id: str, request: Request,
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
    client: CloverClient = Depends(get_clover_client),
) -> AdminSession:
    require_same_origin(request, settings)
    return await _refresh_payment(_session_id(session_id), settings, service, client)


async def _transition(
    kitchen_line_id: str, target: str, request: Request,
    settings: Settings, service: TableSessionService,
) -> AdminSession:
    require_same_origin(request, settings)
    if re.fullmatch(r"[0-9a-f]{32}", kitchen_line_id) is None:
        raise HTTPException(404, "Kitchen item not found")
    scope = service.scope(settings)
    try:
        session_id = await service.repository.transition_kitchen_line(
            scope, kitchen_line_id, target,
        )
    except InvalidKitchenTransition as exc:
        raise HTTPException(404 if str(exc) == "Kitchen item not found" else 409,
                            str(exc)) from None
    snapshot = await service.repository.admin_session(scope, session_id)
    if snapshot is None:
        raise HTTPException(404, "Table session not found")
    return _summary(snapshot)


@router.post("/kitchen-items/{kitchen_line_id}/ready", response_model=AdminSession,
             dependencies=[Depends(admin_guard)])
async def mark_ready(
    kitchen_line_id: str, request: Request,
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> AdminSession:
    return await _transition(kitchen_line_id, "READY", request, settings, service)


@router.post("/kitchen-items/{kitchen_line_id}/delivered", response_model=AdminSession,
             dependencies=[Depends(admin_guard)])
async def mark_delivered(
    kitchen_line_id: str, request: Request,
    settings: Settings = Depends(get_settings),
    service: TableSessionService = Depends(get_table_session_service),
) -> AdminSession:
    return await _transition(kitchen_line_id, "DELIVERED", request, settings, service)
