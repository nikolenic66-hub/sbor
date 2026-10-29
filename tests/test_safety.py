from __future__ import annotations

import unittest

from main import escape_text
from safety import is_private_admin, safe_csv_cell


class SafetyTests(unittest.TestCase):
    def test_csv_formula_markers_are_neutralized(self) -> None:
        for value in ("=1+1", "+SUM(A1:A2)", "-2+3", "@SUM(A1)", "  =1+1", "\t=1+1", "\r=1+1"):
            with self.subTest(value=value):
                self.assertEqual(safe_csv_cell(value), "'" + value)

    def test_plain_values_are_not_changed(self) -> None:
        for value in ("Иван Петров", "Обычное описание", "", "1000", "10-20"):
            with self.subTest(value=value):
                self.assertEqual(safe_csv_cell(value), value)

    def test_non_string_values_are_not_changed(self) -> None:
        self.assertEqual(safe_csv_cell(123), 123)
        self.assertIsNone(safe_csv_cell(None))

    def test_admin_access_requires_configured_id_and_private_chat(self) -> None:
        admins = {123, 456}
        self.assertTrue(is_private_admin(123, "private", admins))
        self.assertFalse(is_private_admin(123, "group", admins))
        self.assertFalse(is_private_admin(999, "private", admins))
        self.assertFalse(is_private_admin(None, "private", admins))

    def test_html_custom_questions_are_escaped_before_telegram_html_parse(self) -> None:
        self.assertEqual(escape_text("<b>Назовите компанию</b>"), "&lt;b&gt;Назовите компанию&lt;/b&gt;")

    def test_private_chat_enum_like_value_is_supported(self) -> None:
        class ChatType:
            value = "private"
        self.assertTrue(is_private_admin(123, ChatType(), {123}))


if __name__ == "__main__":
    unittest.main()
