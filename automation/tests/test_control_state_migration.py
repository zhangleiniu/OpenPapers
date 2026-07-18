import hashlib
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from contextlib import redirect_stdout

from automation.control_state import (
    CONTROL_SCHEMA_VERSION,
    _MIGRATION_1,
    _MIGRATION_2,
    _MIGRATION_3,
    _MIGRATION_4,
    _MIGRATION_5,
    _MIGRATION_6,
    _MIGRATION_7,
    _MIGRATION_8,
    _MIGRATION_9,
    _MIGRATION_10,
)
from automation.control_state_migration import (
    ControlStateMigrationError,
    audit_control_state,
    main,
    rehearse_control_state_migration,
)


NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ControlStateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.source = self.root / "source.sqlite3"
        with sqlite3.connect(self.source) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for statement in (
                *_MIGRATION_1, *_MIGRATION_2, *_MIGRATION_3, *_MIGRATION_4,
                *_MIGRATION_5, *_MIGRATION_6, *_MIGRATION_7, *_MIGRATION_8,
                *_MIGRATION_9, *_MIGRATION_10,
            ):
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                ((version, "2026-07-15T14:00:00Z") for version in range(1, 11)),
            )
            connection.execute(
                "INSERT INTO control_ownership VALUES (1, 'local_control_plane', ?)",
                ("2026-07-15T14:00:00Z",),
            )
            connection.execute(
                "INSERT INTO event_date_schedule VALUES "
                "('icml', 2026, 'pending', ?, NULL, NULL, NULL, NULL, NULL, "
                "0, NULL, NULL, ?)",
                ("2026-07-15T14:00:00Z", "2026-07-15T14:00:00Z"),
            )
            connection.execute("PRAGMA user_version = 10")
        self.source.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_read_only_audit_and_isolated_rehearsal_preserve_source(self):
        before = sha256(self.source)
        audit = audit_control_state(self.source)
        self.assertEqual(audit.schema_version, 10)
        self.assertTrue(audit.quick_check_ok)
        self.assertTrue(audit.migration_ready)
        self.assertEqual(dict(audit.preserved_counts)["event_date_schedule"], 1)
        self.assertEqual(sha256(self.source), before)

        rehearsal_root = self.root / "rehearsal"
        rehearsal_root.mkdir(mode=0o700)
        result = rehearse_control_state_migration(
            self.source, rehearsal_root, clock=lambda: NOW
        )
        self.assertEqual(result.source_schema_version, 10)
        self.assertEqual(result.migrated_schema_version, CONTROL_SCHEMA_VERSION)
        self.assertTrue(result.source_unchanged)
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sha256(self.source), before)
        self.assertEqual(audit_control_state(self.source).schema_version, 10)
        self.assertEqual(
            audit_control_state(result.backup_path).schema_version,
            CONTROL_SCHEMA_VERSION,
        )
        with self.assertRaisesRegex(ControlStateMigrationError, "exists"):
            rehearse_control_state_migration(
                self.source, rehearsal_root, clock=lambda: NOW
            )

    def test_wrong_owner_blocks_rehearsal_without_creating_copy(self):
        with sqlite3.connect(self.source) as connection:
            connection.execute(
                "UPDATE control_ownership SET owner_kind = 'cloud_control_plane'"
            )
        rehearsal_root = self.root / "blocked"
        rehearsal_root.mkdir(mode=0o700)
        self.assertFalse(audit_control_state(self.source).migration_ready)
        with self.assertRaisesRegex(ControlStateMigrationError, "not migration-ready"):
            rehearse_control_state_migration(
                self.source, rehearsal_root, clock=lambda: NOW
            )
        self.assertEqual(tuple(rehearsal_root.iterdir()), ())

    def test_active_artifact_blocks_migration_readiness(self):
        with sqlite3.connect(self.source) as connection:
            connection.execute(
                "INSERT INTO agent_schedule VALUES "
                "('icml', 2026, 'active', NULL, 1, 'run-active', 0, NULL, "
                "NULL, NULL, NULL, ?)",
                ("2026-07-15T14:00:00Z",),
            )
            connection.execute(
                "INSERT INTO agent_run_attempt VALUES "
                "('run-active', 'icml', 2026, 1, ?, NULL, 'active', NULL, "
                "NULL, NULL)",
                ("2026-07-15T14:00:00Z",),
            )
            connection.execute(
                "INSERT INTO agent_execution_artifact VALUES "
                "('run-active', 'active', '/tmp/runs', '/tmp/runs/active', "
                "'agent/active', 'abc123', ?, NULL, NULL, NULL, 0, "
                "'retained', NULL, NULL)",
                ("2026-07-15T14:00:00Z",),
            )
        audit = audit_control_state(self.source)
        self.assertEqual(audit.active_agent_runs, 1)
        self.assertEqual(audit.active_artifacts, 1)
        self.assertFalse(audit.migration_ready)

    def test_audit_command_returns_safe_json_without_echoing_path(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["audit", "--state", str(self.source)])
        self.assertEqual(status, 0)
        payload = output.getvalue()
        self.assertNotIn(str(self.source), payload)
        self.assertIn('"migration_ready": true', payload)


if __name__ == "__main__":
    unittest.main()
