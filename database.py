from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")
LEAD_STATUSES = {"new", "in_progress", "completed", "cancelled"}
NOTIFICATION_STATUSES = {"pending", "sent", "partial", "failed", "unknown"}


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
                        updated_at TEXT,
                        notification_status TEXT NOT NULL DEFAULT 'pending',
                        notification_delivered_admins TEXT NOT NULL DEFAULT '[]',
                        notification_failed_admins TEXT NOT NULL DEFAULT '[]',
                        notification_error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_leads_status_id ON leads(status, id DESC);
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )

                # Forward-compatible migration for databases created by the first MVP.
                existing = {row["name"] for row in db.execute("PRAGMA table_info(leads)").fetchall()}
                migrations = {
                    "notification_status": "TEXT NOT NULL DEFAULT 'unknown'",
                    "notification_delivered_admins": "TEXT NOT NULL DEFAULT '[]'",
                    "notification_failed_admins": "TEXT NOT NULL DEFAULT '[]'",
                    "notification_error": "TEXT NOT NULL DEFAULT ''",
                }
                for column, definition in migrations.items():
                    if column not in existing:
                        db.execute(f"ALTER TABLE leads ADD COLUMN {column} {definition}")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_leads_notification_status "
                    "ON leads(notification_status, id DESC)"
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
        if status is not None and status not in LEAD_STATUSES:
            raise ValueError("Недопустимый статус заявки")

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

    async def count_leads(self, status: str | None = None) -> int:
        if status is not None and status not in LEAD_STATUSES:
            raise ValueError("Недопустимый статус заявки")

        def operation() -> int:
            with self._connection() as db:
                if status:
                    row = db.execute("SELECT COUNT(*) AS total FROM leads WHERE status = ?", (status,)).fetchone()
                else:
                    row = db.execute("SELECT COUNT(*) AS total FROM leads").fetchone()
            return int(row["total"] if row else 0)
        return await self._run(operation)

    async def search_leads(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        query = str(query).strip()[:100]
        limit = max(1, min(int(limit), 100))
        if not query:
            return []

        def operation() -> list[dict[str, Any]]:
            with self._connection() as db:
                clean = query[1:] if query.startswith("#") else query
                if clean.isdigit():
                    rows = db.execute(
                        "SELECT * FROM leads WHERE id = ? OR full_name LIKE ? ESCAPE '!' "
                        "OR contact LIKE ? ESCAPE '!' ORDER BY id DESC LIMIT ?",
                        (int(clean), self._like_pattern(query), self._like_pattern(query), limit),
                    ).fetchall()
                else:
                    pattern = self._like_pattern(query)
                    rows = db.execute(
                        "SELECT * FROM leads WHERE full_name LIKE ? ESCAPE '!' "
                        "OR contact LIKE ? ESCAPE '!' ORDER BY id DESC LIMIT ?",
                        (pattern, pattern, limit),
                    ).fetchall()
            return [self._lead_dict(row) for row in rows]
        return await self._run(operation)

    @staticmethod
    def _like_pattern(query: str) -> str:
        escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        return f"%{escaped}%"

    async def update_status(self, lead_id: int, status: str) -> bool:
        if status not in LEAD_STATUSES:
            raise ValueError("Недопустимый статус заявки")

        def operation() -> bool:
            with self._connection() as db:
                cursor = db.execute(
                    "UPDATE leads SET status = ?, updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now') WHERE id = ?",
                    (status, lead_id),
                )
                return cursor.rowcount > 0
        return await self._run(operation)

    async def update_notification_state(
        self,
        lead_id: int,
        delivered_admins: list[int],
        failed_admins: list[int],
        status: str,
        error: str = "",
    ) -> bool:
        if status not in NOTIFICATION_STATUSES:
            raise ValueError("Недопустимый статус уведомления")
        delivered = json.dumps(sorted({int(x) for x in delivered_admins}), ensure_ascii=False)
        failed = json.dumps(sorted({int(x) for x in failed_admins}), ensure_ascii=False)

        def operation() -> bool:
            with self._connection() as db:
                cursor = db.execute(
                    """UPDATE leads
                       SET notification_delivered_admins = ?,
                           notification_failed_admins = ?,
                           notification_status = ?,
                           notification_error = ?
                       WHERE id = ?""",
                    (delivered, failed, status, str(error)[:500], lead_id),
                )
                return cursor.rowcount > 0
        return await self._run(operation)

    async def list_notification_issues(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))

        def operation() -> list[dict[str, Any]]:
            with self._connection() as db:
                rows = db.execute(
                    """SELECT * FROM leads
                       WHERE notification_status IN ('pending', 'partial', 'failed')
                       ORDER BY id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [self._lead_dict(row) for row in rows]
        return await self._run(operation)

    async def count_notification_issues(self) -> int:
        def operation() -> int:
            with self._connection() as db:
                row = db.execute(
                    "SELECT COUNT(*) AS total FROM leads "
                    "WHERE notification_status IN ('pending', 'partial', 'failed')"
                ).fetchone()
            return int(row["total"] if row else 0)
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
        for field, fallback in (
            ("custom_answers", {}),
            ("notification_failed_admins", []),
            ("notification_delivered_admins", []),
        ):
            try:
                result[field] = json.loads(result.get(field) or json.dumps(fallback))
            except (TypeError, json.JSONDecodeError):
                result[field] = fallback
        return result
