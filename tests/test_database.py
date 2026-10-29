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

    async def test_retention_deletes_only_old_closed_leads(self) -> None:
        old_completed = await self.db.create_lead(sample_lead(10))
        old_cancelled = await self.db.create_lead(sample_lead(11))
        old_new = await self.db.create_lead(sample_lead(12))
        old_in_progress = await self.db.create_lead(sample_lead(13))
        recent_completed = await self.db.create_lead(sample_lead(14))
        await self.db.update_status(old_completed, "completed")
        await self.db.update_status(old_cancelled, "cancelled")
        await self.db.update_status(old_in_progress, "in_progress")

        with sqlite3.connect(self.db.path) as raw:
            raw.execute(
                "UPDATE leads SET created_at = '2020-01-01 00:00:00' WHERE id IN (?, ?, ?, ?)",
                (old_completed, old_cancelled, old_new, old_in_progress),
            )

        self.assertEqual(await self.db.count_old_closed_leads(180), 2)
        result = await self.db.delete_old_closed_leads(180)
        self.assertEqual(result["deleted"], 2)
        self.assertIsNone(await self.db.get_lead(old_completed))
        self.assertIsNone(await self.db.get_lead(old_cancelled))
        self.assertIsNotNone(await self.db.get_lead(old_new))
        self.assertIsNotNone(await self.db.get_lead(old_in_progress))
        self.assertIsNotNone(await self.db.get_lead(recent_completed))

    async def test_retention_days_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            await self.db.count_old_closed_leads(10)
        with self.assertRaises(ValueError):
            await self.db.delete_old_closed_leads(5000)

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



    async def test_backup_and_restore_keeps_a_pre_restore_snapshot(self) -> None:
        original_id = await self.db.create_lead(sample_lead(1))
        await self.db.set_setting("company_name", "До восстановления")
        backup = await self.db.create_backup()

        await self.db.create_lead(sample_lead(2))
        await self.db.set_setting("company_name", "После создания копии")

        result = await self.db.restore_backup(backup["filename"])
        leads = await self.db.list_leads(limit=100)
        self.assertEqual([lead["id"] for lead in leads], [original_id])
        self.assertEqual(await self.db.get_setting("company_name"), "До восстановления")
        self.assertEqual(result["restored_from"], backup["filename"])
        self.assertNotEqual(result["pre_restore_backup"], backup["filename"])
        self.assertTrue((self.db.backup_dir / result["pre_restore_backup"]).is_file())
        self.assertIn(backup["filename"], [item["filename"] for item in await self.db.list_backups()])

    async def test_import_backup_validates_before_adding_restore_point(self) -> None:
        backup = await self.db.create_backup()
        imported = await self.db.import_backup(Path(backup["path"]))
        self.assertNotEqual(imported["filename"], backup["filename"])
        self.assertTrue((self.db.backup_dir / imported["filename"]).is_file())

        corrupt = Path(self.tempdir.name) / "not-a-database.sqlite3"
        corrupt.write_text("not a SQLite database", encoding="utf-8")
        with self.assertRaises(ValueError):
            await self.db.import_backup(corrupt)

    async def test_backup_retention_keeps_configured_number(self) -> None:
        self.db.backup_retention = 2
        for _ in range(5):
            await self.db.create_backup()
        backups = await self.db.list_backups()
        self.assertEqual(len(backups), 2)

    async def test_restore_rejects_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            await self.db.restore_backup("../private.sqlite3")

    async def test_restore_migrates_older_backup_schema(self) -> None:
        old_path = Path(self.tempdir.name) / "legacy.sqlite3"
        with sqlite3.connect(old_path) as legacy:
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
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO leads (user_id, full_name, service, contact, description)
                VALUES (77, 'Старый клиент', 'Тест', '@old', 'Заявка из старой базы');
                INSERT INTO settings(key, value) VALUES ('company_name', '"Старый бренд"');
            """)
        imported = await self.db.import_backup(old_path)
        result = await self.db.restore_backup(imported["filename"])
        lead = await self.db.get_lead(1)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["full_name"], "Старый клиент")
        self.assertEqual(lead["notification_status"], "unknown")
        self.assertEqual(await self.db.get_setting("company_name"), "Старый бренд")
        self.assertEqual(result["lead_count"], 1)

if __name__ == "__main__":
    unittest.main()
