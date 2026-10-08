"""Durable single-claim idempotency ledger for Clover order creation."""

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class SQLiteOrderLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS order_idempotency (
                scope TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending','succeeded','uncertain')),
                response_json TEXT,
                PRIMARY KEY (scope, key_hash)
            )"""
        )
        return connection

    def _claim(self, scope: str, key_hash: str, fingerprint: str) -> tuple[str, dict | None]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT OR IGNORE INTO order_idempotency VALUES (?, ?, ?, 'pending', NULL)",
                (scope, key_hash, fingerprint),
            ).rowcount == 1
            row = db.execute(
                "SELECT fingerprint, state, response_json FROM order_idempotency "
                "WHERE scope=? AND key_hash=?",
                (scope, key_hash),
            ).fetchone()
            db.commit()
        if row[0] != fingerprint:
            return "conflict", None
        return ("claimed" if inserted else row[1]), json.loads(row[2]) if row[2] else None

    async def claim(self, scope: str, key_hash: str, fingerprint: str) -> tuple[str, dict | None]:
        return await asyncio.to_thread(self._claim, scope, key_hash, fingerprint)

    def _finish(
        self, scope: str, key_hash: str, fingerprint: str, state: str, response: dict | None
    ) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE order_idempotency SET state=?, response_json=? "
                "WHERE scope=? AND key_hash=? AND fingerprint=? AND state='pending'",
                (state, json.dumps(response) if response is not None else None,
                 scope, key_hash, fingerprint),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Order idempotency state changed unexpectedly")
            db.commit()

    async def finish(
        self, scope: str, key_hash: str, fingerprint: str, state: str, response: dict | None = None
    ) -> None:
        await asyncio.to_thread(self._finish, scope, key_hash, fingerprint, state, response)

    def _release(self, scope: str, key_hash: str, fingerprint: str) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM order_idempotency WHERE scope=? AND key_hash=? "
                "AND fingerprint=? AND state='pending'",
                (scope, key_hash, fingerprint),
            )
            db.commit()

    async def release(self, scope: str, key_hash: str, fingerprint: str) -> None:
        await asyncio.to_thread(self._release, scope, key_hash, fingerprint)
