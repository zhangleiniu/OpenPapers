import ast
import json
import tempfile
import unittest
from pathlib import Path

from automation.control_state import ControlStateRepository, ExecutionQueueError
from automation.domain import ActionType, Writer
from automation.execution_retention import (
    ExecutionRetentionError,
    retain_execution_actions,
)
from automation.lifecycle import (
    ActionIntent,
    QueueExistingScraperPayload,
    RecheckPayload,
)


FIXTURES = Path(__file__).with_name("fixtures")
MODULE = Path(__file__).resolve().parents[1] / "execution_retention.py"
NOW = "2026-07-14T15:00:00Z"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verification_bundle():
    discovery = load_json(FIXTURES / "phase0" / "discovery-result.v1.json")
    request = load_json(FIXTURES / "phase2" / "verification-request.v2.json")
    result = load_json(FIXTURES / "phase2" / "verification-result.v2.json")
    return discovery, request, result


def scraper_action(result, *, action_id="action:" + "f" * 32):
    return ActionIntent(
        action_id=action_id,
        action_type=ActionType.QUEUE_EXISTING_SCRAPER,
        venue_id=result["venue_id"],
        year=result["year"],
        evidence_ids=(
            result["verification_id"],
            "source:icml:pdf-test",
            "snapshot:icml:pdf-test",
        ),
        payload=QueueExistingScraperPayload(
            readiness="pdf_ready",
            scraper_module="scrapers.icml",
            scraper_class="ICMLScraper",
        ),
    )


def recheck_action(result, *, action_id="action:" + "e" * 32):
    return ActionIntent(
        action_id=action_id,
        action_type=ActionType.RECHECK_AT,
        venue_id=result["venue_id"],
        year=result["year"],
        evidence_ids=(result["verification_id"],),
        payload=RecheckPayload(at=NOW, reason="unknown_schedule_fallback"),
    )


class ExecutionRetentionTests(unittest.TestCase):
    def _seeded_repository(self, path):
        repo = ControlStateRepository(path, writer=Writer.LOCAL_CONTROL_PLANE)
        lease = repo.acquire_lease("dispatch-owner")
        repo.accept_verification(*verification_bundle(), lease=lease, received_at=NOW)
        return repo, lease

    def test_only_scraper_actions_are_retained(self):
        _, _, result = verification_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            repo, lease = self._seeded_repository(path)
            try:
                outcomes = retain_execution_actions(
                    repo,
                    (recheck_action(result), scraper_action(result)),
                    source_verification_id=result["verification_id"],
                    lease=lease,
                    enqueued_at=NOW,
                )
                self.assertEqual(len(outcomes), 1)
                self.assertTrue(outcomes[0].applied)
            finally:
                repo.release_lease(lease)
                repo.close()

    def test_exact_duplicate_action_is_retained_once(self):
        _, _, result = verification_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            repo, lease = self._seeded_repository(path)
            try:
                action = scraper_action(result)
                outcomes = retain_execution_actions(
                    repo,
                    (action, action),
                    source_verification_id=result["verification_id"],
                    lease=lease,
                    enqueued_at=NOW,
                )
                self.assertEqual(len(outcomes), 1)
            finally:
                repo.release_lease(lease)
                repo.close()

    def test_conflicting_duplicate_action_id_fails_before_retention(self):
        _, _, result = verification_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            repo, lease = self._seeded_repository(path)
            try:
                first = scraper_action(result, action_id="action:" + "1" * 32)
                second = scraper_action(result, action_id="action:" + "1" * 32)
                second = ActionIntent(
                    action_id=second.action_id,
                    action_type=second.action_type,
                    venue_id=second.venue_id,
                    year=second.year,
                    evidence_ids=(
                        result["verification_id"],
                        "source:icml:pdf-other",
                        "snapshot:icml:pdf-other",
                    ),
                    payload=second.payload,
                )
                with self.assertRaisesRegex(
                    ExecutionRetentionError, "different meaning"
                ):
                    retain_execution_actions(
                        repo,
                        (first, second),
                        source_verification_id=result["verification_id"],
                        lease=lease,
                        enqueued_at=NOW,
                    )
                # The first (non-conflicting) action was already durably
                # retained by its own transaction before the conflicting
                # duplicate was detected; retention per action is atomic,
                # not atomic across the whole supplied sequence.
                retained = repo.list_execution_jobs()
                self.assertEqual(len(retained), 1)
                self.assertEqual(retained[0].action_id, first.action_id)
            finally:
                repo.release_lease(lease)
                repo.close()

    def test_typed_input_is_required(self):
        _, _, result = verification_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            repo, lease = self._seeded_repository(path)
            try:
                with self.assertRaises(ExecutionRetentionError):
                    retain_execution_actions(
                        object(),
                        (scraper_action(result),),
                        source_verification_id=result["verification_id"],
                        lease=lease,
                        enqueued_at=NOW,
                    )
                with self.assertRaises(ExecutionRetentionError):
                    retain_execution_actions(
                        repo,
                        "not-a-sequence",
                        source_verification_id=result["verification_id"],
                        lease=lease,
                        enqueued_at=NOW,
                    )
                with self.assertRaises(ExecutionRetentionError):
                    retain_execution_actions(
                        repo,
                        (object(),),
                        source_verification_id=result["verification_id"],
                        lease=lease,
                        enqueued_at=NOW,
                    )
            finally:
                repo.release_lease(lease)
                repo.close()

    def test_repository_rejection_propagates_unwrapped(self):
        _, _, result = verification_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            repo, lease = self._seeded_repository(path)
            try:
                action = scraper_action(result)
                with self.assertRaises(ExecutionQueueError):
                    retain_execution_actions(
                        repo,
                        (action,),
                        source_verification_id="verification:" + "0" * 32,
                        lease=lease,
                        enqueued_at=NOW,
                    )
            finally:
                repo.release_lease(lease)
                repo.close()

    def test_module_has_no_process_or_execution_pipeline_dependency(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertTrue(
            {
                "subprocess",
                "prefect",
                "google",
                "automation.execution_pipeline",
                "automation.execution_dispatch",
                "automation.mac_worker",
                "automation.staging_executor",
                "automation.staging_validation",
            }.isdisjoint(imports)
        )


if __name__ == "__main__":
    unittest.main()
