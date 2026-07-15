import hashlib
import json
import tempfile
import unittest
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from automation.local_service.agent_control import (
    AGENT_PRODUCTION_MARKER,
    InstalledAgentProductionEffect,
    initialize_agent_production_root,
    validate_agent_production_root,
)
from automation.local_service.production import initialize_production_root
from automation.local_service.service import LocalEffectOutcome, LocalEffectStatus
from automation.resend_notifications import recipient_fingerprint


NOW = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


class Baseline:
    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return LocalEffectOutcome(LocalEffectStatus.NO_DUE_WORK, 0)


class Agent:
    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return LocalEffectOutcome(LocalEffectStatus.COMPLETED, 1)


class AgentControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.internal = self.root / "internal"
        self.repository = self.root / "runtime"
        self.execution = self.root / "external"
        for path in (
            self.internal,
            self.internal / "control",
            self.internal / "monitor",
            self.repository / "automation" / "config",
            self.execution,
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.agent_source = self.execution / "agent-source"
        self.agent_source.mkdir(mode=0o700)
        subprocess.run(("git", "init", "-q"), cwd=self.agent_source, check=True)
        subprocess.run(
            ("git", "config", "user.name", "Fixture"),
            cwd=self.agent_source, check=True,
        )
        subprocess.run(
            ("git", "config", "user.email", "fixture@example.invalid"),
            cwd=self.agent_source, check=True,
        )
        (self.agent_source / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(("git", "add", "README.md"), cwd=self.agent_source, check=True)
        subprocess.run(
            ("git", "commit", "-q", "-m", "fixture"),
            cwd=self.agent_source, check=True,
        )
        self.agent_source_commit = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=self.agent_source,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        targets = self.repository / "automation" / "config" / "agent_targets.v1.json"
        targets.write_text(json.dumps({
            "schema_version": 1,
            "targets": [{"venue_id": "icml", "year": 2026}],
        }, indent=2) + "\n", encoding="utf-8")
        registry = ROOT / "automation" / "conferences.json"
        initialize_production_root(self.internal, {
            "schema_version": 1,
            "registry_sha256": hashlib.sha256(registry.read_bytes()).hexdigest(),
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
        self.agent_payload = {
            "schema_version": 2,
            "targets_sha256": hashlib.sha256(targets.read_bytes()).hexdigest(),
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
            "failure_backoff_hours": [2, 6, 24, 72],
            "max_consecutive_failures": 5,
            "monthly_run_limit": 120,
            "systemic_failure_threshold": 3,
            "systemic_failure_window_hours": 6,
            "systemic_circuit_delay_hours": 6,
            "minimum_free_bytes": 10_000_000_000,
            "retention_max_retained": 10,
            "retention_max_age_days": 30,
            "retention_max_removals_per_run": 2,
            "resend_recipient_sha256": recipient_fingerprint("to@example.test"),
        }

    def tearDown(self):
        self.temp.cleanup()

    def _configuration(self, *, enabled=False):
        return {
            "schema_version": 2,
            "mode": "agent_production_control",
            "external_effects_enabled": enabled,
            "agent_source_commit": self.agent_source_commit,
            "agent_configuration": self.agent_payload,
        }

    def _run_kwargs(self):
        return {
            "state_path": self.internal / "control" / "state.sqlite3",
            "execution_root": self.execution,
            "scheduled_for": NOW,
            "observed_at": NOW,
        }

    def test_disabled_install_validates_and_never_builds_live_adapters(self):
        initialize_agent_production_root(
            self.internal, self.repository, self._configuration(),
            {"schema_version": 2, "resend": None},
        )
        configuration, secrets = validate_agent_production_root(
            self.internal, self.repository
        )
        self.assertFalse(configuration.external_effects_enabled)
        self.assertIsNone(secrets)
        baseline = Baseline()
        builds = []
        effect = InstalledAgentProductionEffect(
            repository_root=self.repository,
            baseline=baseline,
            live_builder=lambda **kwargs: builds.append(kwargs),
        )
        result = effect.run(**self._run_kwargs())
        self.assertEqual(result.status, LocalEffectStatus.NO_DUE_WORK)
        self.assertEqual(len(baseline.calls), 1)
        self.assertEqual(builds, [])

    def test_enabled_install_requires_secrets_and_runs_composed_effect(self):
        with self.assertRaisesRegex(ValueError, "secrets are missing"):
            initialize_agent_production_root(
                self.internal, self.repository, self._configuration(enabled=True),
                {"schema_version": 2, "resend": None},
            )
        secrets = {
            "schema_version": 2,
            "resend": {
                "api_key": "placeholder-key",
                "email_from": "from@example.test",
                "email_to": "to@example.test",
            },
        }
        initialize_agent_production_root(
            self.internal, self.repository, self._configuration(enabled=True), secrets,
        )
        baseline = Baseline()
        agent = Agent()
        builds = []

        def build(**kwargs):
            builds.append(kwargs)
            return agent

        effect = InstalledAgentProductionEffect(
            repository_root=self.repository,
            baseline=baseline,
            live_builder=build,
        )
        result = effect.run(**self._run_kwargs())
        self.assertEqual(result.status, LocalEffectStatus.COMPLETED)
        self.assertEqual(result.selection_count, 1)
        self.assertEqual(len(baseline.calls), 1)
        self.assertEqual(len(builds), 1)
        self.assertEqual(
            Path(builds[0]["repository_root"]), self.agent_source.resolve()
        )
        self.assertEqual(len(agent.calls), 1)
        self.assertNotIn("placeholder-key", repr(builds[0]["secrets"]))

    def test_marker_binds_baseline_and_agent_files(self):
        initialize_agent_production_root(
            self.internal, self.repository, self._configuration(),
            {"schema_version": 2, "resend": None},
        )
        marker = self.internal / AGENT_PRODUCTION_MARKER
        marker.write_text('{"schema_version":2}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "marker is invalid"):
            validate_agent_production_root(self.internal, self.repository)


if __name__ == "__main__":
    unittest.main()
