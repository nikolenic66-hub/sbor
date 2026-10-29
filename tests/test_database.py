from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from database import Database


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
        lead_id = await self.db.create_lead({
            "user_id": 1001,
            "username": "client",
            "full_name": "Иван",
            "service": "Консультация",
            "contact": "@client",
            "description": "Нужна консультация по услуге",
            "budget": "10000",
            "deadline": "На следующей неделе",
            "custom_answers": {"Город?": "Казань"},
        })
        lead = await self.db.get_lead(lead_id)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["custom_answers"], {"Город?": "Казань"})
        self.assertEqual(lead["status"], "new")
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

    async def test_list_filter_and_limit(self) -> None:
        for index in range(3):
            await self.db.create_lead({
                "user_id": 1000 + index,
                "full_name": f"Клиент {index}",
                "service": "Тест",
                "contact": "@client",
                "description": "Описание заявки",
            })
        latest = await self.db.list_leads(limit=2)
        self.assertEqual(len(latest), 2)
        self.assertGreater(latest[0]["id"], latest[1]["id"])
        self.assertEqual(len(await self.db.list_leads(status="new")), 3)
        self.assertEqual(len(await self.db.list_leads(status="cancelled")), 0)


if __name__ == "__main__":
    unittest.main()
