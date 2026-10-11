"""Replaceable repository backed by a development-only SQLite database."""

import asyncio
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TableSession:
    session_id: str
    table: str
    status: str
    clover_order_id: str | None
    created_at: str
    updated_at: str
    closed_at: str | None
    forced_close: bool = False
    forced_close_reason: str | None = None
    forced_close_at: str | None = None
    forced_close_by: str | None = None
    forced_close_payment_state: str | None = None
    forced_close_clover_order_ref: str | None = None
    forced_close_unresolved: bool = False


class UnsafeSessionClosure(Exception):
    """An unresolved order mutation still protects the session."""


class InvalidKitchenTransition(Exception):
    """The requested kitchen transition is not safe or not sequential."""


@dataclass(frozen=True)
class KitchenLine:
    kitchen_line_id: str
    session_id: str
    clover_order_id: str | None
    clover_line_item_id: str | None
    item_id: str | None
    item_name: str
    quantity: int
    modifiers: list[dict]
    status: str
    created_at: str
    ready_at: str | None
    delivered_at: str | None
    updated_at: str


@dataclass(frozen=True)
class AdminSessionSnapshot:
    session: TableSession
    operation_state: str
    mutation_count: int
    unresolved: bool
    latest_order: dict | None
    kitchen_lines: list[KitchenLine]
    payment_state: str | None
    payment_checked_at: str | None
    payment_warning: str | None


class TableSessionRepository(Protocol):
    async def get_or_create(self, scope: str, table: str) -> TableSession: ...
    async def get(self, scope: str, session_id: str) -> TableSession | None: ...
    async def claim(self, scope: str, session_id: str, key_hash: str,
                    fingerprint: str) -> tuple[str, TableSession | None, dict | None]: ...
    async def applied(self, scope: str, session_id: str, key_hash: str,
                      order_id: str, has_modifiers: bool = False,
                      requested_items: list[dict] | None = None) -> None: ...
    async def prepare_total_update(self, scope: str, session_id: str,
                                   key_hash: str, total_cents: int) -> None: ...
    async def confirm_total_update(self, scope: str, session_id: str,
                                   key_hash: str) -> None: ...
    async def confirm_existing_total(self, scope: str, session_id: str,
                                     key_hash: str, total_cents: int) -> None: ...
    async def finish(self, scope: str, session_id: str, key_hash: str,
                     response: dict, readback_lines: list[dict] | None = None) -> None: ...
    async def confirmation_pending(self, scope: str, session_id: str,
                                   key_hash: str) -> None: ...
    async def confirmed_unresolved(self, scope: str, session_id: str
                                   ) -> tuple[str, dict] | None: ...
    async def has_uncertain(self, scope: str, session_id: str) -> bool: ...
    async def prior_success_for_uncertain(self, scope: str, session_id: str,
                                          key_hash: str) -> dict | None: ...
    async def prior_success_for_applied(self, scope: str, session_id: str,
                                        key_hash: str) -> dict | None: ...
    async def confirm_recovered_append(self, scope: str, session_id: str,
                                       key_hash: str) -> None: ...
    async def uncertain_total_candidate(self, scope: str, session_id: str
                                        ) -> tuple[str, int] | None: ...
    async def confirm_total_from_readback(self, scope: str, session_id: str,
                                          key_hash: str, total_cents: int) -> None: ...
    async def legacy_uncertain_candidate(self, scope: str, session_id: str
                                         ) -> tuple[str, str, dict] | None: ...
    async def release(self, scope: str, session_id: str, key_hash: str) -> None: ...
    async def uncertain(self, scope: str, session_id: str, key_hash: str) -> None: ...
    async def close(self, scope: str, session_id: str) -> TableSession | None: ...
    async def force_close(self, scope: str, session_id: str, reason: str,
                          actor: str) -> TableSession | None: ...
    async def admin_sessions(self, scope: str, status: str, limit: int,
                             offset: int) -> list[AdminSessionSnapshot]: ...
    async def admin_session(self, scope: str, session_id: str) -> AdminSessionSnapshot | None: ...
    async def transition_kitchen_line(self, scope: str, kitchen_line_id: str,
                                      target: str) -> str: ...
    async def update_payment_state(self, scope: str, session_id: str,
                                   order_id: str, state: str,
                                   expected_mutation_count: int) -> bool: ...
    async def clear_payment_state(self, scope: str, session_id: str,
                                  order_id: str, warning: str | None = None) -> None: ...


class SQLiteTableSessionRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("""CREATE TABLE IF NOT EXISTS table_sessions (
            session_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            table_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('open','closed')),
            clover_order_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT,
            operation_state TEXT NOT NULL DEFAULT 'idle'
                CHECK (operation_state IN ('idle','pending','applied','uncertain'))
        )""")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_open_table_session
            ON table_sessions(scope, table_id) WHERE status='open'""")
        db.execute("""CREATE TABLE IF NOT EXISTS table_session_mutations (
            scope TEXT NOT NULL,
            session_id TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending','applied','succeeded','uncertain')),
            response_json TEXT,
            PRIMARY KEY (scope, session_id, key_hash)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS kitchen_lines (
            kitchen_line_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source_key_hash TEXT NOT NULL,
            source_line_index INTEGER NOT NULL,
            clover_order_id TEXT,
            clover_line_item_id TEXT,
            item_id TEXT,
            item_name TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            modifiers_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('NEW','READY','DELIVERED')),
            created_at TEXT NOT NULL,
            ready_at TEXT,
            delivered_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(scope, session_id, source_key_hash, source_line_index)
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS kitchen_lines_session
            ON kitchen_lines(scope, session_id, created_at)""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(table_sessions)")}
        for column, definition in (
            ("payment_state", "TEXT"), ("payment_checked_at", "TEXT"),
            ("payment_warning", "TEXT"),
            ("forced_close", "INTEGER NOT NULL DEFAULT 0"),
            ("forced_close_reason", "TEXT"), ("forced_close_at", "TEXT"),
            ("forced_close_by", "TEXT"), ("forced_close_payment_state", "TEXT"),
            ("forced_close_clover_order_ref", "TEXT"),
            ("forced_close_unresolved", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in columns:
                try:
                    db.execute(f"ALTER TABLE table_sessions ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as exc:
                    # A concurrent connection may have completed this additive migration.
                    if "duplicate column name" not in str(exc).lower():
                        raise
        self._backfill_kitchen(db)
        return db

    @staticmethod
    def _identity(line: dict) -> tuple[str | None, tuple[str, ...]]:
        return (line.get("item_id"), tuple(sorted(
            str(modifier.get("id")) for modifier in line.get("modifiers", [])
            if isinstance(modifier, dict) and modifier.get("id")
        )))

    @classmethod
    def _new_kitchen_lines(cls, db: sqlite3.Connection, scope: str, session_id: str,
                           key_hash: str, response: dict, requested: list[dict] | None,
                           previous: dict | None, closed: bool = False,
                           readback_lines: list[dict] | None = None) -> None:
        order = response.get("order") if isinstance(response, dict) else None
        if not isinstance(order, dict) or not isinstance(order.get("items"), list):
            return
        current = [line for line in order["items"] if isinstance(line, dict)]
        before = previous.get("order", {}).get("items", []) if isinstance(previous, dict) else []
        previous_quantities = {
            cls._identity(line): int(line.get("quantity", 0))
            for line in before if isinstance(line, dict)
        }
        chosen: list[tuple[dict, int]] = []
        if requested:
            for request in requested:
                required_modifiers = set(request.get("modifier_ids", []))
                match = next((line for line in current
                              if line.get("item_id") == request.get("item_id")
                              and required_modifiers.issubset(set(cls._identity(line)[1]))
                              and int(line.get("quantity", 0)) >
                              previous_quantities.get(cls._identity(line), 0)), None)
                if match is None:
                    raise RuntimeError("Confirmed kitchen line is missing from readback")
                chosen.append((match, int(request["quantity"])))
        else:
            # Existing successful sessions predate kitchen tracking. Reconstruct only
            # positive line deltas from their stored, confirmed snapshots.
            for line in current:
                identity = cls._identity(line)
                delta = int(line.get("quantity", 0)) - previous_quantities.get(identity, 0)
                if delta > 0:
                    chosen.append((line, delta))
        timestamp = now_utc()
        known_ids = {row[0] for row in db.execute(
            "SELECT clover_line_item_id FROM kitchen_lines WHERE scope=? AND session_id=? "
            "AND clover_line_item_id IS NOT NULL", (scope, session_id),
        )}
        for index, (line, quantity) in enumerate(chosen):
            kitchen_id = uuid.uuid5(uuid.NAMESPACE_URL,
                                    f"kitchen:{scope}:{session_id}:{key_hash}:{index}").hex
            candidate_ids = [entry.get("id") for entry in readback_lines or []
                             if entry.get("id") not in known_ids and
                             cls._identity(entry) == cls._identity(line)]
            clover_line_id = candidate_ids[0] if quantity == 1 and len(candidate_ids) == 1 else None
            if clover_line_id:
                known_ids.add(clover_line_id)
            db.execute(
                "INSERT OR IGNORE INTO kitchen_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kitchen_id, scope, session_id, key_hash, index, order.get("id"), clover_line_id,
                 line.get("item_id"), line.get("name") or "Item", quantity,
                 json.dumps(line.get("modifiers") or []),
                 "DELIVERED" if closed else "NEW", timestamp, None,
                 timestamp if closed else None, timestamp),
            )

    @classmethod
    def _backfill_kitchen(cls, db: sqlite3.Connection) -> None:
        rows = db.execute(
            "SELECT m.scope,m.session_id,m.key_hash,m.response_json,s.status "
            "FROM table_session_mutations m JOIN table_sessions s "
            "ON s.scope=m.scope AND s.session_id=m.session_id "
            "WHERE m.state='succeeded' ORDER BY m.rowid"
        ).fetchall()
        previous: dict[tuple[str, str], dict] = {}
        for row in rows:
            try:
                response = json.loads(row["response_json"] or "{}")
            except (TypeError, ValueError):
                continue
            key = (row["scope"], row["session_id"])
            exists = db.execute(
                "SELECT 1 FROM kitchen_lines WHERE scope=? AND session_id=? "
                "AND source_key_hash=? LIMIT 1", (*key, row["key_hash"]),
            ).fetchone()
            if exists is None:
                cls._new_kitchen_lines(db, *key, row["key_hash"], response,
                                       None, previous.get(key), row["status"] == "closed")
            previous[key] = response

    @staticmethod
    def _session(row: sqlite3.Row) -> TableSession:
        return TableSession(
            session_id=row["session_id"], table=row["table_id"], status=row["status"],
            clover_order_id=row["clover_order_id"], created_at=row["created_at"],
            updated_at=row["updated_at"], closed_at=row["closed_at"],
            forced_close=bool(row["forced_close"]),
            forced_close_reason=row["forced_close_reason"],
            forced_close_at=row["forced_close_at"], forced_close_by=row["forced_close_by"],
            forced_close_payment_state=row["forced_close_payment_state"],
            forced_close_clover_order_ref=row["forced_close_clover_order_ref"],
            forced_close_unresolved=bool(row["forced_close_unresolved"]),
        )

    def _get_or_create(self, scope: str, table: str) -> TableSession:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM table_sessions WHERE scope=? AND table_id=? AND status='open'",
                (scope, table),
            ).fetchone()
            if row is None:
                session_id, timestamp = uuid.uuid4().hex, now_utc()
                db.execute(
                    "INSERT INTO table_sessions (session_id,scope,table_id,status,created_at,updated_at) "
                    "VALUES (?,?,?,'open',?,?)",
                    (session_id, scope, table, timestamp, timestamp),
                )
                row = db.execute(
                    "SELECT * FROM table_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
            db.commit()
            return self._session(row)

    async def get_or_create(self, scope: str, table: str) -> TableSession:
        return await asyncio.to_thread(self._get_or_create, scope, table)

    def _get(self, scope: str, session_id: str) -> TableSession | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM table_sessions WHERE scope=? AND session_id=?",
                (scope, session_id),
            ).fetchone()
            return self._session(row) if row else None

    async def get(self, scope: str, session_id: str) -> TableSession | None:
        return await asyncio.to_thread(self._get, scope, session_id)

    @staticmethod
    def _admin_snapshot(db: sqlite3.Connection, row: sqlite3.Row) -> AdminSessionSnapshot:
        mutations = db.execute(
            "SELECT state,response_json FROM table_session_mutations "
            "WHERE scope=? AND session_id=? ORDER BY rowid",
            (row["scope"], row["session_id"]),
        ).fetchall()
        latest_order = None
        for mutation in mutations:
            if mutation["state"] == "succeeded" and mutation["response_json"]:
                try:
                    payload = json.loads(mutation["response_json"])
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("order"), dict):
                    latest_order = payload["order"]
        kitchen_rows = db.execute(
            "SELECT * FROM kitchen_lines WHERE scope=? AND session_id=? "
            "ORDER BY created_at,kitchen_line_id", (row["scope"], row["session_id"]),
        ).fetchall()
        kitchen_lines = [KitchenLine(
            kitchen_line_id=line["kitchen_line_id"], session_id=line["session_id"],
            clover_order_id=line["clover_order_id"],
            clover_line_item_id=line["clover_line_item_id"], item_id=line["item_id"],
            item_name=line["item_name"], quantity=line["quantity"],
            modifiers=json.loads(line["modifiers_json"]), status=line["status"],
            created_at=line["created_at"], ready_at=line["ready_at"],
            delivered_at=line["delivered_at"], updated_at=line["updated_at"],
        ) for line in kitchen_rows]
        return AdminSessionSnapshot(
            session=SQLiteTableSessionRepository._session(row),
            operation_state=row["operation_state"],
            mutation_count=len(mutations),
            unresolved=row["operation_state"] != "idle" or any(
                mutation["state"] != "succeeded" for mutation in mutations
            ),
            latest_order=latest_order,
            kitchen_lines=kitchen_lines,
            payment_state=row["payment_state"],
            payment_checked_at=row["payment_checked_at"],
            payment_warning=row["payment_warning"],
        )

    def _admin_sessions(self, scope: str, status: str, limit: int,
                        offset: int) -> list[AdminSessionSnapshot]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM table_sessions WHERE scope=? AND status=? "
                "ORDER BY created_at DESC,session_id DESC",
                (scope, status),
            ).fetchall()
            snapshots = [self._admin_snapshot(db, row) for row in rows]
            if status == "open":
                def priority(snapshot: AdminSessionSnapshot) -> tuple:
                    lines = snapshot.kitchen_lines
                    statuses = {line.status for line in lines}
                    rank = (0 if "NEW" in statuses else
                            2 if statuses == {"DELIVERED"} else
                            3 if not statuses else 1)
                    outstanding = [line.created_at for line in lines if line.status != "DELIVERED"]
                    return rank, min(outstanding) if outstanding else snapshot.session.updated_at, snapshot.session.session_id
                snapshots.sort(key=priority)
            return snapshots[offset:] if limit < 0 else snapshots[offset:offset + limit]

    async def admin_sessions(self, scope: str, status: str, limit: int,
                             offset: int) -> list[AdminSessionSnapshot]:
        return await asyncio.to_thread(self._admin_sessions, scope, status, limit, offset)

    def _admin_session(self, scope: str, session_id: str) -> AdminSessionSnapshot | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM table_sessions WHERE scope=? AND session_id=?",
                (scope, session_id),
            ).fetchone()
            return self._admin_snapshot(db, row) if row else None

    async def admin_session(self, scope: str, session_id: str) -> AdminSessionSnapshot | None:
        return await asyncio.to_thread(self._admin_session, scope, session_id)

    def _claim(self, scope: str, session_id: str, key_hash: str,
               fingerprint: str) -> tuple[str, TableSession | None, dict | None]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM table_sessions WHERE scope=? AND session_id=?",
                (scope, session_id),
            ).fetchone()
            if row is None:
                db.commit()
                return "missing", None, None
            session = self._session(row)
            mutation = db.execute(
                "SELECT fingerprint,state,response_json FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND key_hash=?",
                (scope, session_id, key_hash),
            ).fetchone()
            if mutation:
                db.commit()
                if mutation["fingerprint"] != fingerprint:
                    return "conflict", session, None
                cached = json.loads(mutation["response_json"]) if mutation["response_json"] else None
                if session.status != "open" and mutation["state"] != "succeeded":
                    return "closed", session, None
                if mutation["state"] == "applied" and isinstance(cached, dict) and cached.get("phase") == "readback_pending":
                    return "reconciliation_required", session, cached
                return mutation["state"], session, cached
            if session.status != "open":
                db.commit()
                return "closed", session, None
            if row["operation_state"] != "idle":
                db.commit()
                return "busy", session, None
            db.execute(
                "INSERT INTO table_session_mutations VALUES (?,?,?,?,'pending',NULL)",
                (scope, session_id, key_hash, fingerprint),
            )
            db.execute(
                "UPDATE table_sessions SET operation_state='pending' "
                "WHERE scope=? AND session_id=?",
                (scope, session_id),
            )
            db.commit()
            return "claimed", session, None

    async def claim(self, scope: str, session_id: str, key_hash: str,
                    fingerprint: str) -> tuple[str, TableSession | None, dict | None]:
        return await asyncio.to_thread(self._claim, scope, session_id, key_hash, fingerprint)

    def _confirmed_unresolved(self, scope: str, session_id: str
                              ) -> tuple[str, dict] | None:
        """Oldest confirmed mutation safe to resume without replaying its append."""
        with closing(self._connect()) as db:
            session = db.execute(
                "SELECT clover_order_id,operation_state FROM table_sessions "
                "WHERE scope=? AND session_id=? AND status='open'",
                (scope, session_id),
            ).fetchone()
            if (session is None or session["operation_state"] not in ("applied", "uncertain") or
                    not session["clover_order_id"]):
                return None
            rows = db.execute(
                "SELECT key_hash,response_json FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND state='applied' ORDER BY rowid",
                (scope, session_id),
            ).fetchall()
            for row in rows:
                metadata = json.loads(row["response_json"] or "{}")
                phase = metadata.get("phase")
                if phase == "readback_pending":
                    phase = metadata.get("resume_phase")
                if phase not in ("atomic_created", "append_applied", "total_confirmed"):
                    continue
                if phase == "total_confirmed" and type(metadata.get("target_total_cents")) is not int:
                    continue
                return row["key_hash"], metadata
            return None

    async def confirmed_unresolved(self, scope: str, session_id: str
                                   ) -> tuple[str, dict] | None:
        return await asyncio.to_thread(self._confirmed_unresolved, scope, session_id)

    def _has_uncertain(self, scope: str, session_id: str) -> bool:
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT 1 FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND state='uncertain' LIMIT 1",
                (scope, session_id),
            ).fetchone() is not None

    async def has_uncertain(self, scope: str, session_id: str) -> bool:
        return await asyncio.to_thread(self._has_uncertain, scope, session_id)

    def _prior_success_for_uncertain(self, scope: str, session_id: str,
                                     key_hash: str) -> dict | None:
        with closing(self._connect()) as db:
            current = db.execute(
                "SELECT rowid,response_json FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND key_hash=? AND state='uncertain'",
                (scope, session_id, key_hash),
            ).fetchone()
            session = db.execute(
                "SELECT clover_order_id,operation_state FROM table_sessions "
                "WHERE scope=? AND session_id=? AND status='open'",
                (scope, session_id),
            ).fetchone()
            if (current is None or current["response_json"] is not None or
                    session is None or session["operation_state"] != "uncertain" or
                    not session["clover_order_id"]):
                return None
            others = db.execute(
                "SELECT 1 FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND key_hash<>? "
                "AND state IN ('pending','applied','uncertain') LIMIT 1",
                (scope, session_id, key_hash),
            ).fetchone()
            if others is not None:
                return None
            prior = db.execute(
                "SELECT response_json FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND rowid<? AND state='succeeded' "
                "ORDER BY rowid DESC LIMIT 1",
                (scope, session_id, current["rowid"]),
            ).fetchone()
            if prior is None or not prior["response_json"]:
                return None
            response = json.loads(prior["response_json"])
            return response if isinstance(response, dict) else None

    async def prior_success_for_uncertain(self, scope: str, session_id: str,
                                          key_hash: str) -> dict | None:
        return await asyncio.to_thread(
            self._prior_success_for_uncertain, scope, session_id, key_hash,
        )

    def _prior_success_for_applied(self, scope: str, session_id: str,
                                   key_hash: str) -> dict | None:
        with closing(self._connect()) as db:
            current = db.execute(
                "SELECT rowid FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND key_hash=? AND state='applied'",
                (scope, session_id, key_hash),
            ).fetchone()
            if current is None:
                return None
            prior = db.execute(
                "SELECT response_json FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND rowid<? AND state='succeeded' "
                "ORDER BY rowid DESC LIMIT 1",
                (scope, session_id, current["rowid"]),
            ).fetchone()
            if prior is None or not prior["response_json"]:
                return None
            response = json.loads(prior["response_json"])
            return response if isinstance(response, dict) else None

    async def prior_success_for_applied(self, scope: str, session_id: str,
                                        key_hash: str) -> dict | None:
        return await asyncio.to_thread(
            self._prior_success_for_applied, scope, session_id, key_hash,
        )

    def _legacy_uncertain_candidate(self, scope: str, session_id: str
                                    ) -> tuple[str, str, dict] | None:
        with closing(self._connect()) as db:
            session = db.execute(
                "SELECT clover_order_id,operation_state FROM table_sessions "
                "WHERE scope=? AND session_id=? AND status='open'",
                (scope, session_id),
            ).fetchone()
            if (session is None or not session["clover_order_id"] or
                    session["operation_state"] != "uncertain"):
                return None
            unresolved = db.execute(
                "SELECT rowid,key_hash,fingerprint,state,response_json "
                "FROM table_session_mutations WHERE scope=? AND session_id=? "
                "AND state IN ('pending','applied','uncertain') ORDER BY rowid",
                (scope, session_id),
            ).fetchall()
            if (len(unresolved) != 1 or unresolved[0]["state"] != "uncertain" or
                    unresolved[0]["response_json"] is not None):
                return None
            row = unresolved[0]
            prior = db.execute(
                "SELECT response_json FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND rowid<? AND state='succeeded' "
                "ORDER BY rowid DESC LIMIT 1",
                (scope, session_id, row["rowid"]),
            ).fetchone()
            if prior is None or not prior["response_json"]:
                return None
            response = json.loads(prior["response_json"])
            if not isinstance(response, dict):
                return None
            return row["key_hash"], row["fingerprint"], response

    async def legacy_uncertain_candidate(self, scope: str, session_id: str
                                         ) -> tuple[str, str, dict] | None:
        return await asyncio.to_thread(self._legacy_uncertain_candidate, scope, session_id)

    def _confirm_recovered_append(self, scope: str, session_id: str,
                                  key_hash: str) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT s.clover_order_id,s.operation_state,m.response_json "
                "FROM table_session_mutations m JOIN table_sessions s "
                "ON s.scope=m.scope AND s.session_id=m.session_id "
                "WHERE m.scope=? AND m.session_id=? AND m.key_hash=? "
                "AND m.state='uncertain' AND s.status='open'",
                (scope, session_id, key_hash),
            ).fetchone()
            if (row is None or row["operation_state"] != "uncertain" or
                    not row["clover_order_id"] or row["response_json"] is not None):
                raise RuntimeError("Uncertain append state changed")
            db.execute(
                "UPDATE table_session_mutations SET state='applied',response_json=? "
                "WHERE scope=? AND session_id=? AND key_hash=? AND state='uncertain'",
                (json.dumps({"phase": "append_applied", "recovered_by_get": True}),
                 scope, session_id, key_hash),
            )
            db.execute(
                "UPDATE table_sessions SET operation_state='applied' "
                "WHERE scope=? AND session_id=? AND operation_state='uncertain'",
                (scope, session_id),
            )
            db.commit()

    async def confirm_recovered_append(self, scope: str, session_id: str,
                                       key_hash: str) -> None:
        await asyncio.to_thread(
            self._confirm_recovered_append, scope, session_id, key_hash,
        )

    def _uncertain_total_candidate(self, scope: str, session_id: str
                                   ) -> tuple[str, int] | None:
        with closing(self._connect()) as db:
            session = db.execute(
                "SELECT clover_order_id,operation_state FROM table_sessions "
                "WHERE scope=? AND session_id=? AND status='open'",
                (scope, session_id),
            ).fetchone()
            if (session is None or not session["clover_order_id"] or
                    session["operation_state"] != "uncertain"):
                return None
            rows = db.execute(
                "SELECT key_hash,response_json FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND state='uncertain' ORDER BY rowid",
                (scope, session_id),
            ).fetchall()
            for row in rows:
                metadata = json.loads(row["response_json"] or "{}")
                if (isinstance(metadata, dict) and metadata.get("phase") == "total_pending" and
                        type(metadata.get("target_total_cents")) is int):
                    return row["key_hash"], metadata["target_total_cents"]
            return None

    async def uncertain_total_candidate(self, scope: str, session_id: str
                                        ) -> tuple[str, int] | None:
        return await asyncio.to_thread(self._uncertain_total_candidate, scope, session_id)

    def _confirm_total_from_readback(self, scope: str, session_id: str,
                                     key_hash: str, total_cents: int) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT response_json FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND key_hash=? AND state='uncertain'",
                (scope, session_id, key_hash),
            ).fetchone()
            if row is None:
                raise RuntimeError("Uncertain total state changed")
            metadata = json.loads(row["response_json"] or "{}")
            if (metadata.get("phase") != "total_pending" or
                    metadata.get("target_total_cents") != total_cents):
                raise RuntimeError("Uncertain total target changed")
            metadata["phase"] = "total_confirmed"
            db.execute(
                "UPDATE table_session_mutations SET state='applied',response_json=? "
                "WHERE scope=? AND session_id=? AND key_hash=? AND state='uncertain'",
                (json.dumps(metadata), scope, session_id, key_hash),
            )
            unresolved = db.execute(
                "SELECT state FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND state IN ('uncertain','pending','applied')",
                (scope, session_id),
            ).fetchall()
            states = {entry["state"] for entry in unresolved}
            operation = next(
                (candidate for candidate in ("uncertain", "pending", "applied")
                 if candidate in states), "idle",
            )
            db.execute(
                "UPDATE table_sessions SET operation_state=? WHERE scope=? AND session_id=?",
                (operation, scope, session_id),
            )
            db.commit()

    async def confirm_total_from_readback(self, scope: str, session_id: str,
                                          key_hash: str, total_cents: int) -> None:
        await asyncio.to_thread(
            self._confirm_total_from_readback, scope, session_id, key_hash, total_cents,
        )

    def _applied(self, scope: str, session_id: str, key_hash: str,
                 order_id: str, has_modifiers: bool,
                 requested_items: list[dict] | None) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT clover_order_id,operation_state FROM table_sessions "
                "WHERE scope=? AND session_id=? AND status='open'",
                (scope, session_id),
            ).fetchone()
            if row is None or row["operation_state"] != "pending" or (
                row["clover_order_id"] is not None and row["clover_order_id"] != order_id
            ):
                raise RuntimeError("Table session mutation state changed")
            needs_total_update = row["clover_order_id"] is not None
            metadata = json.dumps({
                "phase": "append_applied" if needs_total_update else "atomic_created",
                "has_modifiers": has_modifiers,
                "requested_items": requested_items,
            })
            updated = db.execute(
                "UPDATE table_session_mutations SET state='applied',response_json=? "
                "WHERE scope=? AND session_id=? AND key_hash=? AND state='pending'",
                (metadata, scope, session_id, key_hash),
            ).rowcount
            if updated != 1:
                raise RuntimeError("Table session mutation claim missing")
            db.execute(
                "UPDATE table_sessions SET clover_order_id=?,operation_state='applied',updated_at=? "
                "WHERE scope=? AND session_id=?",
                (order_id, now_utc(), scope, session_id),
            )
            db.commit()

    async def applied(self, scope: str, session_id: str, key_hash: str,
                      order_id: str, has_modifiers: bool = False,
                      requested_items: list[dict] | None = None) -> None:
        await asyncio.to_thread(self._applied, scope, session_id, key_hash,
                                order_id, has_modifiers, requested_items)

    def _transition_total_phase(
        self, scope: str, session_id: str, key_hash: str,
        expected_phase: str, next_phase: str, total_cents: int | None = None,
    ) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT response_json FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND key_hash=? AND state='applied'",
                (scope, session_id, key_hash),
            ).fetchone()
            if row is None:
                raise RuntimeError("Table session mutation is not applied")
            metadata = json.loads(row["response_json"] or "{}")
            if metadata.get("phase") != expected_phase:
                raise RuntimeError("Table session total phase changed")
            if total_cents is not None:
                metadata["target_total_cents"] = total_cents
            metadata["phase"] = next_phase
            db.execute(
                "UPDATE table_session_mutations SET response_json=? "
                "WHERE scope=? AND session_id=? AND key_hash=? AND state='applied'",
                (json.dumps(metadata), scope, session_id, key_hash),
            )
            db.commit()

    async def prepare_total_update(self, scope: str, session_id: str,
                                   key_hash: str, total_cents: int) -> None:
        await asyncio.to_thread(
            self._transition_total_phase, scope, session_id, key_hash,
            "append_applied", "total_pending", total_cents,
        )

    async def confirm_total_update(self, scope: str, session_id: str,
                                   key_hash: str) -> None:
        await asyncio.to_thread(
            self._transition_total_phase, scope, session_id, key_hash,
            "total_pending", "total_confirmed",
        )

    async def confirm_existing_total(self, scope: str, session_id: str,
                                     key_hash: str, total_cents: int) -> None:
        await asyncio.to_thread(
            self._transition_total_phase, scope, session_id, key_hash,
            "append_applied", "total_confirmed", total_cents,
        )

    def _finish(self, scope: str, session_id: str, key_hash: str, response: dict,
                readback_lines: list[dict] | None) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            mutation = db.execute(
                "SELECT response_json FROM table_session_mutations WHERE scope=? "
                "AND session_id=? AND key_hash=? AND state='applied'",
                (scope, session_id, key_hash),
            ).fetchone()
            if mutation is None:
                raise RuntimeError("Table session mutation is not applied")
            metadata = json.loads(mutation["response_json"] or "{}")
            previous_row = db.execute(
                "SELECT response_json FROM table_session_mutations WHERE scope=? "
                "AND session_id=? AND state='succeeded' ORDER BY rowid DESC LIMIT 1",
                (scope, session_id),
            ).fetchone()
            previous = json.loads(previous_row["response_json"]) if previous_row else None
            self._new_kitchen_lines(db, scope, session_id, key_hash, response,
                                    metadata.get("requested_items"), previous,
                                    readback_lines=readback_lines)
            updated = db.execute(
                "UPDATE table_session_mutations SET state='succeeded',response_json=? "
                "WHERE scope=? AND session_id=? AND key_hash=? AND state='applied'",
                (json.dumps(response), scope, session_id, key_hash),
            ).rowcount
            if updated != 1:
                raise RuntimeError("Table session mutation is not applied")
            unresolved = db.execute(
                "SELECT state FROM table_session_mutations "
                "WHERE scope=? AND session_id=? AND state IN ('uncertain','pending','applied')",
                (scope, session_id),
            ).fetchall()
            states = {row["state"] for row in unresolved}
            next_operation = next(
                (candidate for candidate in ("uncertain", "pending", "applied")
                 if candidate in states), "idle",
            )
            cleared = db.execute(
                "UPDATE table_sessions SET operation_state=?,payment_state=NULL,payment_checked_at=NULL,"
                "payment_warning=NULL "
                "WHERE scope=? AND session_id=? AND operation_state IN ('applied','uncertain')",
                (next_operation, scope, session_id),
            ).rowcount
            if cleared != 1:
                raise RuntimeError("Table session operation was not applied")
            db.commit()

    async def finish(self, scope: str, session_id: str, key_hash: str,
                     response: dict, readback_lines: list[dict] | None = None) -> None:
        await asyncio.to_thread(self._finish, scope, session_id, key_hash,
                                response, readback_lines)

    def _confirmation_pending(self, scope: str, session_id: str, key_hash: str) -> None:
        """Durably mark a confirmed mutation whose final GET is still pending.

        Keep the session order ID and applied operation lock; no POST may be
        replayed while this same key resolves the readback.
        """
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT m.response_json,s.clover_order_id,s.operation_state "
                "FROM table_session_mutations m JOIN table_sessions s "
                "ON s.scope=m.scope AND s.session_id=m.session_id "
                "WHERE m.scope=? AND m.session_id=? AND m.key_hash=? AND m.state='applied'",
                (scope, session_id, key_hash),
            ).fetchone()
            if (row is None or row["operation_state"] not in ("applied", "uncertain") or
                    not row["clover_order_id"]):
                raise RuntimeError("Confirmed order is unavailable for reconciliation")
            metadata = json.loads(row["response_json"] or "{}")
            if metadata.get("phase") != "readback_pending":
                if metadata.get("phase") not in ("atomic_created", "total_confirmed"):
                    raise RuntimeError("Mutation phase cannot enter readback reconciliation")
                metadata["resume_phase"] = metadata["phase"]
                metadata["phase"] = "readback_pending"
                db.execute(
                    "UPDATE table_session_mutations SET response_json=? "
                    "WHERE scope=? AND session_id=? AND key_hash=? AND state='applied'",
                    (json.dumps(metadata), scope, session_id, key_hash),
                )
            db.commit()

    async def confirmation_pending(self, scope: str, session_id: str,
                                   key_hash: str) -> None:
        await asyncio.to_thread(self._confirmation_pending, scope, session_id, key_hash)

    def _release(self, scope: str, session_id: str, key_hash: str) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute(
                "DELETE FROM table_session_mutations WHERE scope=? AND session_id=? "
                "AND key_hash=? AND state='pending'",
                (scope, session_id, key_hash),
            ).rowcount
            if deleted == 1:
                db.execute(
                    "UPDATE table_sessions SET operation_state='idle' "
                    "WHERE scope=? AND session_id=? AND operation_state='pending'",
                    (scope, session_id),
                )
            db.commit()

    async def release(self, scope: str, session_id: str, key_hash: str) -> None:
        await asyncio.to_thread(self._release, scope, session_id, key_hash)

    def _uncertain(self, scope: str, session_id: str, key_hash: str) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                "UPDATE table_session_mutations SET state='uncertain' "
                "WHERE scope=? AND session_id=? AND key_hash=? "
                "AND state IN ('pending','applied')",
                (scope, session_id, key_hash),
            ).rowcount
            if updated != 1:
                raise RuntimeError("Table session mutation is not pending")
            db.execute(
                "UPDATE table_sessions SET operation_state='uncertain' "
                "WHERE scope=? AND session_id=? "
                "AND operation_state IN ('pending','applied')",
                (scope, session_id),
            )
            db.commit()

    async def uncertain(self, scope: str, session_id: str, key_hash: str) -> None:
        await asyncio.to_thread(self._uncertain, scope, session_id, key_hash)

    def _close(self, scope: str, session_id: str) -> TableSession | None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM table_sessions WHERE scope=? AND session_id=?",
                (scope, session_id),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            if row["status"] == "open":
                unresolved = db.execute(
                    "SELECT 1 FROM table_session_mutations "
                    "WHERE scope=? AND session_id=? AND state!='succeeded' LIMIT 1",
                    (scope, session_id),
                ).fetchone()
                if row["operation_state"] != "idle" or unresolved is not None:
                    raise UnsafeSessionClosure("Cannot close a session with an unresolved mutation")
                undelivered = db.execute(
                    "SELECT 1 FROM kitchen_lines WHERE scope=? AND session_id=? "
                    "AND status!='DELIVERED' LIMIT 1", (scope, session_id),
                ).fetchone()
                if undelivered is not None:
                    raise UnsafeSessionClosure("Cannot close a session with undelivered kitchen items")
                checked_at = row["payment_checked_at"]
                if (row["payment_state"] != "PAID" or not checked_at or
                        datetime.fromisoformat(checked_at) <
                        datetime.now(timezone.utc) - timedelta(seconds=30)):
                    raise UnsafeSessionClosure("Cannot close until Clover confirms payment")
                timestamp = now_utc()
                db.execute(
                    "UPDATE table_sessions SET status='closed',closed_at=?,updated_at=?,forced_close=0 "
                    "WHERE scope=? AND session_id=?",
                    (timestamp, timestamp, scope, session_id),
                )
                row = db.execute(
                    "SELECT * FROM table_sessions WHERE scope=? AND session_id=?",
                    (scope, session_id),
                ).fetchone()
            db.commit()
            return self._session(row)

    async def close(self, scope: str, session_id: str) -> TableSession | None:
        return await asyncio.to_thread(self._close, scope, session_id)

    def _force_close(self, scope: str, session_id: str, reason: str,
                     actor: str) -> TableSession | None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM table_sessions WHERE scope=? AND session_id=?",
                (scope, session_id),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            if row["status"] == "open":
                counts = db.execute(
                    "SELECT COUNT(*) AS total, "
                    "SUM(CASE WHEN status!='DELIVERED' THEN 1 ELSE 0 END) AS open_count "
                    "FROM kitchen_lines WHERE scope=? AND session_id=?",
                    (scope, session_id),
                ).fetchone()
                if not counts["total"] or counts["open_count"]:
                    raise UnsafeSessionClosure("Cannot force close with undelivered kitchen items")
                unresolved = row["operation_state"] != "idle" or db.execute(
                    "SELECT 1 FROM table_session_mutations "
                    "WHERE scope=? AND session_id=? AND state!='succeeded' LIMIT 1",
                    (scope, session_id),
                ).fetchone() is not None
                timestamp = now_utc()
                db.execute(
                    "UPDATE table_sessions SET status='closed',closed_at=?,updated_at=?, "
                    "forced_close=1,forced_close_reason=?,forced_close_at=?,forced_close_by=?, "
                    "forced_close_payment_state=?,forced_close_clover_order_ref=?, "
                    "forced_close_unresolved=? WHERE scope=? AND session_id=?",
                    (timestamp, timestamp, reason, timestamp, actor,
                     row["payment_state"] or ("UNAVAILABLE" if row["payment_warning"] else None),
                     row["clover_order_id"], int(unresolved),
                     scope, session_id),
                )
                row = db.execute(
                    "SELECT * FROM table_sessions WHERE scope=? AND session_id=?",
                    (scope, session_id),
                ).fetchone()
            db.commit()
            return self._session(row)

    async def force_close(self, scope: str, session_id: str, reason: str,
                          actor: str) -> TableSession | None:
        return await asyncio.to_thread(self._force_close, scope, session_id, reason, actor)

    def _transition_kitchen_line(self, scope: str, kitchen_line_id: str,
                                 target: str) -> str:
        expected = {"READY": "NEW", "DELIVERED": "READY"}.get(target)
        if expected is None:
            raise InvalidKitchenTransition("Invalid kitchen status")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT k.status,k.session_id,s.status AS session_status,s.operation_state "
                "FROM kitchen_lines k JOIN table_sessions s "
                "ON s.scope=k.scope AND s.session_id=k.session_id "
                "WHERE k.scope=? AND k.kitchen_line_id=?", (scope, kitchen_line_id),
            ).fetchone()
            if row is None:
                raise InvalidKitchenTransition("Kitchen item not found")
            if row["session_status"] != "open":
                raise InvalidKitchenTransition("Closed table items cannot be changed")
            if row["operation_state"] != "idle" or db.execute(
                "SELECT 1 FROM table_session_mutations WHERE scope=? AND session_id=? "
                "AND state!='succeeded' LIMIT 1", (scope, row["session_id"]),
            ).fetchone() is not None:
                raise InvalidKitchenTransition("Order sync needs review before kitchen updates")
            if row["status"] != expected:
                raise InvalidKitchenTransition("Kitchen items must advance one step at a time")
            timestamp = now_utc()
            column = "ready_at" if target == "READY" else "delivered_at"
            db.execute(
                f"UPDATE kitchen_lines SET status=?,updated_at=?,{column}=? "
                "WHERE scope=? AND kitchen_line_id=? AND status=?",
                (target, timestamp, timestamp, scope, kitchen_line_id, expected),
            )
            db.execute(
                "UPDATE table_sessions SET updated_at=? WHERE scope=? AND session_id=?",
                (timestamp, scope, row["session_id"]),
            )
            db.commit()
            return row["session_id"]

    async def transition_kitchen_line(self, scope: str, kitchen_line_id: str,
                                      target: str) -> str:
        return await asyncio.to_thread(self._transition_kitchen_line,
                                       scope, kitchen_line_id, target)

    def _update_payment_state(self, scope: str, session_id: str,
                              order_id: str, state: str,
                              expected_mutation_count: int) -> bool:
        if state not in {"OPEN", "PARTIALLY_PAID", "PAID", "REFUNDED",
                         "CREDITED", "PARTIALLY_REFUNDED"}:
            raise ValueError("Invalid payment state")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                "UPDATE table_sessions SET payment_state=?,payment_checked_at=?,payment_warning=NULL "
                "WHERE scope=? AND session_id=? AND clover_order_id=? AND status='open' "
                "AND operation_state='idle' AND NOT EXISTS ("
                "SELECT 1 FROM table_session_mutations m WHERE m.scope=table_sessions.scope "
                "AND m.session_id=table_sessions.session_id AND m.state!='succeeded') "
                "AND (SELECT COUNT(*) FROM table_session_mutations m "
                "WHERE m.scope=table_sessions.scope AND m.session_id=table_sessions.session_id)=?",
                (state, now_utc(), scope, session_id, order_id, expected_mutation_count),
            ).rowcount
            db.commit()
            return updated == 1

    async def update_payment_state(self, scope: str, session_id: str,
                                   order_id: str, state: str,
                                   expected_mutation_count: int) -> bool:
        return await asyncio.to_thread(self._update_payment_state,
                                       scope, session_id, order_id, state,
                                       expected_mutation_count)

    def _clear_payment_state(self, scope: str, session_id: str,
                             order_id: str, warning: str | None = None) -> None:
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE table_sessions SET payment_state=NULL,payment_checked_at=NULL,"
                "payment_warning=? "
                "WHERE scope=? AND session_id=? AND clover_order_id=? AND status='open'",
                (warning, scope, session_id, order_id),
            )

    async def clear_payment_state(self, scope: str, session_id: str,
                                  order_id: str, warning: str | None = None) -> None:
        await asyncio.to_thread(self._clear_payment_state, scope, session_id, order_id, warning)
