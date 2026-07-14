import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.mac_worker.health import (
    HealthCheckCode,
    HealthCheckName,
    HealthCheckStatus,
    WorkerHealthConfig,
    collect_worker_health,
)
from automation.mac_worker.prefect_support import LocalPrefectSettingsProbe


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "automation" / "mac_worker"
PLIST = PACKAGE / "launchd" / "org.openpapers.prefect-worker.plist.example"


class FakePrefectProbe:
    def __init__(self, configured=True, error=None):
        self.configured = configured
        self.error = error
        self.calls = []

    def is_configured(self, *, work_pool_name):
        self.calls.append(work_pool_name)
        if self.error is not None:
            raise self.error
        return self.configured


class WorkerHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        self.repository = root / "repository"
        self.data_root = root / "data"
        self.auth_path = root / ".codex" / "auth.json"
        (self.repository / "automation" / "mac_worker").mkdir(parents=True)
        self.data_root.mkdir()
        self.auth_path.parent.mkdir()
        for marker in (
            self.repository / "main.py",
            self.repository / "automation" / "job_queue.py",
            self.repository / "automation" / "mac_worker" / "runtime.py",
        ):
            marker.touch()
        self.auth_path.write_text("local fixture marker", encoding="utf-8")
        self.auth_path.chmod(0o600)
        self.config = WorkerHealthConfig(
            repository_root=self.repository,
            data_root=self.data_root,
            codex_auth_path=self.auth_path,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def collect(self, probe=None, **kwargs):
        return collect_worker_health(
            self.config,
            probe or FakePrefectProbe(),
            python_version=kwargs.get("python_version", (3, 12, 10)),
            platform_name=kwargs.get("platform_name", "Darwin"),
            prefect_version=kwargs.get("prefect_version", "3.7.8"),
        )

    def test_healthy_local_fixture_report_is_ready_and_secret_free(self):
        probe = FakePrefectProbe()

        report = self.collect(probe)

        self.assertTrue(report.ready)
        self.assertEqual(probe.calls, ["openpapers-mac"])
        self.assertEqual(len(report.checks), len(HealthCheckName))
        self.assertTrue(
            all(check.status is HealthCheckStatus.PASS for check in report.checks)
        )
        serialized = json.dumps(report.as_dict(), sort_keys=True)
        self.assertNotIn(str(self.repository), serialized)
        self.assertNotIn(str(self.data_root), serialized)
        self.assertNotIn("local fixture marker", serialized)

    def test_runtime_package_and_prefect_probe_failures_are_bounded(self):
        cases = (
            (
                {"python_version": (3, 13, 0)},
                FakePrefectProbe(),
                HealthCheckCode.UNSUPPORTED_PYTHON,
            ),
            (
                {"platform_name": "Linux"},
                FakePrefectProbe(),
                HealthCheckCode.UNSUPPORTED_OPERATING_SYSTEM,
            ),
            (
                {"prefect_version": "4.0.0"},
                FakePrefectProbe(),
                HealthCheckCode.PREFECT_VERSION_UNSUPPORTED,
            ),
            (
                {"prefect_version": "3.6.9"},
                FakePrefectProbe(),
                HealthCheckCode.PREFECT_VERSION_UNSUPPORTED,
            ),
            (
                {},
                FakePrefectProbe(configured=False),
                HealthCheckCode.PREFECT_CONFIGURATION_MISSING,
            ),
            (
                {},
                FakePrefectProbe(error=RuntimeError("api_key=do-not-retain")),
                HealthCheckCode.PREFECT_PROBE_FAILED,
            ),
        )
        for kwargs, probe, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                report = self.collect(probe, **kwargs)
                self.assertFalse(report.ready)
                self.assertIn(expected_code, {check.code for check in report.checks})
                self.assertNotIn("do-not-retain", json.dumps(report.as_dict()))

    def test_repository_data_and_codex_marker_fail_closed(self):
        (self.repository / "main.py").unlink()
        self.data_root.rmdir()
        self.auth_path.chmod(0o644)

        report = self.collect()

        self.assertFalse(report.ready)
        failures = {
            check.name: check.code
            for check in report.checks
            if check.status is HealthCheckStatus.FAIL
        }
        self.assertEqual(
            failures,
            {
                HealthCheckName.REPOSITORY: HealthCheckCode.INVALID_REPOSITORY,
                HealthCheckName.DATA_ROOT: HealthCheckCode.DATA_ROOT_UNAVAILABLE,
                HealthCheckName.CODEX_LOGIN_MARKER: HealthCheckCode.CODEX_LOGIN_UNSAFE,
            },
        )

    def test_missing_or_symlinked_codex_marker_is_not_a_login_signal(self):
        self.auth_path.unlink()
        missing = self.collect()
        self.assertIn(
            HealthCheckCode.CODEX_LOGIN_MISSING,
            {check.code for check in missing.checks},
        )

        target = self.auth_path.parent / "target.json"
        target.write_text("fixture", encoding="utf-8")
        target.chmod(0o600)
        self.auth_path.symlink_to(target)
        linked = self.collect()
        self.assertIn(
            HealthCheckCode.CODEX_LOGIN_UNSAFE,
            {check.code for check in linked.checks},
        )

    def test_configuration_requires_absolute_path_objects(self):
        with self.assertRaisesRegex(ValueError, "absolute Path"):
            WorkerHealthConfig(
                repository_root=Path("relative"),
                data_root=self.data_root,
                codex_auth_path=self.auth_path,
            )
        with self.assertRaisesRegex(ValueError, "absolute Path"):
            WorkerHealthConfig(
                repository_root=self.repository,
                data_root=self.data_root,
                codex_auth_path=os.fspath(self.auth_path),
            )


class MacWorkerPackageAssetsTests(unittest.TestCase):
    def test_local_prefect_probe_reads_settings_without_a_client(self):
        configured = SimpleNamespace(
            api=SimpleNamespace(url="https://api.prefect.cloud/api", key=object())
        )
        missing_key = SimpleNamespace(
            api=SimpleNamespace(url="https://api.prefect.cloud/api", key=None)
        )
        probe = LocalPrefectSettingsProbe()

        with patch(
            "automation.mac_worker.prefect_support.get_current_settings",
            return_value=configured,
        ) as settings:
            self.assertTrue(probe.is_configured(work_pool_name="openpapers-mac"))
            settings.assert_called_once_with()
        with patch(
            "automation.mac_worker.prefect_support.get_current_settings",
            return_value=missing_key,
        ):
            self.assertFalse(probe.is_configured(work_pool_name="openpapers-mac"))
        self.assertFalse(probe.is_configured(work_pool_name="another-pool"))

    def test_launchd_template_is_parseable_fixed_and_credential_free(self):
        with PLIST.open("rb") as file_obj:
            document = plistlib.load(file_obj)

        self.assertEqual(document["Label"], "org.openpapers.prefect-worker")
        self.assertEqual(
            document["ProgramArguments"][1:],
            [
                "worker",
                "start",
                "--pool",
                "openpapers-mac",
                "--type",
                "process",
                "--no-create-pool-if-not-found",
                "--install-policy",
                "never",
                "--no-with-healthcheck",
                "--name",
                "openpapers-mac-local",
            ],
        )
        self.assertTrue(document["RunAtLoad"])
        self.assertFalse(document["KeepAlive"]["SuccessfulExit"])
        self.assertEqual(document["Umask"], 0o77)
        serialized = PLIST.read_text(encoding="utf-8").lower()
        for forbidden in (
            "prefect_api_key",
            "codex_api_key",
            "authorization",
            "google_application_credentials",
            ".env",
            "/bin/sh",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_worker_requirements_are_isolated_and_minimal(self):
        requirements = (PACKAGE / "requirements.txt").read_text(encoding="utf-8")
        active = [
            line.strip()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(active, ["jsonschema>=4.23,<5", "prefect>=3.7,<4"])
        root_requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("prefect", root_requirements.lower())


if __name__ == "__main__":
    unittest.main()
