import tempfile
import unittest
from pathlib import Path
from unittest import mock

from automation import run_execution_shadow


class ExecutionShadowCommandTests(unittest.TestCase):
    def arguments(self, root):
        return [
            "--shadow-root",
            str(root / "shadow"),
            "--canonical-data-root",
            str(root / "canonical"),
            "--repository-root",
            str(root / "repository"),
            "--python-executable",
            str(root / "python"),
            "--venue",
            "colt",
            "--year",
            "2025",
            "--expected-count",
            "181",
            "--timeout-seconds",
            "60",
        ]

    def test_command_refuses_without_live_before_service_or_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(
                run_execution_shadow, "_service_loaded"
            ) as service:
                status = run_execution_shadow.main(self.arguments(root))
            self.assertEqual(status, 2)
            service.assert_not_called()
            self.assertFalse((root / "shadow").exists())

    def test_non_macos_and_missing_service_refuse_before_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = ["--live", *self.arguments(root)]
            with mock.patch.object(run_execution_shadow.sys, "platform", "linux"):
                self.assertEqual(run_execution_shadow.main(args), 2)
            with (
                mock.patch.object(run_execution_shadow.sys, "platform", "darwin"),
                mock.patch.object(run_execution_shadow, "_service_loaded", return_value=False),
            ):
                self.assertEqual(run_execution_shadow.main(args), 2)
            self.assertFalse((root / "shadow").exists())

    def test_invalid_expected_count_refuses_before_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = ["--live", *self.arguments(root)]
            index = args.index("181")
            args[index] = "0"
            with (
                mock.patch.object(run_execution_shadow.sys, "platform", "darwin"),
                mock.patch.object(run_execution_shadow, "_service_loaded", return_value=True),
            ):
                self.assertEqual(run_execution_shadow.main(args), 2)
            self.assertFalse((root / "shadow").exists())

    def test_command_has_no_cloud_scheduler_promotion_or_codex_integration(self):
        source = Path(run_execution_shadow.__file__).read_text(encoding="utf-8")
        self.assertNotIn("gcloud", source)
        self.assertNotIn("statistics", source)
        self.assertNotIn("mustcite", source.lower())
        self.assertNotIn("codex", source.lower())
        self.assertNotIn("local_service", source)


if __name__ == "__main__":
    unittest.main()
