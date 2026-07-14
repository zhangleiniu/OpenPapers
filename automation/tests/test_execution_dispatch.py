import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.control_state import ControlStateRepository
from automation.domain import ActionType, Writer
from automation.execution_dispatch import (
    ExecutionDispatchError,
    dispatch_one_existing_scraper,
)
from automation.execution_pipeline import (
    P5ExecutionObservation,
    P5ExecutionStatus,
    P5FailureClass,
    P5Reason,
)
from automation.lifecycle import ActionIntent, QueueExistingScraperPayload


FIXTURES = Path(__file__).with_name("fixtures")
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


class FixedClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, *, seconds):
        self.value += timedelta(seconds=seconds)


class FakeEffect:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []

    def run(self, job):
        self.calls.append(job)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def seed_one_pending_job(path):
    _, _, result = verification_bundle()
    action = scraper_action(result)
    with ControlStateRepository(path, writer=Writer.LOCAL_CONTROL_PLANE) as repo:
        lease = repo.acquire_lease("dispatch-owner")
        repo.accept_verification(*verification_bundle(), lease=lease, received_at=NOW)
        outcome = repo.retain_existing_scraper_action(
            action,
            source_verification_id=result["verification_id"],
            lease=lease,
            enqueued_at=NOW,
        )
        repo.release_lease(lease)
        return outcome.record.job_id


class ExecutionDispatchTests(unittest.TestCase):
    def test_no_pending_job_returns_undispatched_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with ControlStateRepository(path, writer=Writer.LOCAL_CONTROL_PLANE):
                pass
            clock = FixedClock(datetime(2026, 7, 14, 15, 5, tzinfo=timezone.utc))
            effect = FakeEffect(RuntimeError("must not be called"))
            outcome = dispatch_one_existing_scraper(path, clock=clock, effect=effect)
            self.assertFalse(outcome.dispatched)
            self.assertEqual(effect.calls, [])

    def test_ready_observation_completes_the_job_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            job_id = seed_one_pending_job(path)
            clock = FixedClock(datetime(2026, 7, 14, 15, 5, tzinfo=timezone.utc))
            observation = P5ExecutionObservation(
                status=P5ExecutionStatus.READY,
                failure_class=None,
                reason_code=P5Reason.VALIDATED_READY,
                scrape_job_id=job_id,
                result_job_id="job:" + "0" * 64,
                published=True,
                retry_permitted=False,
                paper_count=3,
                valid_pdf_count=3,
            )
            effect = FakeEffect(observation)
            outcome = dispatch_one_existing_scraper(path, clock=clock, effect=effect)
            self.assertTrue(outcome.dispatched)
            self.assertEqual(outcome.disposition, "completed")
            self.assertEqual(len(effect.calls), 1)
            self.assertEqual(effect.calls[0]["job_id"], job_id)

            with ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE
            ) as repo:
                job = repo.get_execution_job(job_id)
                self.assertEqual(job.state, "completed")

            second = dispatch_one_existing_scraper(path, clock=clock, effect=effect)
            self.assertFalse(second.dispatched)
            self.assertEqual(len(effect.calls), 1)

    def test_retryable_observation_returns_job_to_pending_with_new_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            job_id = seed_one_pending_job(path)
            clock = FixedClock(datetime(2026, 7, 14, 15, 5, tzinfo=timezone.utc))
            retry_observation = P5ExecutionObservation(
                status=P5ExecutionStatus.RETRY,
                failure_class=P5FailureClass.TRANSIENT,
                reason_code=P5Reason.PROCESS_FAILED,
                scrape_job_id=job_id,
                result_job_id=None,
                published=False,
                retry_permitted=True,
            )
            first = dispatch_one_existing_scraper(
                path, clock=clock, effect=FakeEffect(retry_observation)
            )
            self.assertEqual(first.disposition, "retry")
            self.assertEqual(first.attempt_number, 1)

            with ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE
            ) as repo:
                job = repo.get_execution_job(job_id)
                self.assertEqual(job.state, "pending")
                self.assertEqual(job.current_attempt_number, 1)

            ready_observation = P5ExecutionObservation(
                status=P5ExecutionStatus.READY,
                failure_class=None,
                reason_code=P5Reason.VALIDATED_READY,
                scrape_job_id=job_id,
                result_job_id="job:" + "1" * 64,
                published=True,
                retry_permitted=False,
                paper_count=1,
                valid_pdf_count=1,
            )
            second = dispatch_one_existing_scraper(
                path, clock=clock, effect=FakeEffect(ready_observation)
            )
            self.assertEqual(second.disposition, "completed")
            self.assertEqual(second.attempt_number, 2)

    def test_effect_exception_leaves_job_in_flight_and_blocks_redispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            job_id = seed_one_pending_job(path)
            clock = FixedClock(datetime(2026, 7, 14, 15, 5, tzinfo=timezone.utc))
            outcome = dispatch_one_existing_scraper(
                path, clock=clock, effect=FakeEffect(RuntimeError("boom"))
            )
            self.assertTrue(outcome.dispatched)
            self.assertIsNone(outcome.disposition)

            with ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE
            ) as repo:
                job = repo.get_execution_job(job_id)
                self.assertEqual(job.state, "in_flight")

            blocked = dispatch_one_existing_scraper(
                path, clock=clock, effect=FakeEffect(RuntimeError("must not run"))
            )
            self.assertFalse(blocked.dispatched)

    def test_recovery_required_observation_blocks_without_completing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            job_id = seed_one_pending_job(path)
            clock = FixedClock(datetime(2026, 7, 14, 15, 5, tzinfo=timezone.utc))
            ambiguous = P5ExecutionObservation(
                status=P5ExecutionStatus.RECOVERY_REQUIRED,
                failure_class=P5FailureClass.OPERATIONAL,
                reason_code=P5Reason.PROCESS_AMBIGUOUS,
                scrape_job_id=job_id,
                result_job_id=None,
                published=False,
                retry_permitted=False,
            )
            outcome = dispatch_one_existing_scraper(
                path, clock=clock, effect=FakeEffect(ambiguous)
            )
            self.assertTrue(outcome.dispatched)
            self.assertIsNone(outcome.disposition)
            with ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE
            ) as repo:
                job = repo.get_execution_job(job_id)
                self.assertEqual(job.state, "in_flight")

    def test_observation_for_a_different_job_id_is_treated_as_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            job_id = seed_one_pending_job(path)
            clock = FixedClock(datetime(2026, 7, 14, 15, 5, tzinfo=timezone.utc))
            mismatched = P5ExecutionObservation(
                status=P5ExecutionStatus.READY,
                failure_class=None,
                reason_code=P5Reason.VALIDATED_READY,
                scrape_job_id="job:" + "9" * 64,
                result_job_id="job:" + "0" * 64,
                published=True,
                retry_permitted=False,
                paper_count=1,
                valid_pdf_count=1,
            )
            outcome = dispatch_one_existing_scraper(
                path, clock=clock, effect=FakeEffect(mismatched)
            )
            self.assertTrue(outcome.dispatched)
            self.assertIsNone(outcome.disposition)
            with ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE
            ) as repo:
                job = repo.get_execution_job(job_id)
                self.assertEqual(job.state, "in_flight")

    def test_invalid_effect_or_clock_is_rejected_before_any_repository_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self.assertRaises(ExecutionDispatchError):
                dispatch_one_existing_scraper(path, clock=lambda: 5, effect=object())
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
