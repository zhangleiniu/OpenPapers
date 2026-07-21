import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.agent_operations import (
    AgentOperationError,
    mark_schedule_completed,
    recover_interrupted_event_date,
    update_monitor_configuration,
)
from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.local_service.agent_control import (
    initialize_agent_production_root,
    validate_agent_production_root,
)
from automation.local_service.production import (
    initialize_production_root,
    validate_production_root,
)
from automation.resend_notifications import recipient_fingerprints


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)


def recipient_fingerprint(address):
    return recipient_fingerprints((address,))[0]


class ScheduleOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state.sqlite3"
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            try:
                repository.register_event_date_target(
                    "icml", 2026, registered_at=NOW, lease=lease
                )
                repository.register_event_date_target(
                    "aaai", 2026, registered_at=NOW, lease=lease
                )
            finally:
                repository.release_lease(lease)

    def tearDown(self):
        self.temp.cleanup()

    def _repository(self, at=NOW):
        return ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: at
        )

    def _claim(self, repository, lease, venue_id="icml", year=2026):
        return repository.claim_event_date_attempt(
            venue_id, year,
            provider_name="fake", provider_model="fake", prompt_version="v1",
            claimed_at=NOW, lease=lease,
        )

    def test_recover_closes_one_interrupted_attempt_as_retry(self):
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            self._claim(repository, lease)
            repository.release_lease(lease)
        # The claim was never completed: the designed ambiguous-active state.

        dry = recover_interrupted_event_date(
            self.state, apply=False, clock=lambda: NOW + timedelta(hours=1)
        )
        self.assertEqual((dry["venue_id"], dry["applied"]), ("icml", False))
        with self._repository() as repository:
            self.assertEqual(
                repository.get_event_date_schedule("icml", 2026).status, "active"
            )

        applied = recover_interrupted_event_date(
            self.state, apply=True, clock=lambda: NOW + timedelta(hours=1)
        )
        self.assertEqual(applied["status"], "pending")
        with self._repository() as repository:
            record = repository.get_event_date_schedule("icml", 2026)
            self.assertEqual(record.status, "pending")
            history = repository.event_date_attempt_history("icml", 2026)
            self.assertEqual(history[-1].outcome, "retry")
            self.assertEqual(history[-1].failure_category, "operator_interrupted")

    def test_recover_refuses_ambiguous_multiplicity(self):
        with self.assertRaisesRegex(AgentOperationError, "exactly one"):
            recover_interrupted_event_date(self.state, apply=True)

    def test_mark_completed_flips_an_existing_agent_schedule(self):
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            claim = self._claim(repository, lease)
            repository.complete_event_date_success(
                claim,
                estimated_event_date="2026-07-01",
                estimated_at=NOW,
                next_check_at=NOW + timedelta(days=3),
                lease=lease,
            )
            repository.release_lease(lease)

        summary = mark_schedule_completed(
            self.state, "icml", 2026, apply=True, clock=lambda: NOW
        )
        self.assertEqual(summary["shape"], "complete_agent_schedule")
        self.assertEqual(summary["status"], "completed")
        with self._repository() as repository:
            self.assertEqual(
                repository.get_agent_schedule("icml", 2026).status, "completed"
            )
        replay = mark_schedule_completed(
            self.state, "icml", 2026, apply=True, clock=lambda: NOW
        )
        self.assertEqual(replay.get("already"), "completed")

    def test_mark_completed_terminalizes_a_dateless_target(self):
        with self.assertRaisesRegex(AgentOperationError, "--event-date"):
            mark_schedule_completed(self.state, "aaai", 2026, apply=True)

        summary = mark_schedule_completed(
            self.state, "aaai", 2026, event_date="2026-01-20",
            apply=True, clock=lambda: NOW,
        )
        self.assertEqual(summary["shape"], "terminalize_date_stage")
        self.assertEqual(summary["status"], "completed")
        with self._repository() as repository:
            event = repository.get_event_date_schedule("aaai", 2026)
            self.assertEqual(event.status, "scheduled")
            self.assertEqual(event.provider_name, "operator")
            agent = repository.get_agent_schedule("aaai", 2026)
            self.assertEqual(agent.status, "completed")
            # The terminalized target must never surface as due work again
            # (the untouched icml fixture row legitimately remains pending).
            due = repository.list_due_event_date_schedules(
                NOW + timedelta(days=400), limit=10
            )
            self.assertNotIn("aaai", {record.venue_id for record in due})

    def test_mark_completed_refuses_unknown_target(self):
        with self.assertRaisesRegex(AgentOperationError, "not a registered"):
            mark_schedule_completed(self.state, "uai", 2026, apply=True)

    def test_mark_completed_can_chain_the_successor_year(self):
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            claim = self._claim(repository, lease)
            repository.complete_event_date_success(
                claim, estimated_event_date="2026-07-01", estimated_at=NOW,
                next_check_at=NOW + timedelta(days=3), lease=lease,
            )
            repository.release_lease(lease)

        dry = mark_schedule_completed(
            self.state, "icml", 2026, apply=False,
            chain_next_year_interval=1, clock=lambda: NOW,
        )
        self.assertEqual(dry["chain_successor_year"], 2027)
        with self._repository() as repository:
            self.assertIsNone(repository.get_event_date_schedule("icml", 2027))

        applied = mark_schedule_completed(
            self.state, "icml", 2026, apply=True,
            chain_next_year_interval=1, clock=lambda: NOW,
        )
        self.assertEqual(applied["status"], "completed")
        with self._repository() as repository:
            self.assertIsNotNone(repository.get_event_date_schedule("icml", 2027))

    def test_mark_completed_without_interval_does_not_chain(self):
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            claim = self._claim(repository, lease)
            repository.complete_event_date_success(
                claim, estimated_event_date="2026-07-01", estimated_at=NOW,
                next_check_at=NOW + timedelta(days=3), lease=lease,
            )
            repository.release_lease(lease)

        summary = mark_schedule_completed(
            self.state, "icml", 2026, apply=True, clock=lambda: NOW,
        )
        self.assertNotIn("chain_successor_year", summary)
        with self._repository() as repository:
            self.assertIsNone(repository.get_event_date_schedule("icml", 2027))

    def test_mark_completed_rejects_a_non_positive_interval(self):
        with self.assertRaisesRegex(AgentOperationError, "positive integer"):
            mark_schedule_completed(
                self.state, "icml", 2026, apply=True,
                chain_next_year_interval=0,
            )


class MonitorConfigurationOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.internal = root / "internal"
        self.repository = root / "runtime"
        for path in (
            self.internal, self.internal / "control", self.internal / "monitor",
            self.repository / "automation" / "config",
        ):
            path.mkdir(mode=0o700, parents=True)
        self.registry = self.repository / "automation" / "conferences.json"
        self.registry.write_bytes(
            (ROOT / "automation" / "conferences.json").read_bytes()
        )
        targets = self.repository / "automation" / "config" / "agent_targets.v1.json"
        targets.write_text(json.dumps({
            "schema_version": 1,
            "targets": [{"venue_id": "icml", "year": 2026}],
        }, indent=2) + "\n", encoding="utf-8")
        self.agent_source = root / "agent-source"
        self.agent_source.mkdir(mode=0o700)
        for command in (
            ("git", "init", "-q"),
            ("git", "config", "user.name", "Fixture"),
            ("git", "config", "user.email", "fixture@example.invalid"),
        ):
            subprocess.run(command, cwd=self.agent_source, check=True)
        (self.agent_source / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(("git", "add", "README.md"), cwd=self.agent_source, check=True)
        subprocess.run(
            ("git", "commit", "-q", "-m", "fixture"),
            cwd=self.agent_source, check=True,
        )
        commit = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=self.agent_source,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        initialize_production_root(self.internal, {
            "schema_version": 1,
            # Deliberately stale: hash of different bytes and the wrong count.
            "registry_sha256": hashlib.sha256(b"stale registry").hexdigest(),
            "backup_sha256": "a" * 64,
            "remote_state_generation": "123456789",
            "expected_source_count": 6,
            "smtp_host": "smtp.example.test",
            "smtp_port": 465,
            "smtp_username": "openpapers",
            "email_from": "from@example.test",
            "email_to": "to@example.test",
        }, {
            "schema_version": 1,
            "openreview_username": "review-user",
            "openreview_password": "review-password",
            "smtp_password": "smtp-password",
        })
        initialize_agent_production_root(
            self.internal, self.repository,
            {
                "schema_version": 2,
                "mode": "agent_production_control",
                "external_effects_enabled": False,
                "agent_source_commit": commit,
                "agent_configuration": {
                    "schema_version": 2,
                    "targets_sha256": hashlib.sha256(
                        targets.read_bytes()
                    ).hexdigest(),
                    "gemini_project_id": "project-id",
                    "gemini_location": "global",
                    "gemini_model": "gemini-2.5-flash",
                    "monthly_date_lookup_limit": 3,
                    "codex_binary": "/usr/bin/false",
                    "codex_timeout_seconds": 60,
                    "codex_max_output_bytes": 64000,
                    "codex_max_changed_files": 100,
                    "default_not_ready_delay_hours": 12,
                    "minimum_retry_delay_hours": 1,
                    "max_suggested_retry_delay_days": 30,
                    "failure_backoff_hours": [2, 6, 24],
                    "max_consecutive_failures": 5,
                    "monthly_run_limit": 120,
                    "systemic_failure_threshold": 3,
                    "systemic_failure_window_hours": 6,
                    "systemic_circuit_delay_hours": 6,
                    "minimum_free_bytes": 10_000_000_000,
                    "retention_max_retained": 10,
                    "retention_max_age_days": 30,
                    "retention_max_removals_per_run": 2,
                    "resend_recipient_sha256": recipient_fingerprint(
                        "to@example.test"
                    ),
                },
            },
            {"schema_version": 2, "resend": None},
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_update_rewrites_registry_configuration(self):
        dry = update_monitor_configuration(
            self.internal, self.repository, apply=False
        )
        self.assertTrue(dry["changed"])
        self.assertEqual(dry["before"]["expected_source_count"], 6)
        self.assertEqual(dry["after"]["expected_source_count"], 18)
        # Dry run left everything untouched and still valid.
        validate_agent_production_root(self.internal, self.repository)
        self.assertEqual(
            validate_production_root(self.internal)[0].expected_source_count, 6
        )

        applied = update_monitor_configuration(
            self.internal, self.repository, apply=True
        )
        self.assertTrue(applied["validated"])
        configuration, _ = validate_production_root(self.internal)
        self.assertEqual(configuration.expected_source_count, 18)
        self.assertEqual(
            configuration.registry_sha256,
            hashlib.sha256(self.registry.read_bytes()).hexdigest(),
        )
        validate_agent_production_root(self.internal, self.repository)

        replay = update_monitor_configuration(
            self.internal, self.repository, apply=True
        )
        self.assertFalse(replay["changed"])


if __name__ == "__main__":
    unittest.main()
