import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.agent_status import AgentStatusError, build_production_status
from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.resend_notifications import recipient_fingerprints


NOW = datetime(2026, 7, 16, 14, 15, tzinfo=timezone.utc)


class AgentStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.internal = self.root / "internal"
        self.repository = self.root / "runtime"
        self.execution = self.root / "external"
        for path in (
            self.internal / "control", self.internal / "service",
            self.repository, self.execution,
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state = self.internal / "control" / "state.sqlite3"
        with ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW
        ):
            pass
        self.state.chmod(0o600)
        self.run_records = self.internal / "service" / "runs.v1.json"
        self._private_json(self.run_records, {
            "schema_version": 1,
            "records": [{
                "status": "completed", "code": "no_due_work",
                "scheduled_for": self._time(NOW - timedelta(minutes=58)),
                "observed_at": self._time(NOW - timedelta(minutes=57)),
                "selection_count": 0, "health_ready": True,
            }],
        })
        self.codex_home = self.internal / "codex"
        self.codex_home.mkdir(mode=0o700)
        (self.codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
        self.adc = self.internal / "adc.json"
        self.adc.write_text("{}\n", encoding="utf-8")
        os.chmod(self.codex_home / "auth.json", 0o600)
        os.chmod(self.adc, 0o600)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _time(value):
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _private_json(path, payload):
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def _configuration(self):
        recipients = ("first@example.test", "second@example.test")
        return SimpleNamespace(
            external_effects_enabled=True,
            agent_source_commit="a" * 40,
            agent=SimpleNamespace(
                resend_recipient_sha256s=recipient_fingerprints(recipients),
                minimum_free_bytes=10_000_000_000,
            ),
        ), SimpleNamespace(email_to=recipients)

    def _seed_completed_run(self):
        next_check = self._time(NOW + timedelta(days=7))
        with sqlite3.connect(self.state) as connection:
            connection.execute(
                "INSERT INTO event_date_schedule VALUES "
                "('icml', 2026, 'scheduled', ?, '2026-07-07', ?, "
                "'fixture', 'fixture', 'v1', 1, NULL, NULL, ?)",
                (next_check, self._time(NOW - timedelta(days=9)), self._time(NOW)),
            )
            connection.execute(
                "INSERT INTO event_date_attempt VALUES "
                "('date-attempt', 'icml', 2026, 1, ?, ?, 'scheduled', "
                "'fixture', 'fixture', 'v1', '2026-07-07', NULL)",
                (self._time(NOW - timedelta(days=9, minutes=1)),
                 self._time(NOW - timedelta(days=9))),
            )
            connection.execute(
                "INSERT INTO agent_schedule VALUES "
                "('icml', 2026, 'scheduled', ?, 1, NULL, 0, 'not_ready', "
                "?, ?, NULL, ?)",
                (next_check, self._time(NOW - timedelta(hours=1)), next_check,
                 self._time(NOW)),
            )
            connection.execute(
                "INSERT INTO agent_run_attempt VALUES "
                "('private-run-id', 'icml', 2026, 1, ?, ?, 'not_ready', "
                "'private explanation', ?, NULL)",
                (self._time(NOW - timedelta(hours=2)),
                 self._time(NOW - timedelta(hours=1)), next_check),
            )
            connection.execute(
                "INSERT INTO agent_execution_artifact VALUES "
                "('private-run-id', 'terminal', '/private/runs', "
                "'/private/runs/worktree', 'automation/agent/private', ?, ?, ?, "
                "?, 0, 0, 'retained', NULL, NULL)",
                ("b" * 40, self._time(NOW - timedelta(hours=2)),
                 self._time(NOW - timedelta(hours=1)),
                 json.dumps({"items": ["private-name.py"]})),
            )
            connection.execute(
                "INSERT INTO agent_run_report VALUES "
                "('private-report-id', 'private-run-id', 'delivered', "
                "'scheduled', ?, 1, ?, ?, ?, NULL, 'private-receipt')",
                (next_check, self._time(NOW - timedelta(hours=1)),
                 self._time(NOW - timedelta(minutes=59)),
                 self._time(NOW - timedelta(minutes=59))),
            )
            connection.execute(
                "INSERT INTO agent_run_report_attempt VALUES "
                "('private-report-id', 1, ?, ?, 'delivered', NULL, "
                "'private-receipt')",
                (self._time(NOW - timedelta(minutes=60)),
                 self._time(NOW - timedelta(minutes=59))),
            )

    def test_status_is_read_only_bounded_and_secret_free(self):
        self._seed_completed_run()
        before = self.state.read_bytes()
        configuration = self._configuration()
        credentials = SimpleNamespace(codex_home=self.codex_home, google_adc=self.adc)
        with patch(
            "automation.agent_status.validate_agent_production_root",
            return_value=configuration,
        ), patch(
            "automation.agent_status.validate_agent_credential_context",
            return_value=credentials,
        ), patch("automation.agent_status.validate_agent_source"):
            result = build_production_status(
                internal_root=self.internal,
                repository_root=self.repository,
                execution_root=self.execution,
                state_path=self.state,
                service_loaded=True,
                clock=lambda: NOW,
                disk_usage=lambda _: SimpleNamespace(free=20_000_000_000),
            )
        self.assertEqual(self.state.read_bytes(), before)
        self.assertTrue(result["production"]["external_effects_enabled"])
        self.assertEqual(result["production"]["recipient_count"], 2)
        self.assertEqual(result["service"]["recent_wakes"][0]["code"], "no_due_work")
        self.assertEqual(result["targets"][0]["latest_attempt"]["disposition"],
                         "not_ready")
        self.assertEqual(result["targets"][0]["agent"]["updated_at"],
                         self._time(NOW))
        self.assertEqual(result["targets"][0]["event_date"]["updated_at"],
                         self._time(NOW))
        self.assertEqual(result["targets"][0]["latest_report"]["status"],
                         "delivered")
        self.assertEqual(result["targets"][0]["latest_artifact"][
            "changed_file_count"], 1)
        encoded = json.dumps(result)
        for forbidden in (
            str(self.root), "first@example.test", "second@example.test",
            "private explanation", "private-name.py", "/private/runs",
            "private-receipt", "private-run-id", "private-report-id",
            "explanation", "changed_files", "receipt_id",
        ):
            self.assertNotIn(forbidden, encoded)

if __name__ == "__main__":
    unittest.main()
