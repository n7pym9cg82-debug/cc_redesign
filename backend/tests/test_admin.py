"""Staff admin lifecycle with synthetic configuration and temporary SQLite only."""

import json
import sqlite3

import httpx
import pytest
from cryptography.fernet import Fernet

from app.admin.auth import hash_password, issue_session
from app.api.menu import get_clover_client
from app.core import config as config_module
from app.core.config import Settings, get_settings
from app.main import app
from app.public.tokens import TableTokenService
from app.table_sessions.store import SQLiteTableSessionRepository
from app.integrations.clover.exceptions import CloverOrderNotFoundError, CloverUnavailableError

ORIGIN = "http://localhost:3000"


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password("synthetic-password", iterations=100_000)


@pytest.fixture
def admin_settings(password_hash: str) -> Settings:
    import app.admin.auth as auth
    auth._login_attempts.clear()
    settings = Settings(
        _env_file=None, app_env="development", clover_env="sandbox",
        clover_merchant_id="synthetic-merchant", frontend_origin=ORIGIN,
        table_token_signing_key=Fernet.generate_key().decode(),
        admin_username="operator", admin_password_hash=password_hash,
        admin_session_secret="synthetic-admin-signing-secret-with-48-characters",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_clover_client] = lambda: (_ for _ in ()).throw(
        AssertionError("Admin must never call Clover")
    )
    return settings


@pytest.fixture
def repo(admin_settings: Settings) -> SQLiteTableSessionRepository:
    return SQLiteTableSessionRepository(config_module.LOCAL_TABLE_SESSION_DATABASE)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def login(http: httpx.AsyncClient, *, password: str = "synthetic-password") -> httpx.Response:
    return await http.post("/api/admin/login", json={
        "username": "operator", "password": password,
    }, headers={"Origin": ORIGIN})


class FakePaymentClient:
    merchant_id = "synthetic-merchant"

    def __init__(self, state: str = "OPEN") -> None:
        self.state = state
        self.calls: list[str] = []

    async def get_order_payment_state(self, order_id: str) -> str:
        self.calls.append(order_id)
        if self.state == "NOT_FOUND":
            raise CloverOrderNotFoundError("Clover order could not be found")
        if self.state == "FAIL":
            raise CloverUnavailableError("synthetic unavailable")
        return self.state


@pytest.mark.asyncio
async def test_login_auth_cookie_logout_and_rejection(admin_settings: Settings) -> None:
    async with client() as http:
        assert (await http.get("/api/admin/me")).status_code == 401
        assert (await login(http, password="wrong")).status_code == 401
        response = await login(http)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert "Path=/api/admin" in cookie and "Max-Age=" in cookie
        assert "Secure" not in cookie
        assert (await http.get("/api/admin/me")).json() == {"username": "operator"}
        assert (await http.post("/api/admin/logout", headers={"Origin": ORIGIN})).status_code == 200
        assert (await http.get("/api/admin/me")).status_code == 401


@pytest.mark.asyncio
async def test_origin_cookie_tampering_and_expiry(admin_settings: Settings, monkeypatch) -> None:
    async with client() as http:
        assert (await http.post("/api/admin/login", json={
            "username": "operator", "password": "synthetic-password",
        }, headers={"Origin": "https://wrong.example"})).status_code == 403
        assert (await login(http)).status_code == 200
        assert (await http.get("/api/admin/me")).status_code == 200
        token = http.cookies.get("chennai_admin_session")
        assert token
        http.cookies.set("chennai_admin_session", token[:-3] + "bad", path="/api/admin")
        assert (await http.get("/api/admin/me")).status_code == 401
    import app.admin.auth as auth
    monkeypatch.setattr(auth.time, "time", lambda: 1_000_000)
    expired = issue_session(admin_settings)
    monkeypatch.setattr(auth.time, "time", lambda: 1_000_000 + 9 * 3600)
    async with client() as http:
        http.cookies.set("chennai_admin_session", expired, path="/api/admin")
        assert (await http.get("/api/admin/me")).status_code == 401
    production = admin_settings.model_copy(update={"app_env": "production"})
    app.dependency_overrides[get_settings] = lambda: production
    async with client() as http:
        response = await login(http)
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_active_detail_close_history_and_new_qr_session(
    admin_settings: Settings, repo: SQLiteTableSessionRepository,
) -> None:
    scope = "sandbox:synthetic-merchant"
    old = await repo.get_or_create(scope, "T18")
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE table_sessions SET clover_order_id=? WHERE session_id=?",
                   ("CLOVER_ORDER_SECRET_1234", old.session_id))
        db.execute(
            "INSERT INTO table_session_mutations "
            "(scope,session_id,key_hash,fingerprint,state,response_json) "
            "VALUES (?,?,?,?,?,?)",
            (scope, old.session_id, "hashed-private-key", "fingerprint", "succeeded", json.dumps({
                "order": {"id": "CLOVER_ORDER_SECRET_1234", "currency": "USD",
                          "subtotal": 16.99, "tax": 0, "total": 16.99,
                          "items": [{"item_id": "ITEM1", "name": "Biryani", "quantity": 1,
                                     "unit_price": 16.99, "modifiers": []}]},
            })),
        )
    token = TableTokenService(admin_settings).issue("T18")
    payment = FakePaymentClient()
    app.dependency_overrides[get_clover_client] = lambda: payment
    async with client() as http:
        assert (await http.get("/api/admin/table-sessions")).status_code == 401
        assert (await login(http)).status_code == 200
        active = (await http.get("/api/admin/table-sessions")).json()["sessions"]
        assert len(active) == 1 and active[0]["display_name"] == "Table 18"
        assert active[0]["mutation_count"] == 1
        assert active[0]["clover_order_ref"] == "…1234"
        assert "CLOVER_ORDER_SECRET" not in json.dumps(active)
        detail = (await http.get(f"/api/admin/table-sessions/{old.session_id}")).json()
        assert detail["order"]["total"] == 16.99
        assert detail["order"]["items"][0]["name"] == "Biryani"
        assert detail["kitchen_state"] == "NEW_ORDER"
        assert detail["safe_to_close"] is False
        line_id = detail["kitchen_lines"][0]["kitchen_line_id"]
        async with client() as anonymous:
            assert (await anonymous.post(f"/api/admin/kitchen-items/{line_id}/ready",
                                         headers={"Origin": ORIGIN})).status_code == 401
        assert (await http.post(f"/api/admin/kitchen-items/{line_id}/ready",
                                headers={"Origin": "https://wrong.example"})).status_code == 403
        assert (await http.post(f"/api/admin/kitchen-items/{line_id}/delivered",
                                headers={"Origin": ORIGIN})).status_code == 409
        assert (await http.post(f"/api/admin/table-sessions/{old.session_id}/close",
                                headers={"Origin": ORIGIN})).status_code == 409
        ready = await http.post(f"/api/admin/kitchen-items/{line_id}/ready",
                                headers={"Origin": ORIGIN})
        assert ready.status_code == 200 and ready.json()["kitchen_state"] == "IN_PROGRESS"
        assert (await http.post(f"/api/admin/kitchen-items/{line_id}/ready",
                                headers={"Origin": ORIGIN})).status_code == 409
        delivered = await http.post(f"/api/admin/kitchen-items/{line_id}/delivered",
                                    headers={"Origin": ORIGIN})
        assert delivered.status_code == 200 and delivered.json()["safe_to_close"] is False
        unpaid = await http.post(f"/api/admin/table-sessions/{old.session_id}/refresh-payment",
                                 headers={"Origin": ORIGIN})
        assert unpaid.json()["payment_state"] == "UNPAID"
        assert (await http.post(f"/api/admin/table-sessions/{old.session_id}/close",
                                headers={"Origin": ORIGIN})).status_code == 409
        payment.state = "PARTIALLY_PAID"
        partial = await http.post(f"/api/admin/table-sessions/{old.session_id}/refresh-payment",
                                  headers={"Origin": ORIGIN})
        assert partial.json()["payment_state"] == "PARTIALLY_PAID"
        assert (await http.post(f"/api/admin/table-sessions/{old.session_id}/close",
                                headers={"Origin": ORIGIN})).status_code == 409
        payment.state = "PAID"
        refreshed = await http.post(
            f"/api/admin/table-sessions/{old.session_id}/refresh-payment",
            headers={"Origin": ORIGIN},
        )
        assert refreshed.status_code == 200 and refreshed.json()["safe_to_close"] is True
        assert refreshed.json()["payment_state"] == "PAID"
        assert (await http.post(f"/api/admin/table-sessions/{old.session_id}/close",
                                headers={"Origin": "https://wrong.example"})).status_code == 403
        closed = await http.post(f"/api/admin/table-sessions/{old.session_id}/close",
                                 headers={"Origin": ORIGIN})
        assert closed.status_code == 200 and closed.json()["status"] == "closed"
        assert closed.json()["payment_state"] == "PAID"
        closed_at = closed.json()["closed_at"]
        again = await http.post(f"/api/admin/table-sessions/{old.session_id}/close",
                                headers={"Origin": ORIGIN})
        assert again.status_code == 200 and again.json()["closed_at"] == closed_at
        assert len(payment.calls) == 6  # no Clover re-read for an already closed session
        assert (await http.get("/api/admin/table-sessions")).json()["sessions"] == []
        history = (await http.get("/api/admin/table-sessions/history?limit=1")).json()
        assert history["sessions"][0]["session_id"] == old.session_id
        assert history["sessions"][0]["clover_order_ref"] == "…1234"
        fresh = await http.post("/api/public/table-sessions", json={"table_token": token})
        assert fresh.status_code == 200
        assert fresh.json()["session_id"] != old.session_id
        assert fresh.json()["table"]["display_name"] == "Table 18"
        new_record = await repo.get(scope, fresh.json()["session_id"])
        assert new_record is not None and new_record.clover_order_id is None
        historical = await repo.get(scope, old.session_id)
        assert historical is not None and historical.status == "closed"
        assert historical.clover_order_id == "CLOVER_ORDER_SECRET_1234"
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT COUNT(*) FROM table_session_mutations WHERE session_id=?",
                              (old.session_id,)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_physical_floor_is_stable_and_session_states_follow_kitchen(
    admin_settings: Settings, repo: SQLiteTableSessionRepository,
) -> None:
    scope = "sandbox:synthetic-merchant"
    async with client() as http:
        assert (await http.get("/api/admin/tables")).status_code == 401
        assert (await login(http)).status_code == 200
        floor = (await http.get("/api/admin/tables")).json()["tables"]
        assert [row["table_id"] for row in floor] == [
            f"{column}{position}" for position in range(1, 5) for column in "ABCD"
        ]
        assert len(floor) == 16
        assert all(row["session"] is None and row["kitchen_state"] == "AVAILABLE"
                   for row in floor)
        assert "token" not in json.dumps(floor).lower()

        session = await repo.get_or_create(scope, "A3")
        floor = (await http.get("/api/admin/tables")).json()["tables"]
        a3 = next(row for row in floor if row["table_id"] == "A3")
        assert a3["session"]["session_id"] == session.session_id
        assert a3["kitchen_state"] == "AVAILABLE"  # guest has not ordered

        with sqlite3.connect(repo.path) as db:
            db.execute("UPDATE table_sessions SET clover_order_id=? WHERE session_id=?",
                       ("ORDER_A3", session.session_id))
            db.execute("INSERT INTO table_session_mutations "
                       "(scope,session_id,key_hash,fingerprint,state,response_json) "
                       "VALUES (?,?,?,?,?,?)", (scope, session.session_id, "key1", "fp1",
                        "succeeded", json.dumps({"order": {"id": "ORDER_A3", "currency": "USD",
                        "subtotal": 0, "tax": 0, "total": 0, "items": [
                            {"item_id": "TEA", "name": "Tea", "quantity": 4,
                             "unit_price": 0, "modifiers": []}]}})))
        floor = (await http.get("/api/admin/tables")).json()["tables"]
        a3 = next(row for row in floor if row["table_id"] == "A3")
        assert a3["kitchen_state"] == "NEW_ORDER"
        assert (a3["new_count"], a3["open_count"]) == (1, 1)
        line_id = a3["session"]["kitchen_lines"][0]["kitchen_line_id"]
        await repo.transition_kitchen_line(scope, line_id, "READY")
        a3 = next(row for row in (await http.get("/api/admin/tables")).json()["tables"]
                  if row["table_id"] == "A3")
        assert a3["kitchen_state"] == "IN_PROGRESS"
        await repo.transition_kitchen_line(scope, line_id, "DELIVERED")
        a3 = next(row for row in (await http.get("/api/admin/tables")).json()["tables"]
                  if row["table_id"] == "A3")
        assert a3["kitchen_state"] == "ALL_DELIVERED" and a3["open_count"] == 0
        assert (await http.get("/api/admin/prep-summary")).json()["items"] == []

        # An add-on returns the tile to green, while delivered quantity stays excluded.
        with sqlite3.connect(repo.path) as db:
            db.execute("INSERT INTO table_session_mutations "
                       "(scope,session_id,key_hash,fingerprint,state,response_json) "
                       "VALUES (?,?,?,?,?,?)", (scope, session.session_id, "key2", "fp2",
                        "succeeded", json.dumps({"order": {"id": "ORDER_A3", "currency": "USD",
                        "subtotal": 0, "tax": 0, "total": 0, "items": [
                            {"item_id": "TEA", "name": "Tea", "quantity": 4,
                             "unit_price": 0, "modifiers": []},
                            {"item_id": "LASSI", "name": "Lassi", "quantity": 2,
                             "unit_price": 0, "modifiers": []}]}})))
        a3 = next(row for row in (await http.get("/api/admin/tables")).json()["tables"]
                  if row["table_id"] == "A3")
        assert a3["kitchen_state"] == "NEW_ORDER"
        assert (a3["new_count"], a3["delivered_count"]) == (1, 1)
        prep = (await http.get("/api/admin/prep-summary")).json()["items"]
        assert [(item["item_id"], item["remaining_quantity"]) for item in prep] == [("LASSI", 2)]
        lassi_id = next(line["kitchen_line_id"] for line in a3["session"]["kitchen_lines"]
                        if line["item_id"] == "LASSI")
        await repo.transition_kitchen_line(scope, lassi_id, "READY")
        await repo.transition_kitchen_line(scope, lassi_id, "DELIVERED")
        snapshot = await repo.admin_session(scope, session.session_id)
        assert await repo.update_payment_state(scope, session.session_id, "ORDER_A3",
                                               "PAID", snapshot.mutation_count)
        await repo.close(scope, session.session_id)
        a3 = next(row for row in (await http.get("/api/admin/tables")).json()["tables"]
                  if row["table_id"] == "A3")
        assert a3["session"] is None and a3["kitchen_state"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_payment_refresh_failure_clears_paid_cache(
    admin_settings: Settings, repo: SQLiteTableSessionRepository,
) -> None:
    scope = "sandbox:synthetic-merchant"
    session = await repo.get_or_create(scope, "T30")
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE table_sessions SET clover_order_id=? WHERE session_id=?",
                   ("ORDER30", session.session_id))
    payment = FakePaymentClient("PAID")
    app.dependency_overrides[get_clover_client] = lambda: payment
    async with client() as http:
        assert (await login(http)).status_code == 200
        path = f"/api/admin/table-sessions/{session.session_id}/refresh-payment"
        assert (await http.post(path, headers={"Origin": ORIGIN})).json()["payment_state"] == "PAID"
        payment.state = "FAIL"
        unavailable = await http.post(path, headers={"Origin": ORIGIN})
        assert unavailable.status_code == 200
        assert unavailable.json()["payment_state"] == "UNAVAILABLE"
        assert unavailable.json()["payment_warning"] == "Clover payment could not be confirmed"
        detail = (await http.get(f"/api/admin/table-sessions/{session.session_id}")).json()
        assert detail["payment_state"] == "UNAVAILABLE" and detail["safe_to_close"] is False
        assert (await http.post(path, headers={"Origin": "https://wrong.example"})).status_code == 403


@pytest.mark.asyncio
async def test_deleted_clover_order_keeps_local_detail_and_force_close_available(
    admin_settings: Settings, repo: SQLiteTableSessionRepository,
) -> None:
    scope = "sandbox:synthetic-merchant"
    session_id, line_id = await confirmed_force_close_session(repo, "A1")
    await repo.transition_kitchen_line(scope, line_id, "READY")
    await repo.transition_kitchen_line(scope, line_id, "DELIVERED")
    payment = FakePaymentClient("NOT_FOUND")
    app.dependency_overrides[get_clover_client] = lambda: payment
    async with client() as http:
        assert (await login(http)).status_code == 200
        detail_path = f"/api/admin/table-sessions/{session_id}"
        before = await http.get(detail_path)
        assert before.status_code == 200
        assert before.json()["delivered_count"] == 1
        refreshed = await http.post(f"{detail_path}/refresh-payment",
                                    headers={"Origin": ORIGIN})
        assert refreshed.status_code == 200
        body = refreshed.json()
        assert body["payment_state"] == "UNAVAILABLE"
        assert body["payment_warning"] == "Clover order could not be found"
        assert body["safe_to_close"] is False and body["safe_to_force_close"] is True
        assert body["delivered_count"] == 1 and len(body["kitchen_lines"]) == 1
        assert body["clover_order_ref"] == "…1234"
        detail = (await http.get(detail_path)).json()
        assert detail["payment_warning"] == "Clover order could not be found"
        assert detail["safe_to_force_close"] is True
        assert (await http.post(f"{detail_path}/close",
                                headers={"Origin": ORIGIN})).status_code == 503
        calls_before_force = len(payment.calls)
        closed = await http.post(f"{detail_path}/force-close",
                                 json={"reason": "Clover order removed during testing"},
                                 headers={"Origin": ORIGIN})
        assert closed.status_code == 200
        assert len(payment.calls) == calls_before_force
        assert closed.json()["forced_close"] is True
        assert closed.json()["forced_close_payment_state"] == "UNAVAILABLE"
        assert closed.json()["clover_order_ref"] == "…1234"
        history = (await http.get("/api/admin/table-sessions/history")).json()["sessions"]
        assert history[0]["forced_close_reason"] == "Clover order removed during testing"
        assert history[0]["payment_state"] == "UNAVAILABLE"
        assert history[0]["clover_order_ref"] == "…1234"


@pytest.mark.asyncio
async def test_true_admin_storage_failure_still_surfaces_as_server_error(
    admin_settings: Settings, monkeypatch,
) -> None:
    async with client() as http:
        assert (await login(http)).status_code == 200
        async def fail_storage(self, scope: str, session_id: str):
            raise RuntimeError("synthetic storage failure")
        monkeypatch.setattr(SQLiteTableSessionRepository, "admin_session", fail_storage)
        response = await http.get(f"/api/admin/table-sessions/{'a' * 32}")
        assert response.status_code == 500
        assert response.json() == {"detail": "Internal server error"}


@pytest.mark.asyncio
async def test_prep_summary_groups_authoritative_ids_and_quantities(
    admin_settings: Settings, repo: SQLiteTableSessionRepository,
) -> None:
    scope = "sandbox:synthetic-merchant"
    for table, key, items in (
        ("T31", "key31", [("ITEM_A", "Same name", 4), ("ITEM_B", "Same name", 2)]),
        ("T32", "key32", [("ITEM_A", "Same name", 3)]),
    ):
        session = await repo.get_or_create(scope, table)
        assert (await repo.claim(scope, session.session_id, key, key))[0] == "claimed"
        await repo.applied(scope, session.session_id, key, f"ORDER_{table}",
                           requested_items=[{"item_id": item_id, "quantity": quantity,
                                             "modifier_ids": []} for item_id, _, quantity in items])
        await repo.finish(scope, session.session_id, key, {
            "order": {"id": f"ORDER_{table}", "currency": "USD", "total": 0,
                      "subtotal": 0, "tax": 0,
                      "items": [{"item_id": item_id, "name": name,
                                 "quantity": quantity, "unit_price": 1,
                                 "modifiers": []} for item_id, name, quantity in items]},
        })
    t31 = next(row for row in await repo.admin_sessions(scope, "open", -1, 0)
               if row.session.table == "T31")
    t32 = next(row for row in await repo.admin_sessions(scope, "open", -1, 0)
               if row.session.table == "T32")
    ready_id = next(line.kitchen_line_id for line in t32.kitchen_lines if line.item_id == "ITEM_A")
    delivered_id = next(line.kitchen_line_id for line in t31.kitchen_lines if line.item_id == "ITEM_B")
    await repo.transition_kitchen_line(scope, ready_id, "READY")
    async with client() as http:
        assert (await http.get("/api/admin/prep-summary")).status_code == 401
        assert (await login(http)).status_code == 200
        before = (await http.get("/api/admin/prep-summary")).json()["items"]
        assert {row["item_id"] for row in before} == {"ITEM_A", "ITEM_B"}
        await repo.transition_kitchen_line(scope, delivered_id, "READY")
        await repo.transition_kitchen_line(scope, delivered_id, "DELIVERED")
        items = (await http.get("/api/admin/prep-summary")).json()["items"]
        assert len(items) == 1
        assert items[0]["item_id"] == "ITEM_A"
        assert (items[0]["new_quantity"], items[0]["ready_quantity"],
                items[0]["remaining_quantity"]) == (4, 3, 7)
        assert {(row["table"], row["new_quantity"], row["ready_quantity"])
                for row in items[0]["tables"]} == {("T31", 4, 0), ("T32", 0, 3)}


@pytest.mark.asyncio
async def test_unsafe_mutation_blocks_close_even_if_session_marker_is_idle(
    admin_settings: Settings, repo: SQLiteTableSessionRepository,
) -> None:
    scope = "sandbox:synthetic-merchant"
    app.dependency_overrides[get_clover_client] = lambda: FakePaymentClient()
    pending = await repo.get_or_create(scope, "T19")
    with sqlite3.connect(repo.path) as db:
        db.execute(
            "INSERT INTO table_session_mutations "
            "(scope,session_id,key_hash,fingerprint,state,response_json) "
            "VALUES (?,?,?,?,?,?)",
            (scope, pending.session_id, "key-hash", "fingerprint", "uncertain", None),
        )
    async with client() as http:
        assert (await login(http)).status_code == 200
        detail = (await http.get(f"/api/admin/table-sessions/{pending.session_id}")).json()
        assert detail["reconciliation_pending"] is True
        assert detail["safe_to_close"] is False
        close = await http.post(f"/api/admin/table-sessions/{pending.session_id}/close",
                                headers={"Origin": ORIGIN})
        assert close.status_code == 409
        assert (await repo.get(scope, pending.session_id)).status == "open"


async def confirmed_force_close_session(
    repo: SQLiteTableSessionRepository, table: str,
) -> tuple[str, str]:
    scope = "sandbox:synthetic-merchant"
    session = await repo.get_or_create(scope, table)
    key = f"synthetic-{table}"
    assert (await repo.claim(scope, session.session_id, key, key))[0] == "claimed"
    await repo.applied(scope, session.session_id, key, "SYNTHETIC_ORDER_1234",
                       requested_items=[{"item_id": "TEA", "quantity": 1,
                                         "modifier_ids": []}])
    await repo.finish(scope, session.session_id, key, {
        "session_id": session.session_id, "table": table, "status": "open",
        "order": {"id": "SYNTHETIC_ORDER_1234", "currency": "USD",
                  "subtotal": 4.99, "tax": 0, "total": 4.99,
                  "items": [{"item_id": "TEA", "name": "Tea", "quantity": 1,
                             "unit_price": 4.99, "modifiers": []}]},
    })
    snapshot = await repo.admin_session(scope, session.session_id)
    return session.session_id, snapshot.kitchen_lines[0].kitchen_line_id


@pytest.mark.asyncio
async def test_force_close_requires_delivery_and_records_audit_without_clover(
    admin_settings: Settings, repo: SQLiteTableSessionRepository,
) -> None:
    scope = "sandbox:synthetic-merchant"
    session_id, line_id = await confirmed_force_close_session(repo, "A1")
    url = f"/api/admin/table-sessions/{session_id}/force-close"
    async with client() as http:
        assert (await http.post(url, json={"reason": "Payment handled manually"},
                                headers={"Origin": ORIGIN})).status_code == 401
        assert (await login(http)).status_code == 200
        assert (await http.post(url, json={"reason": "Payment handled manually"},
                                headers={"Origin": "https://wrong.example"})).status_code == 403
        assert (await http.post(url, json={"reason": "short"},
                                headers={"Origin": ORIGIN})).status_code == 422
        assert (await http.post(url, json={"reason": "Payment handled manually"},
                                headers={"Origin": ORIGIN})).status_code == 409
        await repo.transition_kitchen_line(scope, line_id, "READY")
        assert (await http.post(url, json={"reason": "Payment handled manually"},
                                headers={"Origin": ORIGIN})).status_code == 409
        await repo.transition_kitchen_line(scope, line_id, "DELIVERED")
        snapshot = await repo.admin_session(scope, session_id)
        assert await repo.update_payment_state(scope, session_id, "SYNTHETIC_ORDER_1234",
                                               "OPEN", snapshot.mutation_count)
        detail = (await http.get(f"/api/admin/table-sessions/{session_id}")).json()
        assert detail["safe_to_close"] is False and detail["safe_to_force_close"] is True
        closed = await http.post(url, json={"reason": "  Payment handled manually  "},
                                 headers={"Origin": ORIGIN})
        assert closed.status_code == 200
        assert closed.json()["forced_close"] is True
        assert closed.json()["forced_close_reason"] == "Payment handled manually"
        assert closed.json()["forced_close_by"] == "operator"
        assert closed.json()["forced_close_at"]
        assert closed.json()["payment_state"] == "UNPAID"
        assert closed.json()["clover_order_ref"] == "…1234"
        assert "SYNTHETIC_ORDER_1234" not in closed.text
        with sqlite3.connect(repo.path) as db:
            audit = db.execute("SELECT forced_close,forced_close_reason,forced_close_at,"
                               "forced_close_by,forced_close_payment_state,"
                               "forced_close_clover_order_ref,forced_close_unresolved "
                               "FROM table_sessions WHERE session_id=?", (session_id,)).fetchone()
        assert audit == (1, "Payment handled manually", closed.json()["forced_close_at"],
                         "operator", "OPEN", "SYNTHETIC_ORDER_1234", 0)
        again = await http.post(url, json={"reason": "Different valid reason"},
                                headers={"Origin": ORIGIN})
        assert again.status_code == 200
        assert again.json()["forced_close_reason"] == "Payment handled manually"
        history = (await http.get("/api/admin/table-sessions/history")).json()["sessions"]
        assert history[0]["forced_close"] is True
        floor = (await http.get("/api/admin/tables")).json()["tables"]
        assert next(table for table in floor if table["table_id"] == "A1")["session"] is None
        token = TableTokenService(admin_settings).issue("A1")
        fresh = await http.post("/api/public/table-sessions", json={"table_token": token})
        assert fresh.status_code == 200 and fresh.json()["session_id"] != session_id


@pytest.mark.asyncio
async def test_force_close_unknown_payment_with_unresolved_mutation_is_audited(
    admin_settings: Settings, repo: SQLiteTableSessionRepository,
) -> None:
    scope = "sandbox:synthetic-merchant"
    session_id, line_id = await confirmed_force_close_session(repo, "A2")
    await repo.transition_kitchen_line(scope, line_id, "READY")
    await repo.transition_kitchen_line(scope, line_id, "DELIVERED")
    assert (await repo.claim(scope, session_id, "pending-key", "pending-key"))[0] == "claimed"
    async with client() as http:
        assert (await login(http)).status_code == 200
        detail = (await http.get(f"/api/admin/table-sessions/{session_id}")).json()
        assert detail["reconciliation_pending"] is True
        assert detail["safe_to_force_close"] is True
        response = await http.post(f"/api/admin/table-sessions/{session_id}/force-close",
                                   json={"reason": "Clover unavailable"},
                                   headers={"Origin": ORIGIN})
        assert response.status_code == 200
        assert response.json()["forced_close_unresolved"] is True
        assert response.json()["payment_state"] == "UNKNOWN"
        assert response.json()["forced_close_payment_state"] is None
        assert (await repo.get(scope, session_id)).status == "closed"


@pytest.mark.asyncio
async def test_history_paginates_and_admin_requires_configuration(
    admin_settings: Settings, repo: SQLiteTableSessionRepository,
) -> None:
    scope = "sandbox:synthetic-merchant"
    first = await repo.get_or_create(scope, "T21")
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE table_sessions SET status='closed',closed_at=updated_at "
                   "WHERE session_id=?", (first.session_id,))
    second = await repo.get_or_create(scope, "T22")
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE table_sessions SET status='closed',closed_at=updated_at "
                   "WHERE session_id=?", (second.session_id,))
    async with client() as http:
        assert (await login(http)).status_code == 200
        page = (await http.get("/api/admin/table-sessions/history?limit=1")).json()
        assert len(page["sessions"]) == 1 and page["next_offset"] == 1
        second_page = (await http.get("/api/admin/table-sessions/history?limit=1&offset=1")).json()
        assert len(second_page["sessions"]) == 1 and second_page["next_offset"] is None
        assert page["sessions"][0]["session_id"] != second_page["sessions"][0]["session_id"]
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    async with client() as http:
        assert (await http.get("/api/admin/table-sessions")).status_code == 503
        assert (await http.post("/api/admin/login", json={
            "username": "operator", "password": "synthetic-password",
        }, headers={"Origin": ORIGIN})).status_code == 503
