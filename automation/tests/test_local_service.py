import ast
import json
import os
import plistlib
import sqlite3
import tempfile
import unittest
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from automation.local_service import (
    LOCAL_SERVICE_LABEL,
    HealthCheckCode,
    HealthCheckName,
    HealthCheckStatus,
    LocalEffectOutcome,
    LocalEffectStatus,
    LocalMountProbe,
    LocalServiceConfig,
    LocalServiceRunCode,
    LocalServiceRunStatus,
    PRODUCTION_MARKER,
    ProductionMonitorEffect,
    build_rollback_scope,
    collect_local_service_health,
    render_launchdaemon,
    render_production_launchdaemon,
    run_local_service_once,
    initialize_production_root,
    validate_production_root,
)
from automation.local_service.__main__ import main


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "automation" / "local_service"
NOW = datetime(2026, 7, 14, 14, 34, tzinfo=timezone.utc)


class FakeVolumeProbe:
    def __init__(self, available=True, error=None):
        self.available = available
        self.error = error
        self.calls = []

    def is_available(self, root):
        self.calls.append(root)
        if self.error is not None:
            raise self.error
        return self.available


class FakeEffect:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome or LocalEffectOutcome(
            status=LocalEffectStatus.COMPLETED,
            selection_count=1,
        )
        self.error = error
        self.calls = []

    def run(self, *, state_path, execution_root, scheduled_for, observed_at):
        self.calls.append(
            (state_path, execution_root, scheduled_for, observed_at)
        )
        if self.error is not None:
            raise self.error
        return self.outcome


class FakeNotifier:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def send(self, event, *, configuration, password):
        self.calls.append((event, configuration, password))
        if self.error is not None:
            raise self.error


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class LocalServiceFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        self.repository = root / "repository"
        self.python = root / "python3"
        self.internal = root / "internal"
        self.external = root / "external-volume"
        (self.repository / "automation" / "local_service").mkdir(parents=True)
        for marker in (
            self.repository / "main.py",
            self.repository / "automation" / "control_state.py",
            self.repository / "automation" / "local_service" / "__main__.py",
        ):
            marker.touch()
        self.python.touch()
        self.python.chmod(0o700)
        self.internal.mkdir(mode=0o700)
        (self.internal / "control").mkdir(mode=0o700)
        self.external.mkdir()
        self.config = LocalServiceConfig(
            repository_root=self.repository,
            python_executable=self.python,
            internal_root=self.internal,
            external_volume_root=self.external,
            role_user="openpapers-test",
            schedule_minute=17,
            record_limit=2,
        )

    def tearDown(self):
        self.temporary.cleanup()


class LocalServiceConfigurationTests(LocalServiceFixture):
    def test_internal_paths_are_fixed_and_disjoint_from_execution_data(self):
        self.assertEqual(
            self.config.state_path,
            self.internal / "control" / "state.sqlite3",
        )
        self.assertEqual(
            self.config.health_path,
            self.internal / "service" / "health.v1.json",
        )
        self.assertEqual(
            self.config.run_records_path,
            self.internal / "service" / "runs.v1.json",
        )
        serialized = json.dumps(self.config.public_summary(), sort_keys=True)
        self.assertNotIn(str(self.repository), serialized)
        self.assertNotIn(str(self.internal), serialized)
        self.assertNotIn(str(self.external), serialized)
        self.assertNotIn(self.config.role_user, serialized)

    def test_relative_overlapping_and_unbounded_configuration_is_rejected(self):
        valid = {
            "repository_root": self.repository,
            "python_executable": self.python,
            "internal_root": self.internal,
            "external_volume_root": self.external,
            "role_user": "openpapers-test",
        }
        cases = (
            ({"repository_root": Path("relative")}, "absolute"),
            ({"repository_root": self.repository / ".." / "repository"}, "normalized"),
            ({"internal_root": self.external / "control"}, "disjoint"),
            ({"external_volume_root": self.internal / "data"}, "disjoint"),
            ({"role_user": "bad user"}, "role user"),
            ({"role_user": "root"}, "role user"),
            ({"schedule_minute": 60}, "schedule minute"),
            ({"record_limit": 0}, "record limit"),
            ({"record_limit": 257}, "record limit"),
        )
        for override, message in cases:
            with self.subTest(override=override), self.assertRaisesRegex(
                (TypeError, ValueError), message
            ):
                LocalServiceConfig(**(valid | override))


class LocalServiceHealthAndRunTests(LocalServiceFixture):
    def test_repository_health_uses_current_control_entrypoints(self):
        probe = FakeVolumeProbe()
        ready = collect_local_service_health(
            self.config, probe, platform_name="Darwin"
        )
        self.assertTrue(ready.ready)

        (self.repository / "automation" / "local_service" / "__main__.py").unlink()
        blocked = collect_local_service_health(
            self.config, probe, platform_name="Darwin"
        )
        repository = next(
            item for item in blocked.checks
            if item.name is HealthCheckName.REPOSITORY
        )
        self.assertEqual(repository.status, HealthCheckStatus.FAIL)
        self.assertEqual(repository.code, HealthCheckCode.INVALID_REPOSITORY)

    def test_concrete_probe_accepts_private_directory_on_non_root_mount(self):
        probe = LocalMountProbe()
        mount_root = self.external.parent
        with patch(
            "automation.local_service.service.os.path.ismount",
            side_effect=lambda path: Path(path) == mount_root,
        ):
            self.assertTrue(probe.is_available(self.external))

        with patch(
            "automation.local_service.service.os.path.ismount",
            side_effect=lambda path: Path(path) == Path("/"),
        ):
            self.assertFalse(probe.is_available(self.external))

        target = self.internal.parent / "mounted-target"
        nested = target / "private"
        nested.mkdir(parents=True)
        linked_parent = self.internal.parent / "linked-mount"
        linked_parent.symlink_to(target, target_is_directory=True)
        with patch(
            "automation.local_service.service.os.path.ismount",
            side_effect=lambda path: Path(path) == target,
        ):
            self.assertFalse(probe.is_available(linked_parent / "private"))

    def test_health_is_bounded_and_does_not_report_paths_or_probe_text(self):
        probe = FakeVolumeProbe(error=RuntimeError("token=do-not-retain"))

        report = collect_local_service_health(
            self.config,
            probe,
            platform_name="Darwin",
        )

        self.assertFalse(report.ready)
        self.assertIn(
            HealthCheckCode.EXTERNAL_VOLUME_PROBE_FAILED,
            {item.code for item in report.checks},
        )
        serialized = json.dumps(report.as_dict(), sort_keys=True)
        for forbidden in (
            str(self.repository),
            str(self.internal),
            str(self.external),
            "do-not-retain",
            "openpapers-test",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_missing_volume_fails_before_effect_or_control_state(self):
        effect = FakeEffect()
        probe = FakeVolumeProbe(available=False)

        report = run_local_service_once(
            self.config,
            effect=effect,
            volume_probe=probe,
            clock=MutableClock(),
            platform_name="Darwin",
        )

        self.assertEqual(report.status, LocalServiceRunStatus.BLOCKED)
        self.assertEqual(report.code, LocalServiceRunCode.HEALTH_FAILED)
        self.assertEqual(effect.calls, [])
        self.assertEqual(probe.calls, [self.external])
        self.assertFalse(self.config.state_path.exists())
        retained = json.loads(
            self.config.run_records_path.read_text(encoding="utf-8")
        )
        self.assertEqual(len(retained["records"]), 1)
        self.assertEqual(retained["records"][0]["code"], "health_failed")

    def test_missing_or_symlinked_control_directory_blocks_the_effect(self):
        control_root = self.config.control_root
        control_root.rmdir()
        effect = FakeEffect()

        missing = run_local_service_once(
            self.config,
            effect=effect,
            volume_probe=FakeVolumeProbe(),
            clock=MutableClock(),
            platform_name="Darwin",
        )
        self.assertEqual(missing.code, LocalServiceRunCode.HEALTH_FAILED)
        self.assertEqual(effect.calls, [])

        target = self.internal / "redirected-control"
        target.mkdir(mode=0o700)
        control_root.symlink_to(target, target_is_directory=True)
        linked = run_local_service_once(
            self.config,
            effect=effect,
            volume_probe=FakeVolumeProbe(),
            clock=MutableClock(NOW + timedelta(hours=1)),
            platform_name="Darwin",
        )
        self.assertEqual(linked.code, LocalServiceRunCode.HEALTH_FAILED)
        self.assertEqual(effect.calls, [])
        self.assertFalse(target.joinpath("state.sqlite3").exists())

    def test_healthy_fake_run_uses_fixed_paths_slot_and_bounded_records(self):
        effect = FakeEffect()
        probe = FakeVolumeProbe()
        clock = MutableClock()

        first = run_local_service_once(
            self.config,
            effect=effect,
            volume_probe=probe,
            clock=clock,
            platform_name="Darwin",
        )
        clock.value += timedelta(hours=1)
        second = run_local_service_once(
            self.config,
            effect=effect,
            volume_probe=probe,
            clock=clock,
            platform_name="Darwin",
        )
        clock.value += timedelta(hours=1)
        third = run_local_service_once(
            self.config,
            effect=effect,
            volume_probe=probe,
            clock=clock,
            platform_name="Darwin",
        )

        self.assertEqual(first.status, LocalServiceRunStatus.COMPLETED)
        self.assertEqual(second.code, LocalServiceRunCode.COMPLETED)
        self.assertEqual(third.selection_count, 1)
        self.assertEqual(len(effect.calls), 3)
        state_path, execution_root, scheduled_for, observed_at = effect.calls[0]
        self.assertEqual(state_path, self.config.state_path)
        self.assertEqual(execution_root, self.external)
        self.assertEqual(
            scheduled_for,
            datetime(2026, 7, 14, 14, 17, tzinfo=timezone.utc),
        )
        self.assertEqual(observed_at, NOW)
        self.assertFalse(self.config.state_path.exists())
        retained = json.loads(
            self.config.run_records_path.read_text(encoding="utf-8")
        )
        self.assertEqual(retained["schema_version"], 1)
        self.assertEqual(len(retained["records"]), 2)
        self.assertEqual(
            [item["observed_at"] for item in retained["records"]],
            ["2026-07-14T15:34:00Z", "2026-07-14T16:34:00Z"],
        )
        health = json.loads(self.config.health_path.read_text(encoding="utf-8"))
        self.assertTrue(health["ready"])

    def test_corrupt_records_and_effect_failure_are_bounded_and_closed(self):
        probe = FakeVolumeProbe()
        first_effect = FakeEffect()
        run_local_service_once(
            self.config,
            effect=first_effect,
            volume_probe=probe,
            clock=MutableClock(),
            platform_name="Darwin",
        )
        self.config.run_records_path.write_text("not-json", encoding="utf-8")
        second_effect = FakeEffect()

        corrupt = run_local_service_once(
            self.config,
            effect=second_effect,
            volume_probe=probe,
            clock=MutableClock(NOW + timedelta(hours=1)),
            platform_name="Darwin",
        )

        self.assertEqual(corrupt.status, LocalServiceRunStatus.BLOCKED)
        self.assertEqual(corrupt.code, LocalServiceRunCode.RECORDS_UNAVAILABLE)
        self.assertEqual(second_effect.calls, [])

        retained = {
            "schema_version": 1,
            "records": [
                {
                    "status": "arbitrary",
                    "code": "token=do-not-retain",
                    "scheduled_for": "2026-07-14T14:17:00Z",
                    "observed_at": "2026-07-14T14:34:00Z",
                    "selection_count": 0,
                    "health_ready": True,
                }
            ],
        }
        self.config.run_records_path.write_text(
            json.dumps(retained), encoding="utf-8"
        )
        third_effect = FakeEffect()
        arbitrary = run_local_service_once(
            self.config,
            effect=third_effect,
            volume_probe=probe,
            clock=MutableClock(NOW + timedelta(hours=2)),
            platform_name="Darwin",
        )
        self.assertEqual(arbitrary.code, LocalServiceRunCode.RECORDS_UNAVAILABLE)
        self.assertEqual(third_effect.calls, [])

        self.config.run_records_path.unlink()
        failing = run_local_service_once(
            self.config,
            effect=FakeEffect(error=RuntimeError("password=do-not-retain")),
            volume_probe=probe,
            clock=MutableClock(NOW + timedelta(hours=3)),
            platform_name="Darwin",
        )
        self.assertEqual(failing.status, LocalServiceRunStatus.FAILED)
        self.assertEqual(failing.code, LocalServiceRunCode.EFFECT_FAILED)
        serialized = self.config.run_records_path.read_text(encoding="utf-8")
        self.assertNotIn("do-not-retain", serialized)
        self.assertNotIn(str(self.internal), serialized)


class FailureVisibilityTests(LocalServiceFixture):
    def test_category_keeps_control_plane_messages_and_masks_others(self):
        from automation.local_service.production import ProductionControlError
        from automation.local_service.service import failure_category_from_exception

        self.assertEqual(
            failure_category_from_exception(
                ProductionControlError("production registry fingerprint changed")
            ),
            "ProductionControlError: production registry fingerprint changed",
        )
        # Non-automation exceptions contribute only their class name, so an
        # OSError can never place a filesystem path into the records.
        self.assertEqual(
            failure_category_from_exception(
                OSError("/private/secret/path is unavailable")
            ),
            "OSError",
        )
        noisy = ProductionControlError("line\nbreaks\tand\x00controls" + "x" * 300)
        category = failure_category_from_exception(noisy)
        self.assertLessEqual(len(category), 200)
        self.assertNotIn("\n", category)
        self.assertNotIn("\x00", category)

    def test_failed_run_record_carries_category_and_legacy_records_still_read(self):
        from automation.local_service.production import ProductionControlError
        from automation.local_service.records import read_service_run_records

        probe = FakeVolumeProbe()
        report = run_local_service_once(
            self.config,
            effect=FakeEffect(
                error=ProductionControlError("restored monitor state is incomplete")
            ),
            volume_probe=probe,
            clock=MutableClock(),
            platform_name="Darwin",
        )
        self.assertEqual(report.code, LocalServiceRunCode.EFFECT_FAILED)
        self.assertEqual(
            report.failure_category,
            "ProductionControlError: restored monitor state is incomplete",
        )
        records = read_service_run_records(self.config.run_records_path, limit=3)
        self.assertEqual(
            records[-1]["failure_category"],
            "ProductionControlError: restored monitor state is incomplete",
        )
        # A legacy record without the optional key must remain readable.
        document = json.loads(self.config.run_records_path.read_text())
        legacy = dict(records[-1])
        legacy.pop("failure_category")
        document["records"].append(legacy)
        self.config.run_records_path.write_text(json.dumps(document))
        reread = read_service_run_records(self.config.run_records_path, limit=3)
        self.assertNotIn("failure_category", reread[-1])

    def test_alert_thresholds_fire_at_three_then_daily(self):
        from automation.local_service.production import (
            consecutive_wake_failures,
            should_alert_wake_failures,
        )

        failed = {"status": "failed"}
        ok = {"status": "completed"}
        self.assertEqual(consecutive_wake_failures([ok, failed, failed]), 2)
        self.assertEqual(consecutive_wake_failures([failed, ok]), 0)
        self.assertEqual(consecutive_wake_failures([failed] * 5), 5)

        fired = [n for n in range(1, 60) if should_alert_wake_failures(n)]
        self.assertEqual(fired, [3, 27, 51])


class LocalServiceLaunchdAndCommandTests(LocalServiceFixture):
    def test_launchdaemon_is_fixed_low_impact_and_credential_free(self):
        rendered = render_launchdaemon(self.config)
        self.assertEqual(rendered, render_launchdaemon(self.config))
        document = plistlib.loads(rendered)

        self.assertEqual(document["Label"], LOCAL_SERVICE_LABEL)
        self.assertEqual(document["UserName"], "openpapers-test")
        self.assertNotIn("GroupName", document)
        self.assertEqual(document["WorkingDirectory"], str(self.repository))
        self.assertEqual(
            document["ProgramArguments"][:3],
            [str(self.python), "-m", "automation.local_service"],
        )
        self.assertIn(str(self.internal), document["ProgramArguments"])
        self.assertIn(str(self.external), document["ProgramArguments"])
        self.assertTrue(document["RunAtLoad"])
        self.assertEqual(document["StartCalendarInterval"], {"Minute": 17})
        self.assertEqual(document["Umask"], 0o77)
        self.assertEqual(document["ProcessType"], "Background")
        self.assertTrue(document["LowPriorityIO"])
        self.assertGreater(document["Nice"], 0)
        self.assertEqual(document["StandardOutPath"], "/dev/null")
        self.assertEqual(document["StandardErrorPath"], "/dev/null")
        for absent in (
            "KeepAlive",
            "EnvironmentVariables",
            "Sockets",
            "MachServices",
        ):
            self.assertNotIn(absent, document)
        lowered = rendered.decode("utf-8").lower()
        for forbidden in (
            ".env",
            "api_key",
            "authorization",
            "prefect",
            "google_application_credentials",
            "resend",
            "codex",
            "/bin/sh",
            "launchctl",
        ):
            self.assertNotIn(forbidden, lowered)

        production_document = plistlib.loads(
            render_production_launchdaemon(self.config)
        )
        self.assertEqual(
            production_document["ProgramArguments"][:-1],
            document["ProgramArguments"],
        )
        self.assertEqual(
            production_document["ProgramArguments"][-1], "--production-control"
        )

    def test_rollback_scope_names_only_openpapers_and_preserves_data(self):
        scope = build_rollback_scope(self.config)

        self.assertEqual(scope.label, LOCAL_SERVICE_LABEL)
        self.assertEqual(scope.domain_target, f"system/{LOCAL_SERVICE_LABEL}")
        self.assertEqual(
            scope.plist_path,
            Path("/Library/LaunchDaemons") / f"{LOCAL_SERVICE_LABEL}.plist",
        )
        self.assertEqual(scope.removable_paths, (scope.plist_path,))
        self.assertEqual(
            scope.preserved_paths,
            (self.internal, self.repository, self.external),
        )
        self.assertTrue(scope.matches_label(LOCAL_SERVICE_LABEL))
        self.assertFalse(scope.matches_label("org.example.mustcite"))
        self.assertFalse(scope.may_remove(self.internal))
        self.assertFalse(scope.may_remove(self.external))

    def test_cli_without_injected_effect_fails_closed_and_reports_bounded_json(self):
        args = [
            "--repository-root", str(self.repository),
            "--python-executable", str(self.python),
            "--internal-root", str(self.internal),
            "--external-volume-root", str(self.external),
            "--role-user", "openpapers-test",
            "--schedule-minute", "17",
            "--record-limit", "2",
        ]
        with patch("builtins.print") as printed:
            code = main(
                args,
                volume_probe=FakeVolumeProbe(),
                clock=MutableClock(),
                platform_name="Darwin",
            )

        self.assertEqual(code, 3)
        output = printed.call_args.args[0]
        self.assertIn('"code": "effect_unconfigured"', output)
        self.assertNotIn(str(self.internal), output)
        self.assertFalse(self.config.state_path.exists())

    def test_package_has_no_network_or_effect_adapter_dependency(self):
        imported = set()
        source = ""
        for name in ("service.py", "records.py", "launchd.py"):
            module = PACKAGE / name
            text = module.read_text(encoding="utf-8")
            source += text
            tree = ast.parse(text)
            imported.update(
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            )
        for forbidden in (
            "prefect",
            "requests",
            "urllib",
            "google",
            "resend",
            "subprocess",
            "automation.discovery",
            "automation.verification",
            "automation.notifications",
            "automation.job_queue",
            "automation.job_results",
        ):
            self.assertNotIn(forbidden, imported)
            self.assertNotIn(forbidden, source)
        self.assertNotIn("getenv", source)
        self.assertNotIn("launchctl", source)


class ProductionControlTests(LocalServiceFixture):
    def setUp(self):
        super().setUp()
        (self.internal / "monitor").mkdir(mode=0o700)
        self.registry = ROOT / "automation" / "conferences.json"
        self.configuration = {
            "schema_version": 1,
            "registry_sha256": sha256(self.registry.read_bytes()).hexdigest(),
            "backup_sha256": "a" * 64,
            "remote_state_generation": "123456789",
            "expected_source_count": 18,
            "smtp_host": "smtp.example.test",
            "smtp_port": 465,
            "smtp_username": "openpapers",
            "email_from": "from@example.test",
            "email_to": "to@example.test",
        }
        self.secrets = {
            "schema_version": 1,
            "openreview_username": "review-user",
            "openreview_password": "review-password",
            "smtp_password": "smtp-password",
        }
        initialize_production_root(
            self.internal, self.configuration, self.secrets
        )
        self.monitor_state = self.internal / "monitor" / "state.sqlite3"
        with sqlite3.connect(self.monitor_state) as connection:
            connection.execute(
                "CREATE TABLE source_state (venue TEXT, year INTEGER, "
                "source_key TEXT, checked_at TEXT, status TEXT, "
                "content_hash TEXT, item_count INTEGER, detail TEXT, "
                "snapshot_path TEXT)"
            )
            connection.executemany(
                "INSERT INTO source_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "icml", 2026, f"source:{index}",
                        "2026-07-14T12:00:00Z", "available", "hash", index,
                        "", "snapshot",
                    )
                    for index in range(18)
                ],
            )
        self.monitor_state.chmod(0o600)

    def _events(self, *, changed=True, error=False):
        events = []
        for index in range(18):
            events.append(
                {
                    "venue": "icml",
                    "year": 2026,
                    "source_key": f"source:{index}",
                    "status": "error" if error and index == 0 else "available",
                    "item_count": index,
                    "detail": "bounded",
                    "snapshot_path": str(self.internal / "monitor" / "private.html"),
                    "changed": changed and index == 0,
                }
            )
        return events

    def test_private_configuration_is_exact_and_shadow_conflicts(self):
        configuration, secrets = validate_production_root(self.internal)
        self.assertEqual(configuration.expected_source_count, 18)
        self.assertEqual(secrets.smtp_password, "smtp-password")
        marker = self.internal / PRODUCTION_MARKER
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        initialize_production_root(
            self.internal, self.configuration, self.secrets
        )

        marker.write_text('{"mode":"wrong"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "marker is invalid"):
            validate_production_root(self.internal)

        marker.unlink()
        (self.internal / ".isolated-shadow.v1.json").write_text(
            "{}\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "cannot coexist"):
            initialize_production_root(
                self.internal, self.configuration, self.secrets
            )

    def test_wake_failure_alert_uses_validated_config_and_bounded_content(self):
        from automation.local_service.production import send_wake_failure_alert

        notifier = FakeNotifier()
        send_wake_failure_alert(
            self.internal,
            consecutive=3,
            latest_record={
                "scheduled_for": "2026-07-17T14:17:00Z",
                "failure_category": "ProductionControlError: registry changed",
            },
            notifier=notifier,
        )
        (event, configuration, password), = notifier.calls
        self.assertEqual(event["status"], "error")
        self.assertEqual(event["item_count"], 3)
        self.assertIn("3 consecutive failed wakes", event["detail"])
        self.assertIn("ProductionControlError", event["detail"])
        self.assertEqual(password, "smtp-password")
        serialized = json.dumps(event)
        self.assertNotIn(str(self.internal), serialized)
        self.assertNotIn("smtp-password", serialized)

    def test_monitor_notification_and_exact_replay(self):
        monitor_calls = []
        notifier = FakeNotifier()

        def monitor(registry_path, state_path):
            monitor_calls.append((registry_path, state_path))
            self.assertEqual(os.environ["OPENREVIEW_USERNAME"], "review-user")
            return self._events()

        effect = ProductionMonitorEffect(
            repository_root=ROOT,
            monitor=monitor,
            notifier=notifier,
        )
        previous_username = os.environ.get("OPENREVIEW_USERNAME")
        first = effect.run(
            state_path=self.config.state_path,
            execution_root=self.external,
            scheduled_for=NOW.replace(minute=17),
            observed_at=NOW,
        )
        replay = effect.run(
            state_path=self.config.state_path,
            execution_root=self.external,
            scheduled_for=NOW.replace(minute=17),
            observed_at=NOW,
        )

        self.assertEqual(first.status, LocalEffectStatus.NO_DUE_WORK)
        self.assertEqual(replay.status, LocalEffectStatus.NO_DUE_WORK)
        self.assertEqual(len(monitor_calls), 1)
        self.assertEqual(monitor_calls[0][1], self.monitor_state)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0][2], "smtp-password")
        self.assertEqual(os.environ.get("OPENREVIEW_USERNAME"), previous_username)
        journal = self.internal / "monitor" / "production-wakeups.sqlite3"
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(journal) as connection:
            hints = connection.execute(
                "SELECT venue_id, year, status FROM production_source_hint"
            ).fetchall()
        self.assertEqual(hints, [("icml", 2026, "pending")])

    def test_monitor_waits_for_daily_chicago_slot(self):
        monitor_calls = []
        effect = ProductionMonitorEffect(
            repository_root=ROOT,
            monitor=lambda *args: monitor_calls.append(args) or self._events(),
            notifier=FakeNotifier(),
        )
        before_daily_slot = datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)
        result = effect.run(
            state_path=self.config.state_path,
            execution_root=self.external,
            scheduled_for=before_daily_slot.replace(minute=17),
            observed_at=before_daily_slot,
        )
        self.assertEqual(result.status, LocalEffectStatus.NO_DUE_WORK)
        self.assertEqual(monitor_calls, [])

    def test_failure_retains_ambiguity_and_blocks_effect_replay(self):
        calls = []

        def monitor(registry_path, state_path):
            calls.append((registry_path, state_path))
            return self._events(error=True)

        effect = ProductionMonitorEffect(
            repository_root=ROOT,
            monitor=monitor,
            notifier=FakeNotifier(),
        )
        with self.assertRaisesRegex(ValueError, "source errors"):
            effect.run(
                state_path=self.config.state_path,
                execution_root=self.external,
                scheduled_for=NOW.replace(minute=17),
                observed_at=NOW,
            )
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            effect.run(
                state_path=self.config.state_path,
                execution_root=self.external,
                scheduled_for=NOW.replace(minute=17),
                observed_at=NOW,
            )
        self.assertEqual(len(calls), 1)

    def test_incomplete_restored_state_fails_before_effect_or_journal(self):
        with sqlite3.connect(self.monitor_state) as connection:
            connection.execute("DELETE FROM source_state WHERE source_key = ?", ("source:5",))
        calls = []
        effect = ProductionMonitorEffect(
            repository_root=ROOT,
            monitor=lambda *args: calls.append(args) or self._events(),
            notifier=FakeNotifier(),
        )
        with self.assertRaisesRegex(ValueError, "state is incomplete"):
            effect.run(
                state_path=self.config.state_path,
                execution_root=self.external,
                scheduled_for=NOW.replace(minute=17),
                observed_at=NOW,
            )
        self.assertEqual(calls, [])
        self.assertFalse(
            (self.internal / "monitor" / "production-wakeups.sqlite3").exists()
        )

    def test_production_cli_fails_before_network_without_marker(self):
        (self.internal / PRODUCTION_MARKER).unlink()
        args = [
            "--repository-root", str(self.repository),
            "--python-executable", str(self.python),
            "--internal-root", str(self.internal),
            "--external-volume-root", str(self.external),
            "--role-user", "openpapers-test",
            "--production-control",
        ]
        with patch("builtins.print") as printed:
            code = main(
                args,
                volume_probe=FakeVolumeProbe(),
                clock=MutableClock(),
                platform_name="Darwin",
            )
        self.assertEqual(code, 3)
        self.assertIn('"code": "effect_failed"', printed.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
