import ast
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.production_wakeup import ProductionControlPlaneConfig
from automation.production_wakeup_canary import (
    CANARY_VENUE_ID,
    CANARY_YEAR,
    CanaryRootError,
    prepare_canary_root,
    run_canary,
    seed_due_conference_state,
)
from automation.tests.test_production_wakeup import (
    FactoryCounter,
    FakeProvider,
    MappingFetcher,
    pdf_ready_discovery_response,
    pdf_response,
)


MODULE = Path(__file__).resolve().parents[1] / "production_wakeup_canary.py"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
PDF_URL = "https://proceedings.mlr.press/openpapers-fixture/colt2025/paper.pdf"


class CanaryRootTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name) / "canary"
        self.addCleanup(self._temp_dir.cleanup)

    def test_fresh_root_is_stamped_and_marked(self):
        state = prepare_canary_root(self.root, clock=lambda: NOW)
        self.assertEqual(state.scheduled_for, NOW)
        self.assertEqual(state.venue_id, CANARY_VENUE_ID)
        self.assertEqual(state.year, CANARY_YEAR)
        self.assertTrue((self.root / ".p2-8s-live-canary.v1.json").is_file())
        self.assertTrue(state.control_state_path.parent.is_dir())
        self.assertTrue(state.automation_root.is_dir())
        self.assertTrue(state.review_root.is_dir())

    def test_marked_root_replays_the_stamped_schedule(self):
        first = prepare_canary_root(self.root, clock=lambda: NOW)
        second = prepare_canary_root(
            self.root, clock=lambda: NOW + timedelta(days=1)
        )
        self.assertEqual(first.scheduled_for, second.scheduled_for)

    def test_nonempty_unmarked_root_is_rejected(self):
        self.root.mkdir(parents=True)
        (self.root / "foreign.txt").write_text("not a canary")
        with self.assertRaises(CanaryRootError):
            prepare_canary_root(self.root, clock=lambda: NOW)

    def test_production_marker_is_rejected(self):
        self.root.mkdir(parents=True)
        (self.root / ".production-control.v1.json").write_text("{}")
        with self.assertRaises(CanaryRootError):
            prepare_canary_root(self.root, clock=lambda: NOW)

    def test_isolated_shadow_marker_is_rejected(self):
        self.root.mkdir(parents=True)
        (self.root / ".isolated-shadow.v1.json").write_text("{}")
        with self.assertRaises(CanaryRootError):
            prepare_canary_root(self.root, clock=lambda: NOW)

    def test_relative_root_is_rejected(self):
        with self.assertRaises(CanaryRootError):
            prepare_canary_root(Path("relative-canary-root"), clock=lambda: NOW)

    def test_drifted_marker_fails_closed(self):
        prepare_canary_root(self.root, clock=lambda: NOW)
        marker_path = self.root / ".p2-8s-live-canary.v1.json"
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        payload["year"] = 2026
        marker_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CanaryRootError):
            prepare_canary_root(self.root, clock=lambda: NOW)

    def test_naive_clock_is_rejected(self):
        with self.assertRaises(CanaryRootError):
            prepare_canary_root(self.root, clock=lambda: datetime(2026, 7, 14, 12, 0))


class SeedDueConferenceStateTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self._temp_dir.name) / "state.sqlite3"
        self.addCleanup(self._temp_dir.cleanup)

    def test_seed_creates_one_due_row(self):
        seed_due_conference_state(
            self.state_path, venue_id=CANARY_VENUE_ID, year=CANARY_YEAR, due_at=NOW,
        )
        with ControlStateRepository(
            self.state_path, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW,
        ) as repository:
            current = repository.get_conference_state(CANARY_VENUE_ID, CANARY_YEAR)
        self.assertIsNotNone(current)
        self.assertEqual(current.state["next_check_at"], "2026-07-14T12:00:00Z")
        self.assertEqual(
            current.state["next_check_reason"], "unknown_schedule_fallback"
        )
        self.assertEqual(current.state["lifecycle_state"], "unknown")

    def test_seed_is_a_no_op_when_a_row_already_exists(self):
        seed_due_conference_state(
            self.state_path, venue_id=CANARY_VENUE_ID, year=CANARY_YEAR, due_at=NOW,
        )
        seed_due_conference_state(
            self.state_path,
            venue_id=CANARY_VENUE_ID,
            year=CANARY_YEAR,
            due_at=NOW + timedelta(hours=1),
        )
        with ControlStateRepository(
            self.state_path, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW,
        ) as repository:
            current = repository.get_conference_state(CANARY_VENUE_ID, CANARY_YEAR)
        self.assertEqual(current.state["next_check_at"], "2026-07-14T12:00:00Z")


class RunCanaryTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name) / "canary"
        self.addCleanup(self._temp_dir.cleanup)

    def test_pdf_ready_round_trip_retains_exactly_one_job_and_replay_is_free(self):
        provider = FakeProvider([
            pdf_ready_discovery_response(
                venue_id=CANARY_VENUE_ID, year=CANARY_YEAR, pdf_url=PDF_URL
            )
        ])
        fetcher = MappingFetcher({PDF_URL: pdf_response(PDF_URL)})

        outcome = run_canary(
            self.root,
            gemini_project="test-project",
            gemini_location="global",
            gemini_model="gemini-test",
            clock=lambda: NOW,
            _discovery_provider_factory=FactoryCounter(provider),
            _verification_fetcher=fetcher,
        )
        self.assertFalse(outcome.replayed)
        self.assertEqual(outcome.outcome, "action_retained")
        self.assertEqual(len(outcome.retained_jobs), 1)
        self.assertEqual(outcome.retained_jobs[0]["action_type"], "queue_existing_scraper")
        self.assertEqual(outcome.selection_count, 1)
        self.assertEqual(len(outcome.verification_ids), 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(fetcher.requests), 1)

        summary_path = self.root / "review" / "summary.v1.json"
        self.assertTrue(summary_path.is_file())
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["outcome"], "action_retained")
        self.assertEqual(summary["venue_id"], CANARY_VENUE_ID)
        self.assertEqual(summary["year"], CANARY_YEAR)
        self.assertEqual(len(summary["retained_jobs"]), 1)

        replay = run_canary(
            self.root,
            gemini_project="test-project",
            gemini_location="global",
            gemini_model="gemini-test",
            clock=lambda: NOW + timedelta(minutes=1),
            _discovery_provider_factory=FactoryCounter(provider),
            _verification_fetcher=fetcher,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(replay.retained_jobs, ())
        # Exact replay of the same wakeup makes no further fake calls.
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(fetcher.requests), 1)
        # The original evidence remains untouched.
        self.assertEqual(
            json.loads(summary_path.read_text(encoding="utf-8")), summary
        )

    def test_invalid_pdf_signature_completes_with_no_action(self):
        provider = FakeProvider([
            pdf_ready_discovery_response(
                venue_id=CANARY_VENUE_ID, year=CANARY_YEAR, pdf_url=PDF_URL
            )
        ])
        fetcher = MappingFetcher(
            {PDF_URL: pdf_response(PDF_URL, body=b"not a pdf")}
        )

        outcome = run_canary(
            self.root,
            gemini_project="test-project",
            gemini_location="global",
            gemini_model="gemini-test",
            clock=lambda: NOW,
            _discovery_provider_factory=FactoryCounter(provider),
            _verification_fetcher=fetcher,
        )
        self.assertFalse(outcome.replayed)
        self.assertEqual(outcome.outcome, "no_action")
        self.assertEqual(outcome.retained_jobs, ())
        summary = json.loads(
            (self.root / "review" / "summary.v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary["outcome"], "no_action")

    def test_open_circuit_refuses_before_any_call_and_records_refusal(self):
        state = prepare_canary_root(self.root, clock=lambda: NOW)
        config = ProductionControlPlaneConfig(
            control_state_path=state.control_state_path,
            automation_root=state.automation_root,
            gemini_project="test-project",
            gemini_location="global",
            gemini_model="gemini-test",
        )
        deadline = NOW + timedelta(hours=1)
        ledger_payload = {
            "version": 1,
            "venues": {},
            "circuit": {
                "systemic_fingerprint": "fixture-systemic-fingerprint",
                "venues": ["colt", "icml", "ijcai"],
                "opened_at": "2026-07-14T10:00:00Z",
                "deadline_at": deadline.isoformat().replace("+00:00", "Z"),
            },
            "systemic_events": [],
        }
        config.discovery_health_ledger_path.parent.mkdir(
            parents=True, exist_ok=True
        )
        config.discovery_health_ledger_path.write_text(
            json.dumps(ledger_payload), encoding="utf-8"
        )
        provider = FakeProvider([
            pdf_ready_discovery_response(
                venue_id=CANARY_VENUE_ID, year=CANARY_YEAR, pdf_url=PDF_URL
            )
        ])
        fetcher = MappingFetcher({})

        outcome = run_canary(
            self.root,
            gemini_project="test-project",
            gemini_location="global",
            gemini_model="gemini-test",
            clock=lambda: NOW,
            _discovery_provider_factory=FactoryCounter(provider),
            _verification_fetcher=fetcher,
        )
        self.assertEqual(outcome.outcome, "refused")
        self.assertIsNotNone(outcome.refusal_category)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(fetcher.requests, [])
        with ControlStateRepository(
            state.control_state_path,
            writer=Writer.LOCAL_CONTROL_PLANE,
            clock=lambda: NOW,
        ) as repository:
            self.assertEqual(
                repository.list_scheduler_wakeups()[0].status, "active"
            )
            self.assertEqual(repository.list_execution_jobs(), ())
        summary = json.loads(
            (self.root / "review" / "summary.v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary["outcome"], "refused")
        self.assertIsNotNone(summary["refusal_category"])


class ScopeTests(unittest.TestCase):
    def test_module_has_no_dispatch_execution_or_installed_service_import(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        source = MODULE.read_text(encoding="utf-8")
        for forbidden in (
            "automation.execution_dispatch",
            "automation.execution_pipeline",
            "automation.mac_worker",
            "automation.staging_executor",
            "automation.local_service",
            "prefect",
            "requests",
            "subprocess",
        ):
            self.assertNotIn(forbidden, imported)
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
