import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.discovery import GroundingSource, ProviderResponse
from automation.run_discovery import main


FIXTURE = (
    Path(__file__).with_name("fixtures")
    / "phase1"
    / "gemini-grounded-response.v1.json"
)


class FakeProvider:
    name = "fake-search"
    model = "fake-model"
    prompt_version = "v1"

    def __init__(self):
        self.calls = 0
        self.closed = False

    def discover(self, request):
        self.calls += 1
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        body = payload["body"]
        body["venue_id"] = request.venue_id
        body["year"] = request.year
        for claim in body["claims"]:
            claim["venue_id"] = request.venue_id
            claim["year"] = request.year
        for milestone in body["candidate_milestones"]:
            milestone["venue_id"] = request.venue_id
            milestone["year"] = request.year
            milestone["date"] = f"{request.year}-07-13"
        if request.venue_id != "icml":
            body["claims"] = []
            body["candidate_milestones"] = []
            body["conference_status"] = "unknown"
            body["paper_list_status"] = "unknown"
            body["metadata_status"] = "unknown"
            body["pdf_status"] = "unknown"
            body["proceedings_status"] = "unknown"
        return ProviderResponse(
            body=body,
            grounding_sources=tuple(
                GroundingSource(**source)
                for source in payload["grounding_sources"]
            ),
            search_queries=tuple(payload["search_queries"]),
        )

    def close(self):
        self.closed = True


class DiscoveryCommandTests(unittest.TestCase):
    def test_command_refuses_without_live_before_constructing_provider(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        def forbidden_factory():
            raise AssertionError("provider factory must not run")

        code = main(
            ["--venue", "icml"],
            provider_factory=forbidden_factory,
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(code, 2)
        self.assertIn("without --live", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_negative_retry_count_is_rejected_without_constructing_provider(self):
        provider = FakeProvider()
        stderr = io.StringIO()

        code = main(
            ["--live", "--max-retries", "-1"],
            provider_factory=lambda: provider,
            stdout=io.StringIO(),
            stderr=stderr,
        )

        self.assertEqual(code, 2)
        self.assertEqual(provider.calls, 0)
        self.assertIn("cannot be negative", stderr.getvalue())

    def test_live_fake_run_retains_shadow_artifact_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "discovery"
            provider = FakeProvider()
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = main(
                [
                    "--live",
                    "--venue", "icml",
                    "--year", "2026",
                    "--artifact-root", str(root),
                ],
                provider_factory=lambda: provider,
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(provider.calls, 1)
            self.assertTrue(provider.closed)
            self.assertIn("Unmetered manual development", stdout.getvalue())
            self.assertIn("candidate_milestones=2", stdout.getvalue())
            artifacts = list((root / "artifacts").rglob("*.json"))
            self.assertEqual(len(artifacts), 1)
            self.assertFalse((root / "state.sqlite3").exists())
            self.assertFalse((root / "jobs").exists())
            self.assertFalse((root / "budget-ledger.v1.json").exists())

    def test_live_command_allows_catalog_venue_outside_initial_cohort(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = FakeProvider()
            stderr = io.StringIO()
            code = main(
                [
                    "--live",
                    "--force",
                    "--venue", "neurips",
                    "--year", "2026",
                    "--artifact-root", temp_dir,
                ],
                provider_factory=lambda: provider,
                stdout=io.StringIO(),
                stderr=stderr,
            )
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(provider.calls, 1)

    def test_live_command_rejects_unknown_catalog_venue(self):
        provider = FakeProvider()
        stderr = io.StringIO()
        code = main(
            ["--live", "--venue", "not-a-venue", "--year", "2026"],
            provider_factory=lambda: provider,
            stdout=io.StringIO(),
            stderr=stderr,
        )
        self.assertEqual(code, 2)
        self.assertEqual(provider.calls, 0)
        self.assertIn("absent from the automation catalog", stderr.getvalue())

    def test_default_artifact_root_is_resolved_after_dotenv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            def load_test_environment(**kwargs):
                os.environ["SCRAPER_DATA_ROOT"] = str(root)

            with patch.dict(os.environ, {}, clear=True), patch(
                    "dotenv.load_dotenv", side_effect=load_test_environment):
                provider = FakeProvider()
                code = main(
                    ["--live", "--venue", "icml", "--year", "2026"],
                    provider_factory=lambda: provider,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
                self.assertEqual(code, 0)
                self.assertTrue(
                    (root / "automation" / "discovery"
                     / "artifacts").exists())
                self.assertFalse(
                    (root / "automation" / "discovery"
                     / "budget-ledger.v1.json").exists())


if __name__ == "__main__":
    unittest.main()
