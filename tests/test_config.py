from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class ConfigValidationTests(unittest.TestCase):
    def test_example_token_is_rejected_with_clear_error(self) -> None:
        with patch.dict(os.environ, {"BOT_TOKEN": "YOUR_TELEGRAM_BOT_TOKEN_HERE"}):
            with self.assertRaisesRegex(RuntimeError, "BOT_TOKEN не задан"):
                config.get_bot_token()

    def test_nonempty_token_is_returned_unchanged(self) -> None:
        token = "123456789:realistic_test_token_value"
        with patch.dict(os.environ, {"BOT_TOKEN": token}):
            self.assertEqual(config.get_bot_token(), token)

    def test_admin_ids_accept_only_positive_numeric_user_ids(self) -> None:
        with patch.dict(os.environ, {"ADMIN_IDS": "123, 456;123"}):
            self.assertEqual(config.get_admin_ids(), {123, 456})
        for invalid in ("abc", "0", "-10"):
            with self.subTest(invalid=invalid), patch.dict(os.environ, {"ADMIN_IDS": invalid}):
                with self.assertRaises(RuntimeError):
                    config.get_admin_ids()

    def test_blank_database_path_uses_safe_default(self) -> None:
        with patch.dict(os.environ, {"DATABASE_PATH": "   "}):
            self.assertEqual(config.get_database_path(), config.BASE_DIR / "data" / "sbor.sqlite3")

    def test_config_json_must_contain_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text("[]", encoding="utf-8")
            with patch.object(config, "BASE_DIR", Path(temp_dir)):
                with self.assertRaisesRegex(RuntimeError, "JSON-объект"):
                    config.load_defaults()


if __name__ == "__main__":
    unittest.main()
