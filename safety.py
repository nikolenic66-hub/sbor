from __future__ import annotations

from typing import Any


DANGEROUS_SPREADSHEET_PREFIXES = ("=", "+", "-", "@")


def safe_csv_cell(value: Any) -> Any:
    """Prevent spreadsheet formula evaluation for untrusted CSV cell values."""
    if not isinstance(value, str) or not value:
        return value
    # Leading spaces are also skipped by some spreadsheet applications before
    # formula parsing; prefix the whole cell so the formula marker stays text.
    probe = value.lstrip(" \t\r\n")
    if value[0] in "\t\r\n" or probe.startswith(DANGEROUS_SPREADSHEET_PREFIXES):
        return "'" + value
    return value


def is_private_chat(chat_type: Any) -> bool:
    """Return whether Telegram identifies the chat as a private conversation."""
    chat_value = getattr(chat_type, "value", chat_type)
    return str(chat_value) == "private"


def is_private_admin(user_id: int | None, chat_type: Any, admin_ids: set[int]) -> bool:
    """Access policy for admin actions: configured user, private bot chat only."""
    return user_id is not None and user_id in admin_ids and is_private_chat(chat_type)
