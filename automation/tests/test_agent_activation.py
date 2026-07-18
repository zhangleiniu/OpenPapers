import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from automation.agent_activation import (
    AgentActivationError,
    ActivationReadiness,
    audit_external_effects_readiness,
    main,
    probe_local_service_loaded,
)
from automation.agent_credentials import prepare_agent_credential_context
from automation.control_state import CONTROL_SCHEMA_VERSION, ControlStateRepository
from automation.domain import Writer
from automation.local_service.agent_control import initialize_agent_production_root
from automation.local_service.production import initialize_production_root
from automation.resend_notifications import recipient_fingerprints


NOW = datetime(2026, 7, 16, 1, 45, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


class AgentActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.internal = self.root / "internal"
        self.repository = self.root / "runtime"
        self.external = self.root / "external"
        for path in (
            self.internal,
            self.internal / "control",
            self.internal / "monitor",
            self.repository / "automation" / "config",
            self.external,
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        targets = self.repository / "automation" / "config" / "agent_targets.v1.json"
        targets.write_text(json.dumps({
            "schema_version": 1,
            "targets": [{"venue_id": "icml", "year": 2026}],
        }, indent=2) + "\n", encoding="utf-8")
        self.source = self.external / "agent-source"
        self.source.mkdir(mode=0o700)
        subprocess.run(("git", "init", "-q"), cwd=self.source, check=True)
        subprocess.run(
            ("git", "config", "user.name", "Fixture"),
            cwd=self.source, check=True,
        )
        subprocess.run(
            ("git", "config", "user.email", "fixture@example.invalid"),
            cwd=self.source, check=True,
        )
        (self.source / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(("git", "add", "README.md"), cwd=self.source, check=True)
        subprocess.run(
            ("git", "commit", "-q", "-m", "fixture"),
            cwd=self.source, check=True,
        )
        commit = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=self.source,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        recipients = ("first@example.test", "second@example.test")
        agent = {
            "schema_version": 3,
            "targets_sha256": hashlib.sha256(targets.read_bytes()).hexdigest(),
            "gemini_project_id": "project-id",
            "gemini_location": "global",
            "gemini_model": "gemini-2.5-flash",
            "monthly_date_lookup_limit": 100,
            "codex_binary": "/usr/bin/false",
            "codex_timeout_seconds": 60,
            "codex_max_output_bytes": 64000,
            "codex_max_changed_files": 100,
            "default_not_ready_delay_hours": 12,
            "minimum_retry_delay_hours": 1,
            "max_suggested_retry_delay_days": 30,
            "failure_backoff_hours": [2, 6, 24, 72],
            "max_consecutive_failures": 5,
            "monthly_run_limit": 1000,
            "systemic_failure_threshold": 10,
            "systemic_failure_window_hours": 6,
            "systemic_circuit_delay_hours": 6,
            "minimum_free_bytes": 10_000_000_000,
            "retention_max_retained": 100,
            "retention_max_age_days": 90,
            "retention_max_removals_per_run": 5,
            "resend_recipient_sha256s": list(recipient_fingerprints(recipients)),
        }
        initialize_agent_production_root(
            self.internal,
            self.repository,
            {
                "schema_version": 2,
                "mode": "agent_production_control",
                "external_effects_enabled": False,
                "agent_source_commit": commit,
                "agent_configuration": agent,
            },
            {"schema_version": 3, "resend": {
                "api_key": "placeholder-key",
                "email_from": "from@example.test",
                "email_to": list(recipients),
            }},
        )
        credentials = prepare_agent_credential_context(self.internal)
        (credentials.codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
        credentials.google_adc.write_text("{}\n", encoding="utf-8")
        os.chmod(credentials.codex_home / "auth.json", 0o600)
        os.chmod(credentials.google_adc, 0o600)
        self.state = self.internal / "control" / "state.sqlite3"
        with ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW
        ):
            pass
        self.state.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def _audit(self, **overrides):
        arguments = {
            "internal_root": self.internal,
            "repository_root": self.repository,
            "execution_root": self.external,
            "state_path": self.state,
            "service_loaded": True,
            "expected_service_loaded": True,
            "disk_usage": lambda _: SimpleNamespace(free=20_000_000_000),
        }
        arguments.update(overrides)
        return audit_external_effects_readiness(**arguments)

    def test_readiness_audit_reports_only_safe_bounded_evidence(self):
        before_state = self.state.read_bytes()
        before_config = tuple(
            (self.internal / name).read_bytes() for name in (
                ".agent-production-config.v2.json",
                ".agent-production-secrets.v2.json",
                ".agent-production-control.v2.json",
            )
        )
        result = self._audit()

        self.assertTrue(result.ready)
        self.assertEqual(result.schema_version, CONTROL_SCHEMA_VERSION)
        self.assertEqual(result.recipient_count, 2)
        rendered = repr(result)
        self.assertNotIn("placeholder-key", rendered)
        self.assertNotIn("example.test", rendered)
        self.assertNotIn(str(self.root), rendered)
        self.assertEqual(self.state.read_bytes(), before_state)
        self.assertEqual(before_config, tuple(
            (self.internal / name).read_bytes() for name in (
                ".agent-production-config.v2.json",
                ".agent-production-secrets.v2.json",
                ".agent-production-control.v2.json",
            )
        ))

    def test_service_mismatch_blocks(self):
        with self.assertRaisesRegex(AgentActivationError, "service state"):
            self._audit(service_loaded=False)

    def test_credentials_allowlist_disk_and_source_defects_block(self):
        credentials = prepare_agent_credential_context(self.internal)
        (credentials.codex_home / "auth.json").unlink()
        with self.assertRaisesRegex(AgentActivationError, "credentials"):
            self._audit()
        (credentials.codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
        os.chmod(credentials.codex_home / "auth.json", 0o600)
        with patch(
            "automation.agent_activation.recipient_fingerprints",
            return_value=("f" * 64,),
        ):
            with self.assertRaisesRegex(AgentActivationError, "allowlist"):
                self._audit()
        with self.assertRaisesRegex(AgentActivationError, "free space"):
            self._audit(disk_usage=lambda _: SimpleNamespace(free=1))
        (self.source / "dirty").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(AgentActivationError, "agent source"):
            self._audit()

    def test_schema_or_active_state_blocks(self):
        audit = SimpleNamespace(
            schema_version=9,
            current_schema_version=10,
            quick_check_ok=True,
            owner_kind="local_control_plane",
            journal_mode="delete",
            active_event_date_attempts=0,
            active_agent_runs=0,
            in_flight_reports=0,
            migration_ready=True,
        )
        with patch("automation.agent_activation.audit_control_state", return_value=audit):
            with self.assertRaisesRegex(AgentActivationError, "control state"):
                self._audit()
        audit.schema_version = 10
        audit.active_agent_runs = 1
        with patch("automation.agent_activation.audit_control_state", return_value=audit):
            with self.assertRaisesRegex(AgentActivationError, "control state"):
                self._audit()

    def test_service_probe_distinguishes_stopped_from_probe_failure(self):
        loaded = Mock(return_value=SimpleNamespace(
            returncode=0, stdout="service", stderr=""
        ))
        self.assertTrue(probe_local_service_loaded(runner=loaded))
        self.assertEqual(
            loaded.call_args.args[0],
            ("/bin/launchctl", "print", "system/org.openpapers.local-control"),
        )
        missing = Mock(return_value=SimpleNamespace(
            returncode=113,
            stdout="",
            stderr=("Bad request.\nCould not find service "
                    "\"org.openpapers.local-control\" in domain for system\n"),
        ))
        self.assertFalse(probe_local_service_loaded(runner=missing))
        denied = Mock(return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="permission denied"
        ))
        with self.assertRaisesRegex(AgentActivationError, "probe failed"):
            probe_local_service_loaded(runner=denied)

    def test_activation_cli_requires_exact_authority_before_audit(self):
        audit = Mock()
        activate = Mock()
        with patch("automation.agent_activation.audit_external_effects_readiness", audit), \
                patch("automation.agent_activation.activate_agent_production_root", activate), \
                redirect_stdout(Mock()):
            result = main([
                    "activate",
                    "--internal-root", str(self.internal),
                    "--repository-root", str(self.repository),
                    "--execution-root", str(self.external),
                    "--state", str(self.state),
                    "--backup-root", str(self.root / "backup"),
                    "--confirm-service-stopped",
                ])
        self.assertEqual(result, 2)
        audit.assert_not_called()
        activate.assert_not_called()

    def test_authorized_activation_cli_requires_stopped_probe_and_audit(self):
        readiness = ActivationReadiness(
            True, False, 10, True, 0, 0, 0, True, True, 2,
            True, True, False,
        )
        audit = Mock(return_value=readiness)
        activate = Mock()
        with patch("automation.agent_activation.probe_local_service_loaded", return_value=False), \
                patch("automation.agent_activation.audit_external_effects_readiness", audit), \
                patch("automation.agent_activation.activate_agent_production_root", activate), \
                redirect_stdout(Mock()):
            result = main([
                "activate",
                "--internal-root", str(self.internal),
                "--repository-root", str(self.repository),
                "--execution-root", str(self.external),
                "--state", str(self.state),
                "--backup-root", str(self.root / "backup"),
                "--confirm-service-stopped",
                "--authorize-external-effects-activation",
            ])
        self.assertEqual(result, 0)
        audit.assert_called_once()
        self.assertFalse(audit.call_args.kwargs["service_loaded"])
        self.assertFalse(audit.call_args.kwargs["expected_service_loaded"])
        activate.assert_called_once_with(
            self.internal, self.repository, self.root / "backup"
        )


if __name__ == "__main__":
    unittest.main()
