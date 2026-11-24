"""Audit timestamps must preserve unknown history and survive repeated migrations."""

import unittest
from unittest.mock import MagicMock, patch

from migrate_provider_timestamps import migrate
from promote_nppes import upsert


class TimestampTests(unittest.TestCase):
    def test_existing_insert_dates_are_not_invented(self):
        connection = MagicMock()
        with patch("migrate_provider_timestamps.provider_columns", return_value={"ImportedUTC": ("datetime2", 8, False)}), patch("migrate_provider_timestamps.validate_timestamps"):
            migrate(connection, True)
        statements = [call.args[0] for call in connection.cursor.return_value.execute.call_args_list]
        self.assertTrue(any("SET [LastUpdatedUTC] = [ImportedUTC]" in sql for sql in statements))
        self.assertFalse(any("SET [InsertedUTC]" in sql for sql in statements))
        connection.commit.assert_called_once()

    def test_repeat_migration_does_not_reset_dates(self):
        connection = MagicMock()
        columns = {name: ("datetime2", 8, True) for name in ("ImportedUTC", "InsertedUTC", "LastUpdatedUTC")}
        with patch("migrate_provider_timestamps.provider_columns", return_value=columns), patch("migrate_provider_timestamps.validate_timestamps"):
            self.assertEqual(migrate(connection, True), [])
        self.assertEqual(connection.cursor.return_value.execute.call_count, 1)

    def test_updates_preserve_insert_time_and_skip_unchanged_rows(self):
        cursor = MagicMock()
        cursor.execute.return_value.fetchone.return_value = None
        upsert(cursor, "#stage", "#final", dict.fromkeys(("NPI", "DeactivationDate", "ReactivationDate")), "ImportedUTC")
        update, insert, _ = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertNotIn("InsertedUTC", update)
        self.assertIn("[LastUpdatedUTC] = SYSUTCDATETIME()", update)
        self.assertIn("WHERE EXISTS", update)
        self.assertIn("[InsertedUTC], [LastUpdatedUTC]", insert)

    def test_failure_rolls_back_migration(self):
        connection = MagicMock()
        with patch("migrate_provider_timestamps.provider_columns", return_value={}):
            with self.assertRaises(ValueError):
                migrate(connection, True)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
