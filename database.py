from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")
LEAD_STATUSES = {"new", "in_progress", "completed", "cancelled"}
NOTIFICATION_STATUSES = {"pending", "sent", "partial", "failed", "unknown"}
BACKUP_FILENAME_RE = re.compile(r"^sbor_\d{8}_\d{6}_[0-9a-f]{4}\.sqlite3$")
BACKUP_REQUIRED_TABLES = {"leads", "settings"}


class Database:
    """SQLite repository; local file operations run in a worker thread."""

    def __init__(self, path: Path, backup_dir: Path | None = None, backup_retention: int = 10) -> None:
        self.path = path
        self.backup_dir = backup_dir or (path.parent / "backups")
        self.backup_retention = max(1, min(int(backup_retention), 100))
        self._operation_lock = threading.RLock()

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
        # Backups/restores must not overlap a write in this process. Keep each
        # SQLite operation serialised; handlers remain async while I/O is threaded.
        def locked_operation() -> T:
            with self._operation_lock:
                return operation()
        return await asyncio.to_thread(locked_operation)

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

    @staticmethod
    def _validate_backup_file(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise ValueError("Файл резервной копии не найден.")
        connection: sqlite3.Connection | None = None
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=30)
            connection.execute("PRAGMA busy_timeout = 30000")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise ValueError("Проверка целостности SQLite не пройдена.")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not BACKUP_REQUIRED_TABLES.issubset(tables):
                raise ValueError("В файле нет обязательных таблиц заявок и настроек.")
            lead_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(leads)").fetchall()
            }
            settings_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(settings)").fetchall()
            }
            required_lead_columns = {
                "id", "user_id", "username", "full_name", "service",
                "contact", "description", "status", "created_at",
            }
            if not required_lead_columns.issubset(lead_columns):
                raise ValueError("Схема таблицы заявок не распознана.")
            if not {"key", "value"}.issubset(settings_columns):
                raise ValueError("Схема таблицы настроек не распознана.")
            lead_count = int(connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0])
            setting_count = int(connection.execute("SELECT COUNT(*) FROM settings").fetchone()[0])
            return {"lead_count": lead_count, "setting_count": setting_count}
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"Файл не является исправной базой SQLite: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    def _backup_path(self, filename: str) -> Path:
        if not BACKUP_FILENAME_RE.fullmatch(str(filename)):
            raise ValueError("Недопустимое имя резервной копии.")
        path = self.backup_dir / filename
        if not path.is_file():
            raise ValueError("Резервная копия уже недоступна.")
        return path

    @staticmethod
    def _backup_metadata(path: Path) -> dict[str, Any]:
        stat = path.stat()
        created = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        return {
            "filename": path.name,
            "path": str(path),
            "size_bytes": stat.st_size,
            "created_at": created.strftime("%Y-%m-%d %H:%M:%S UTC"),
        }

    def _prune_backups_sync(self, protected: set[str] | None = None) -> None:
        protected = protected or set()
        if not self.backup_dir.exists():
            return
        files = [
            path for path in self.backup_dir.iterdir()
            if path.is_file() and BACKUP_FILENAME_RE.fullmatch(path.name)
        ]
        files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        keep = {item.name for item in files[:self.backup_retention]} | protected
        for path in files:
            if path.name not in keep:
                try:
                    path.unlink()
                except OSError:
                    # Cleanup failure must not turn a valid backup into an error.
                    pass

    def _create_backup_sync(self, *, prune: bool = True) -> dict[str, Any]:
        if not self.path.is_file():
            raise FileNotFoundError(f"База данных не найдена: {self.path}")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        filename = f"sbor_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}.sqlite3"
        target = self.backup_dir / filename
        temporary = self.backup_dir / f".{filename}.{secrets.token_hex(4)}.tmp"
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(str(self.path), timeout=30)
            source.execute("PRAGMA busy_timeout = 30000")
            destination = sqlite3.connect(str(temporary), timeout=30)
            source.backup(destination)
            # A standalone backup must not depend on a -wal file beside it.
            destination.execute("PRAGMA journal_mode = DELETE")
            destination.commit()
            destination.close()
            destination = None
            source.close()
            source = None

            self._validate_backup_file(temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            result = self._backup_metadata(target)
            if prune:
                self._prune_backups_sync(protected={filename})
            return result
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    async def create_backup(self) -> dict[str, Any]:
        return await self._run(self._create_backup_sync)

    def _list_backups_sync(self) -> list[dict[str, Any]]:
        if not self.backup_dir.exists():
            return []
        backups: list[dict[str, Any]] = []
        for path in self.backup_dir.iterdir():
            if not path.is_file() or not BACKUP_FILENAME_RE.fullmatch(path.name):
                continue
            try:
                self._validate_backup_file(path)
                backups.append(self._backup_metadata(path))
            except (ValueError, OSError):
                # Corrupt or incomplete files must never be offered for restore.
                continue
        backups.sort(key=lambda item: item["created_at"], reverse=True)
        return backups

    async def list_backups(self) -> list[dict[str, Any]]:
        return await self._run(self._list_backups_sync)

    def _import_backup_sync(self, source_path: Path) -> dict[str, Any]:
        # Validate the uploaded file before it becomes a selectable restore point.
        self._validate_backup_file(source_path)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        filename = f"sbor_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}.sqlite3"
        target = self.backup_dir / filename
        temporary = self.backup_dir / f".{filename}.{secrets.token_hex(4)}.tmp"
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
            destination = sqlite3.connect(str(temporary), timeout=30)
            source.backup(destination)
            destination.execute("PRAGMA journal_mode = DELETE")
            destination.commit()
            destination.close()
            destination = None
            source.close()
            source = None

            self._validate_backup_file(temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            self._prune_backups_sync(protected={filename})
            return self._backup_metadata(target)
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    async def import_backup(self, source_path: Path) -> dict[str, Any]:
        return await self._run(lambda: self._import_backup_sync(Path(source_path)))

    @staticmethod
    def _ensure_restored_schema(connection: sqlite3.Connection) -> None:
        existing = {
            row[1] for row in connection.execute("PRAGMA table_info(leads)").fetchall()
        }
        migrations = {
            "notification_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "notification_delivered_admins": "TEXT NOT NULL DEFAULT '[]'",
            "notification_failed_admins": "TEXT NOT NULL DEFAULT '[]'",
            "notification_error": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in migrations.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE leads ADD COLUMN {column} {definition}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_status_id ON leads(status, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_notification_status "
            "ON leads(notification_status, id DESC)"
        )

    def _restore_backup_sync(self, filename: str) -> dict[str, Any]:
        backup_path = self._backup_path(filename)
        validation = self._validate_backup_file(backup_path)
        # Keep a snapshot of the current state before any data is overwritten.
        pre_restore = self._create_backup_sync(prune=False)
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(
                f"{backup_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30
            )
            destination = self._connect()
            source.backup(destination)
            self._ensure_restored_schema(destination)
            destination.execute("PRAGMA journal_mode = WAL")
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise sqlite3.DatabaseError("Целостность восстановленной базы не подтверждена.")
            destination.commit()
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
        self._prune_backups_sync(protected={filename, pre_restore["filename"]})
        return {
            "restored_from": filename,
            "pre_restore_backup": pre_restore["filename"],
            "lead_count": validation["lead_count"],
            "setting_count": validation["setting_count"],
        }

    async def restore_backup(self, filename: str) -> dict[str, Any]:
        return await self._run(lambda: self._restore_backup_sync(filename))

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
