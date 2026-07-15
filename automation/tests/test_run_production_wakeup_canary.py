import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.production_wakeup_canary import CanaryOutcome, CanaryRootError
from automation.run_production_wakeup_canary import main


class RunProductionWakeupCanaryCommandTests(unittest.TestCase):
    def test_command_refuses_without_live_before_any_root_or_project_check(self):
        calls = []
        with patch(
            "automation.run_production_wakeup_canary.run_canary",
            side_effect=lambda *a, **k: calls.append((a, k)),
        ), patch("automation.run_production_wakeup_canary.load_dotenv"), patch(
            "sys.stderr"
        ):
            with self.assertRaises(SystemExit) as caught:
                main(["--canary-root", "/nonexistent/canary-root"])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(calls, [])

    def test_missing_gemini_project_refuses_before_run_canary(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {}, clear=True
        ), patch("automation.run_production_wakeup_canary.load_dotenv"), patch(
            "automation.run_production_wakeup_canary.run_canary",
            side_effect=lambda *a, **k: calls.append((a, k)),
        ), patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                main([
                    "--live",
                    "--canary-root", str(Path(directory) / "canary"),
                ])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(calls, [])
        self.assertFalse((Path(directory) / "canary").exists())

    def test_action_retained_outcome_prints_summary_and_returns_zero(self):
        outcome = CanaryOutcome(
            replayed=False,
            outcome="action_retained",
            refusal_category=None,
            selection_count=1,
            verification_ids=("verification:fixture",),
            retained_jobs=(
                {
                    "job_id": "job:fixture",
                    "action_type": "queue_existing_scraper",
                    "state": "pending",
                },
            ),
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {}, clear=True
        ), patch("automation.run_production_wakeup_canary.load_dotenv"), patch(
            "automation.run_production_wakeup_canary.run_canary",
            return_value=outcome,
        ) as run_mock, patch("builtins.print") as printed:
            code = main([
                "--live",
                "--canary-root", str(Path(directory) / "canary"),
                "--gemini-project", "test-project",
            ])
        self.assertEqual(code, 0)
        run_mock.assert_called_once()
        rendered = printed.call_args.args[0]
        self.assertIn('"outcome": "action_retained"', rendered)
        self.assertIn("job:fixture", rendered)

    def test_refused_outcome_returns_two(self):
        outcome = CanaryOutcome(
            replayed=False,
            outcome="refused",
            refusal_category="AutomaticDiscoveryRefused",
            selection_count=0,
            verification_ids=(),
            retained_jobs=(),
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {}, clear=True
        ), patch("automation.run_production_wakeup_canary.load_dotenv"), patch(
            "automation.run_production_wakeup_canary.run_canary",
            return_value=outcome,
        ), patch("builtins.print"):
            code = main([
                "--live",
                "--canary-root", str(Path(directory) / "canary"),
                "--gemini-project", "test-project",
            ])
        self.assertEqual(code, 2)

    def test_no_action_and_replayed_outcomes_return_three(self):
        for value in ("no_action", "replayed"):
            outcome = CanaryOutcome(
                replayed=(value == "replayed"),
                outcome=value,
                refusal_category=None,
                selection_count=0,
                verification_ids=(),
                retained_jobs=(),
            )
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {}, clear=True
            ), patch("automation.run_production_wakeup_canary.load_dotenv"), patch(
                "automation.run_production_wakeup_canary.run_canary",
                return_value=outcome,
            ), patch("builtins.print"):
                code = main([
                    "--live",
                    "--canary-root", str(Path(directory) / "canary"),
                    "--gemini-project", "test-project",
                ])
            self.assertEqual(code, 3)

    def test_canary_root_error_exits_two(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {}, clear=True
        ), patch("automation.run_production_wakeup_canary.load_dotenv"), patch(
            "automation.run_production_wakeup_canary.run_canary",
            side_effect=CanaryRootError("unsafe root"),
        ), patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                main([
                    "--live",
                    "--canary-root", str(Path(directory) / "canary"),
                    "--gemini-project", "test-project",
                ])
        self.assertEqual(caught.exception.code, 2)

    def test_gemini_project_falls_back_to_environment(self):
        outcome = CanaryOutcome(
            replayed=False,
            outcome="no_action",
            refusal_category=None,
            selection_count=1,
            verification_ids=(),
            retained_jobs=(),
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"GCP_PROJECT_ID": "env-project"}, clear=True
        ), patch("automation.run_production_wakeup_canary.load_dotenv"), patch(
            "automation.run_production_wakeup_canary.run_canary",
            return_value=outcome,
        ) as run_mock, patch("builtins.print"):
            main([
                "--live",
                "--canary-root", str(Path(directory) / "canary"),
            ])
        self.assertEqual(run_mock.call_args.kwargs["gemini_project"], "env-project")


if __name__ == "__main__":
    unittest.main()
