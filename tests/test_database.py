from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database


def sample_lead(index: int = 0) -> dict:
    return {
        "user_id": 1000 + index,
        "username": "client",
        "full_name": f"Клиент {index}",
        "service": "Консультация",
        "contact": "@client",
        "description": "Нужна консультация по услуге",
        "budget": "10000",
        "deadline": "На следующей неделе",
        "custom_answers": {"Город?": "Казань"},
    }


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.sqlite3")
        await self.db.initialize({
            "company_name": "Тест",
            "services": ["Консультация"],
            "custom_questions": [],
        })

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_settings_can_be_updated(self) -> None:
        self.assertEqual(await self.db.get_setting("company_name"), "Тест")
        await self.db.set_setting("company_name", "Новое название")
        self.assertEqual(await self.db.get_setting("company_name"), "Новое название")

    async def test_lead_lifecycle_and_stats(self) -> None:
        lead_id = await self.db.create_lead(sample_lead())
        lead = await self.db.get_lead(lead_id)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["custom_answers"], {"Город?": "Казань"})
        self.assertEqual(lead["status"], "new")
        self.assertEqual(lead["notification_status"], "pending")
        self.assertTrue(await self.db.update_status(lead_id, "in_progress"))
        self.assertFalse(await self.db.update_status(9999, "completed"))
        stats = await self.db.get_stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["in_progress"], 1)
        self.assertEqual(len(await self.db.export_leads()), 1)

    async def test_reinitialize_preserves_admin_settings(self) -> None:
        await self.db.set_setting("company_name", "Настроено владельцем")
        await self.db.initialize({"company_name": "Новое значение по умолчанию"})
        self.assertEqual(await self.db.get_setting("company_name"), "Настроено владельцем")

    async def test_invalid_status_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.db.update_status(1, "unknown")

    async def test_list_filter_count_and_pagination(self) -> None:
        for index in range(23):
            await self.db.create_lead(sample_lead(index))
        self.assertEqual(await self.db.count_leads(), 23)
        self.assertEqual(await self.db.count_leads("new"), 23)
        self.assertEqual(len(await self.db.list_leads(limit=10, offset=0)), 10)
        second_page = await self.db.list_leads(limit=10, offset=10)
        self.assertEqual(len(second_page), 10)
        self.assertGreater(second_page[0]["id"], second_page[-1]["id"])
        self.assertEqual(len(await self.db.list_leads(status="cancelled")), 0)

    async def test_search_by_id_name_and_literal_wildcards(self) -> None:
        first_id = await self.db.create_lead(sample_lead(1))
        await self.db.create_lead({**sample_lead(2), "full_name": "Анна 100% честно"})
        self.assertEqual((await self.db.search_leads(f"#{first_id}"))[0]["id"], first_id)
        self.assertEqual((await self.db.search_leads("Анна"))[0]["full_name"], "Анна 100% честно")
        self.assertEqual((await self.db.search_leads("100%"))[0]["full_name"], "Анна 100% честно")

    async def test_notification_failure_tracking_and_retry_state(self) -> None:
        lead_id = await self.db.create_lead(sample_lead())
        self.assertTrue(await self.db.update_notification_state(
            lead_id, delivered_admins=[11], failed_admins=[12], status="partial",
            error="12: TelegramForbiddenError",
        ))
        lead = await self.db.get_lead(lead_id)
        self.assertEqual(lead["notification_status"], "partial")
        self.assertEqual(lead["notification_delivered_admins"], [11])
        self.assertEqual(lead["notification_failed_admins"], [12])
        self.assertEqual(await self.db.count_notification_issues(), 1)
        self.assertEqual((await self.db.list_notification_issues())[0]["id"], lead_id)
        await self.db.update_notification_state(lead_id, [11, 12], [], "sent")
        self.assertEqual(await self.db.count_notification_issues(), 0)

    async def test_migrates_old_mvp_database_without_losing_leads(self) -> None:
        path = Path(self.tempdir.name) / "legacy.sqlite3"
        with sqlite3.connect(path) as legacy:
            legacy.executescript("""
                CREATE TABLE leads (
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
                INSERT INTO leads (user_id, full_name, service, contact, description)
                VALUES (42, 'Старый клиент', 'Тест', '@client', 'Старая заявка');
            """)
        legacy_db = Database(path)
        await legacy_db.initialize({"company_name": "Миграция"})
        lead = await legacy_db.get_lead(1)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["full_name"], "Старый клиент")
        self.assertEqual(lead["notification_status"], "unknown")
        self.assertEqual(await legacy_db.count_notification_issues(), 0)


if __name__ == "__main__":
    unittest.main()
