from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class Database:
    """SQLite repository; local file operations run in a worker thread."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def _run(self, operation: Callable[[], T]) -> T:
        return await asyncio.to_thread(operation)

    async def initialize(self, defaults: dict[str, Any]) -> None:
        def operation() -> None:
            with self._connection() as db:
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS leads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        username TEXT NOT NULL DEFAULT '',
                        full_name TEXT NOT NULL,
                        service TEXT NOT NULL,
                        contact TEXT NOT NULL,
                        description TEXT NOT NULL,
                        budget TEXT NOT NULL DEFAULT '',
                        deadline TEXT NOT NULL DEFAULT '',
                        custom_answers TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'new',
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
                        updated_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_leads_status_id ON leads(status, id DESC);
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                for key, value in defaults.items():
                    db.execute(
                        "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                        (key, json.dumps(value, ensure_ascii=False)),
                    )
        await self._run(operation)

    async def get_setting(self, key: str, default: Any = None) -> Any:
        def operation() -> Any:
            with self._connection() as db:
                row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                return row["value"]
        return await self._run(operation)

    async def set_setting(self, key: str, value: Any) -> None:
        packed = json.dumps(value, ensure_ascii=False)
        def operation() -> None:
            with self._connection() as db:
                db.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, packed),
                )
        await self._run(operation)

    async def create_lead(self, data: dict[str, Any]) -> int:
        def operation() -> int:
            with self._connection() as db:
                cursor = db.execute(
                    """INSERT INTO leads
                       (user_id, username, full_name, service, contact, description,
                        budget, deadline, custom_answers)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(data["user_id"]),
                        str(data.get("username") or ""),
                        str(data["full_name"]).strip()[:100],
                        str(data["service"])[:200],
                        str(data["contact"]).strip()[:200],
                        str(data["description"]).strip()[:4000],
                        str(data.get("budget") or "")[:300],
                        str(data.get("deadline") or "")[:300],
                        json.dumps(data.get("custom_answers") or {}, ensure_ascii=False),
                    ),
                )
                return int(cursor.lastrowid)
        return await self._run(operation)

    async def get_lead(self, lead_id: int) -> dict[str, Any] | None:
        def operation() -> dict[str, Any] | None:
            with self._connection() as db:
                row = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            return self._lead_dict(row) if row else None
        return await self._run(operation)

    async def list_leads(
        self, status: str | None = None, limit: int = 10, offset: int = 0
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        def operation() -> list[dict[str, Any]]:
            with self._connection() as db:
                if status:
                    rows = db.execute(
                        "SELECT * FROM leads WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                        (status, limit, offset),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT * FROM leads ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
                    ).fetchall()
            return [self._lead_dict(row) for row in rows]
        return await self._run(operation)

    async def update_status(self, lead_id: int, status: str) -> bool:
        allowed = {"new", "in_progress", "completed", "cancelled"}
        if status not in allowed:
            raise ValueError("Недопустимый статус заявки")
        def operation() -> bool:
            with self._connection() as db:
                cursor = db.execute(
                    "UPDATE leads SET status = ?, updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now') WHERE id = ?",
                    (status, lead_id),
                )
                return cursor.rowcount > 0
        return await self._run(operation)

    async def get_stats(self) -> dict[str, int]:
        def operation() -> dict[str, int]:
            with self._connection() as db:
                rows = db.execute("SELECT status, COUNT(*) AS total FROM leads GROUP BY status").fetchall()
                total_row = db.execute("SELECT COUNT(*) AS total FROM leads").fetchone()
            result = {"total": int(total_row["total"] if total_row else 0)}
            for status in ("new", "in_progress", "completed", "cancelled"):
                result[status] = 0
            for row in rows:
                result[str(row["status"])] = int(row["total"])
            return result
        return await self._run(operation)

    async def export_leads(self) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            with self._connection() as db:
                rows = db.execute("SELECT * FROM leads ORDER BY id ASC").fetchall()
            return [self._lead_dict(row) for row in rows]
        return await self._run(operation)

    @staticmethod
    def _lead_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["custom_answers"] = json.loads(result.get("custom_answers") or "{}")
        except json.JSONDecodeError:
            result["custom_answers"] = {}
        return result
