import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.agent_status import (
    AgentStatusError,
    build_production_status,
    create_canary_proof,
    read_canary_proof,
)
from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.resend_notifications import recipient_fingerprints


NOW = datetime(2026, 7, 16, 14, 15, tzinfo=timezone.utc)


def git(root: Path, *arguments: str, binary: bool = False):
    return subprocess.run(
        ("git", *arguments), cwd=root, text=not binary,
        capture_output=True, check=True,
    ).stdout


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
        self.cloud_proof = self.internal / "cloud-proof.json"
        self._private_json(self.cloud_proof, {
            "schema_version": 1,
            "cloud_schedule_paused": True,
            "active_cloud_executions": 0,
            "checked_at": self._time(NOW),
        })
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
        self.canary_proof = self.internal / "canary-proof.json"
        self._private_json(self.canary_proof, {
            "schema_version": 1,
            "checked_at": self._time(NOW),
            "canaries": [self._canary_item(name) for name in (
                "codex_installed", "icml_2026"
            )],
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
    def _canary_item(name):
        return {
            "name": name, "head_matches": True, "branch_matches": True,
            "status_matches": True, "remote_count_matches": True,
            "drifted": False,
        }

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
                cloud_proof_path=self.cloud_proof,
                canary_proof_path=self.canary_proof,
                service_loaded=True,
                clock=lambda: NOW,
                disk_usage=lambda _: SimpleNamespace(free=20_000_000_000),
            )
        self.assertEqual(self.state.read_bytes(), before)
        self.assertTrue(result["production"]["external_effects_enabled"])
        self.assertEqual(result["production"]["recipient_count"], 2)
        self.assertEqual(result["service"]["recent_wakes"][0]["code"], "no_due_work")
        self.assertEqual(len(result["canaries"]["items"]), 2)
        self.assertEqual(result["targets"][0]["latest_attempt"]["disposition"],
                         "not_ready")
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

    def test_canary_proof_accepts_expected_dirty_state_and_detects_drift(self):
        canaries = []
        for name in ("codex_installed", "icml_2026"):
            root = self.root / name
            root.mkdir(mode=0o700)
            git(root, "init", "-q")
            git(root, "config", "user.name", "Fixture")
            git(root, "config", "user.email", "fixture@example.invalid")
            (root / "tracked.txt").write_text("base\n", encoding="utf-8")
            git(root, "add", "tracked.txt")
            git(root, "commit", "-q", "-m", "base")
            if name == "codex_installed":
                (root / "tracked.txt").write_text("accepted dirty\n", encoding="utf-8")
            status = git(root, "status", "--porcelain=v1", "-z", binary=True)
            canaries.append({
                "name": name, "path": str(root),
                "head": git(root, "rev-parse", "HEAD").strip(),
                "branch": git(root, "symbolic-ref", "--short", "HEAD").strip(),
                "status_sha256": hashlib.sha256(status).hexdigest(),
                "remote_count": 0,
            })
        baseline = self.root / "baseline.json"
        self._private_json(baseline, {"schema_version": 1, "canaries": canaries})
        first = create_canary_proof(baseline, clock=lambda: NOW)
        self.assertFalse(any(item["drifted"] for item in first["canaries"]))
        (self.root / "codex_installed" / "new.txt").write_text(
            "drift\n", encoding="utf-8"
        )
        second = create_canary_proof(baseline, clock=lambda: NOW)
        installed = next(item for item in second["canaries"]
                         if item["name"] == "codex_installed")
        self.assertTrue(installed["drifted"])
        self.assertFalse(installed["status_matches"])
        self.assertNotIn(str(self.root), json.dumps(second))

    def test_canary_proof_rejects_stale_or_inconsistent_evidence(self):
        payload = json.loads(self.canary_proof.read_text(encoding="utf-8"))
        payload["checked_at"] = self._time(NOW - timedelta(minutes=16))
        self._private_json(self.canary_proof, payload)
        with self.assertRaisesRegex(AgentStatusError, "stale"):
            read_canary_proof(self.canary_proof, clock=lambda: NOW)
        payload["checked_at"] = self._time(NOW)
        payload["canaries"][0]["drifted"] = True
        self._private_json(self.canary_proof, payload)
        with self.assertRaisesRegex(AgentStatusError, "inconsistent"):
            read_canary_proof(self.canary_proof, clock=lambda: NOW)


if __name__ == "__main__":
    unittest.main()
