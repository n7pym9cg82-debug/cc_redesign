"""Replaceable repository backed by a development-only SQLite database."""

import asyncio
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
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


class TableSessionRepository(Protocol):
    async def get_or_create(self, scope: str, table: str) -> TableSession: ...
    async def get(self, scope: str, session_id: str) -> TableSession | None: ...
    async def claim(self, scope: str, session_id: str, key_hash: str,
                    fingerprint: str) -> tuple[str, TableSession | None, dict | None]: ...
    async def applied(self, scope: str, session_id: str, key_hash: str,
                      order_id: str, has_modifiers: bool = False) -> None: ...
    async def prepare_total_update(self, scope: str, session_id: str,
                                   key_hash: str, total_cents: int) -> None: ...
    async def confirm_total_update(self, scope: str, session_id: str,
                                   key_hash: str) -> None: ...
    async def confirm_existing_total(self, scope: str, session_id: str,
                                     key_hash: str, total_cents: int) -> None: ...
    async def finish(self, scope: str, session_id: str, key_hash: str,
                     response: dict) -> None: ...
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
        return db

    @staticmethod
    def _session(row: sqlite3.Row) -> TableSession:
        return TableSession(
            session_id=row["session_id"], table=row["table_id"], status=row["status"],
            clover_order_id=row["clover_order_id"], created_at=row["created_at"],
            updated_at=row["updated_at"], closed_at=row["closed_at"],
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
                 order_id: str, has_modifiers: bool) -> None:
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
                      order_id: str, has_modifiers: bool = False) -> None:
        await asyncio.to_thread(self._applied, scope, session_id, key_hash,
                                order_id, has_modifiers)

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

    def _finish(self, scope: str, session_id: str, key_hash: str, response: dict) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
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
                "UPDATE table_sessions SET operation_state=? "
                "WHERE scope=? AND session_id=? AND operation_state IN ('applied','uncertain')",
                (next_operation, scope, session_id),
            ).rowcount
            if cleared != 1:
                raise RuntimeError("Table session operation was not applied")
            db.commit()

    async def finish(self, scope: str, session_id: str, key_hash: str,
                     response: dict) -> None:
        await asyncio.to_thread(self._finish, scope, session_id, key_hash, response)

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
                if row["operation_state"] != "idle":
                    raise RuntimeError("Cannot close a session with an unresolved mutation")
                timestamp = now_utc()
                db.execute(
                    "UPDATE table_sessions SET status='closed',closed_at=?,updated_at=? "
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
