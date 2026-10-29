from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file() -> None:
    """Load simple KEY=value entries from .env without requiring a dependency."""
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


def load_defaults() -> dict[str, Any]:
    """Load editable default settings; persisted admin edits live in the database."""
    path = BASE_DIR / "config.json"
    try:
        with path.open("r", encoding="utf-8") as fh:
            defaults = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось прочитать config.json: {exc}") from exc

    if not isinstance(defaults, dict):
        raise RuntimeError("config.json должен содержать JSON-объект с настройками.")

    env_overrides = {
        "company_name": os.getenv("COMPANY_NAME"),
        "welcome_text": os.getenv("WELCOME_TEXT"),
        "contact_text": os.getenv("CONTACT_TEXT"),
    }
    for key, value in env_overrides.items():
        if value:
            defaults[key] = value
    return defaults


def get_bot_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    placeholder_markers = ("YOUR_TELEGRAM", "TOKEN_HERE", "PLACEHOLDER", "CHANGE_ME", "REPLACE_ME")
    if not token or any(marker in token.upper() for marker in placeholder_markers):
        raise RuntimeError(
            "BOT_TOKEN не задан: укажите настоящий токен, полученный у @BotFather, "
            "в секретах хостинга или в локальном .env."
        )
    return token


def get_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    result: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            user_id = int(item)
        except ValueError as exc:
            raise RuntimeError(f"ADMIN_IDS содержит нечисловой Telegram ID: {item!r}") from exc
        if user_id <= 0:
            raise RuntimeError(f"ADMIN_IDS должен содержать только положительные Telegram user ID: {item!r}")
        result.add(user_id)
    return result


def get_database_path() -> Path:
    raw = os.getenv("DATABASE_PATH", "").strip() or "data/sbor.sqlite3"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def get_backup_directory() -> Path:
    raw = os.getenv("BACKUP_DIRECTORY", "").strip()
    if not raw:
        return get_database_path().parent / "backups"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def get_backup_retention() -> int:
    raw = os.getenv("BACKUP_RETENTION", "10").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("BACKUP_RETENTION должен быть целым числом от 1 до 100.") from exc
    if not 1 <= value <= 100:
        raise RuntimeError("BACKUP_RETENTION должен быть от 1 до 100.")
    return value


def get_lead_retention_days() -> int:
    raw = os.getenv("LEAD_RETENTION_DAYS", "180").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("LEAD_RETENTION_DAYS должен быть целым числом от 30 до 3650.") from exc
    if not 30 <= value <= 3650:
        raise RuntimeError("LEAD_RETENTION_DAYS должен быть от 30 до 3650.")
    return value


def get_backup_interval_hours() -> int:
    raw = os.getenv("BACKUP_INTERVAL_HOURS", "24").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("BACKUP_INTERVAL_HOURS должен быть целым числом от 1 до 168.") from exc
    if not 1 <= value <= 168:
        raise RuntimeError("BACKUP_INTERVAL_HOURS должен быть от 1 до 168.")
    return value
