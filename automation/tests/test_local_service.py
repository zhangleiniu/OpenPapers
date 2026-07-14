import ast
import json
import os
import plistlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from automation.local_service import (
    LOCAL_SERVICE_LABEL,
    HealthCheckCode,
    ISOLATED_SHADOW_MARKER,
    LocalEffectOutcome,
    LocalEffectStatus,
    LocalMountProbe,
    LocalServiceConfig,
    LocalServiceRunCode,
    LocalServiceRunStatus,
    build_rollback_scope,
    collect_local_service_health,
    initialize_isolated_shadow_root,
    render_launchdaemon,
    render_isolated_shadow_launchdaemon,
    run_local_service_once,
    validate_isolated_shadow_root,
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
        (self.repository / "automation").mkdir(parents=True)
        for marker in (
            self.repository / "main.py",
            self.repository / "automation" / "local_scheduler.py",
            self.repository / "automation" / "local_control_plane.py",
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

        shadow_document = plistlib.loads(
            render_isolated_shadow_launchdaemon(self.config)
        )
        self.assertEqual(
            shadow_document["ProgramArguments"][:-1],
            document["ProgramArguments"],
        )
        self.assertEqual(
            shadow_document["ProgramArguments"][-1], "--isolated-shadow"
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

    def test_isolated_shadow_requires_marker_then_replays_empty_scheduler(self):
        args = [
            "--repository-root", str(self.repository),
            "--python-executable", str(self.python),
            "--internal-root", str(self.internal),
            "--external-volume-root", str(self.external),
            "--role-user", "openpapers-test",
            "--schedule-minute", "17",
            "--record-limit", "4",
            "--isolated-shadow",
        ]
        with patch("builtins.print") as printed:
            missing = main(
                args,
                volume_probe=FakeVolumeProbe(),
                clock=MutableClock(),
                platform_name="Darwin",
            )
        self.assertEqual(missing, 3)
        self.assertIn('"code": "effect_failed"', printed.call_args.args[0])
        self.assertFalse(self.config.state_path.exists())

        marker = initialize_isolated_shadow_root(self.internal)
        self.assertEqual(marker.name, ISOLATED_SHADOW_MARKER)
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(initialize_isolated_shadow_root(self.internal), marker)
        validate_isolated_shadow_root(self.internal)

        with patch("builtins.print") as printed:
            first = main(
                args,
                volume_probe=FakeVolumeProbe(),
                clock=MutableClock(),
                platform_name="Darwin",
            )
            second = main(
                args,
                volume_probe=FakeVolumeProbe(),
                clock=MutableClock(),
                platform_name="Darwin",
            )
        self.assertEqual((first, second), (0, 0))
        self.assertIn('"code": "no_due_work"', printed.call_args.args[0])
        with sqlite3.connect(self.config.state_path) as connection:
            owner = connection.execute(
                "SELECT owner_kind FROM control_ownership WHERE ownership_id = 1"
            ).fetchone()
            wakeups = connection.execute(
                "SELECT status, COUNT(*) FROM scheduler_wakeup GROUP BY status"
            ).fetchall()
        self.assertEqual(owner, ("local_control_plane",))
        self.assertEqual(wakeups, [("completed", 1)])

        marker.write_text('{"mode":"production"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "marker is invalid"):
            validate_isolated_shadow_root(self.internal)
        with self.assertRaisesRegex(ValueError, "marker is invalid"):
            initialize_isolated_shadow_root(self.internal)

        marker.unlink()
        self.internal.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "directory is unsafe"):
            initialize_isolated_shadow_root(self.internal)

    def test_package_has_no_network_or_effect_adapter_dependency(self):
        imported = set()
        source = ""
        for module in PACKAGE.glob("*.py"):
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
            "automation.mac_worker",
            "automation.prefect_flows",
        ):
            self.assertNotIn(forbidden, imported)
            self.assertNotIn(forbidden, source)
        self.assertNotIn("getenv", source)
        self.assertNotIn("launchctl", source)


if __name__ == "__main__":
    unittest.main()
