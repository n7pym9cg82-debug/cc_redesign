"""Kitchen workflow uses only synthetic confirmed snapshots and temporary SQLite."""

import sqlite3
import pytest

from app.admin.api import _summary
from app.table_sessions.store import (
    InvalidKitchenTransition, SQLiteTableSessionRepository, UnsafeSessionClosure,
)

SCOPE = "sandbox:synthetic-merchant"


def response(session_id: str, table: str, items: list[dict]) -> dict:
    return {
        "session_id": session_id, "table": table, "status": "open",
        "clover_order_id": "SYNTHETIC_ORDER", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "closed_at": None,
        "order": {"id": "SYNTHETIC_ORDER", "currency": "USD", "subtotal": 0,
                  "tax": 0, "total": 0, "items": items},
    }


def item(item_id: str, quantity: int = 1, modifiers: list[dict] | None = None) -> dict:
    return {"item_id": item_id, "name": item_id, "quantity": quantity,
            "unit_price": 1, "modifiers": modifiers or []}


async def confirm(repo: SQLiteTableSessionRepository, session_id: str, table: str,
                  key: str, requested: list[dict], all_items: list[dict]) -> None:
    state, _, _ = await repo.claim(SCOPE, session_id, key, key)
    assert state == "claimed"
    await repo.applied(SCOPE, session_id, key, "SYNTHETIC_ORDER",
                       requested_items=requested)
    await repo.finish(SCOPE, session_id, key, response(session_id, table, all_items))


@pytest.mark.asyncio
async def test_confirmed_lines_addons_statuses_and_close(tmp_path) -> None:
    repo = SQLiteTableSessionRepository(tmp_path / "kitchen.sqlite3")
    session = await repo.get_or_create(SCOPE, "T18")
    first = [item("BIRYANI"), item("NAAN", 2)]
    await confirm(repo, session.session_id, "T18", "first-key", [
        {"item_id": "BIRYANI", "quantity": 1, "modifier_ids": []},
        {"item_id": "NAAN", "quantity": 2, "modifier_ids": []},
    ], first)
    snapshot = await repo.admin_session(SCOPE, session.session_id)
    assert snapshot is not None
    summary = _summary(snapshot)
    assert summary.kitchen_state == "NEW_ORDER"
    assert (summary.new_count, summary.ready_count, summary.delivered_count,
            summary.open_count, summary.total_item_lines) == (2, 0, 0, 2, 2)
    assert sorted(line.quantity for line in snapshot.kitchen_lines) == [1, 2]
    assert summary.safe_to_close is False
    with pytest.raises(UnsafeSessionClosure):
        await repo.close(SCOPE, session.session_id)

    first_ids = [line.kitchen_line_id for line in snapshot.kitchen_lines]
    with pytest.raises(InvalidKitchenTransition):
        await repo.transition_kitchen_line(SCOPE, first_ids[0], "DELIVERED")
    await repo.transition_kitchen_line(SCOPE, first_ids[0], "READY")
    assert _summary(await repo.admin_session(SCOPE, session.session_id)).kitchen_state == "NEW_ORDER"
    with pytest.raises(UnsafeSessionClosure):
        await repo.close(SCOPE, session.session_id)
    for line_id in first_ids:
        if line_id != first_ids[0]:
            await repo.transition_kitchen_line(SCOPE, line_id, "READY")
        await repo.transition_kitchen_line(SCOPE, line_id, "DELIVERED")
    with pytest.raises(InvalidKitchenTransition):
        await repo.transition_kitchen_line(SCOPE, first_ids[0], "READY")
    assert _summary(await repo.admin_session(SCOPE, session.session_id)).kitchen_state == "ALL_DELIVERED"
    count = (await repo.admin_session(SCOPE, session.session_id)).mutation_count
    assert await repo.update_payment_state(SCOPE, session.session_id, "SYNTHETIC_ORDER", "PAID", count)
    assert _summary(await repo.admin_session(SCOPE, session.session_id)).payment_state == "PAID"

    sweet = [{"id": "SWEET", "name": "Sweet", "price_delta": 0}]
    await confirm(repo, session.session_id, "T18", "addon-key", [
        {"item_id": "LASSI", "quantity": 1, "modifier_ids": ["SWEET"]},
    ], [*first, item("LASSI", modifiers=sweet)])
    snapshot = await repo.admin_session(SCOPE, session.session_id)
    assert snapshot is not None
    assert len(snapshot.kitchen_lines) == 3
    assert [line.status for line in snapshot.kitchen_lines].count("DELIVERED") == 2
    new_line = next(line for line in snapshot.kitchen_lines if line.item_id == "LASSI")
    assert new_line.status == "NEW" and new_line.modifiers == sweet
    assert _summary(snapshot).kitchen_state == "NEW_ORDER"
    assert _summary(snapshot).payment_state == "UNKNOWN"
    # A successful idempotency-key replay is served from the prior response.
    assert (await repo.claim(SCOPE, session.session_id, "addon-key", "addon-key"))[0] == "succeeded"
    assert len((await repo.admin_session(SCOPE, session.session_id)).kitchen_lines) == 3
    await repo.transition_kitchen_line(SCOPE, new_line.kitchen_line_id, "READY")
    await repo.transition_kitchen_line(SCOPE, new_line.kitchen_line_id, "DELIVERED")
    assert not _summary(await repo.admin_session(SCOPE, session.session_id)).safe_to_close
    count = (await repo.admin_session(SCOPE, session.session_id)).mutation_count
    assert not await repo.update_payment_state(SCOPE, session.session_id, "SYNTHETIC_ORDER", "PAID", count - 1)
    assert await repo.update_payment_state(SCOPE, session.session_id, "SYNTHETIC_ORDER", "PAID", count)
    assert _summary(await repo.admin_session(SCOPE, session.session_id)).safe_to_close
    await repo.close(SCOPE, session.session_id)
    closed = await repo.admin_session(SCOPE, session.session_id)
    assert closed is not None and _summary(closed).kitchen_state == "CLOSED"
    assert len(closed.kitchen_lines) == 3
    with pytest.raises(InvalidKitchenTransition):
        await repo.transition_kitchen_line(SCOPE, new_line.kitchen_line_id, "READY")
    fresh = await repo.get_or_create(SCOPE, "T18")
    assert fresh.session_id != session.session_id
    assert (await repo.admin_session(SCOPE, fresh.session_id)).kitchen_lines == []


@pytest.mark.asyncio
async def test_unresolved_mutation_blocks_transition_and_close(tmp_path) -> None:
    repo = SQLiteTableSessionRepository(tmp_path / "kitchen.sqlite3")
    session = await repo.get_or_create(SCOPE, "T19")
    await confirm(repo, session.session_id, "T19", "first-key", [
        {"item_id": "TEA", "quantity": 1, "modifier_ids": []},
    ], [item("TEA")])
    line_id = (await repo.admin_session(SCOPE, session.session_id)).kitchen_lines[0].kitchen_line_id
    assert (await repo.claim(SCOPE, session.session_id, "pending-key", "pending-key"))[0] == "claimed"
    with pytest.raises(InvalidKitchenTransition, match="sync needs review"):
        await repo.transition_kitchen_line(SCOPE, line_id, "READY")
    with pytest.raises(UnsafeSessionClosure):
        await repo.close(SCOPE, session.session_id)


@pytest.mark.asyncio
async def test_active_tables_sort_green_then_amber_then_red(tmp_path) -> None:
    repo = SQLiteTableSessionRepository(tmp_path / "kitchen.sqlite3")
    sessions = {}
    for table in ("T20", "T21", "T22"):
        session = await repo.get_or_create(SCOPE, table)
        sessions[table] = session
        await confirm(repo, session.session_id, table, f"key-{table}", [
            {"item_id": "TEA", "quantity": 1, "modifier_ids": []},
        ], [item("TEA")])
    for table, target in (("T21", "READY"), ("T22", "DELIVERED")):
        line_id = (await repo.admin_session(SCOPE, sessions[table].session_id)).kitchen_lines[0].kitchen_line_id
        await repo.transition_kitchen_line(SCOPE, line_id, "READY")
        if target == "DELIVERED":
            await repo.transition_kitchen_line(SCOPE, line_id, "DELIVERED")
    active = await repo.admin_sessions(SCOPE, "open", -1, 0)
    assert [row.session.table for row in active] == ["T20", "T21", "T22"]


@pytest.mark.asyncio
async def test_existing_session_database_gains_nullable_payment_columns(tmp_path) -> None:
    path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE table_sessions (
            session_id TEXT PRIMARY KEY, scope TEXT NOT NULL, table_id TEXT NOT NULL,
            status TEXT NOT NULL, clover_order_id TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, closed_at TEXT, operation_state TEXT NOT NULL
        )""")
        db.execute("INSERT INTO table_sessions VALUES (?,?,?,?,?,?,?,?,?)", (
            "a" * 32, SCOPE, "T33", "open", "ORDER33", "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00", None, "idle",
        ))
    repo = SQLiteTableSessionRepository(path)
    snapshot = await repo.admin_session(SCOPE, "a" * 32)
    assert snapshot is not None and snapshot.session.clover_order_id == "ORDER33"
    assert snapshot.payment_state is None and snapshot.payment_checked_at is None
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(table_sessions)")}
    assert {"payment_state", "payment_checked_at"}.issubset(columns)


@pytest.mark.asyncio
async def test_stale_paid_cache_cannot_close(tmp_path) -> None:
    repo = SQLiteTableSessionRepository(tmp_path / "stale.sqlite3")
    session = await repo.get_or_create(SCOPE, "T34")
    await confirm(repo, session.session_id, "T34", "first-key", [
        {"item_id": "TEA", "quantity": 1, "modifier_ids": []},
    ], [item("TEA")])
    snapshot = await repo.admin_session(SCOPE, session.session_id)
    line_id = snapshot.kitchen_lines[0].kitchen_line_id
    await repo.transition_kitchen_line(SCOPE, line_id, "READY")
    await repo.transition_kitchen_line(SCOPE, line_id, "DELIVERED")
    assert await repo.update_payment_state(SCOPE, session.session_id,
                                           "SYNTHETIC_ORDER", "PAID", snapshot.mutation_count)
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE table_sessions SET payment_checked_at=? WHERE session_id=?",
                   ("2020-01-01T00:00:00+00:00", session.session_id))
    assert not _summary(await repo.admin_session(SCOPE, session.session_id)).safe_to_close
    with pytest.raises(UnsafeSessionClosure):
        await repo.close(SCOPE, session.session_id)
