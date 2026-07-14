import ast
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from automation.execution_pipeline import (
    P5ExecutionConfig,
    P5ExecutionError,
    P5ExecutionStatus,
    P5FailureClass,
    P5Reason,
    build_candidate_validation_job,
    run_existing_scraper_pipeline,
)
from automation.job_queue import JobType, build_job
from automation.job_results import PublishedResultBundle, validate_result_bundle
from automation.mac_worker.safety import (
    DiskSpacePolicy,
    JournalState,
    LocalJobJournal,
    WorkerSafetyConfig,
)
from automation.staging_executor import StagingExecutorConfig
from automation.staging_validation import StagingValidationConfig


MODULE = Path(__file__).resolve().parents[1] / "execution_pipeline.py"
BASE_TIME = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)


def build_scrape_job(*, suffix="pipeline", expected_count=1):
    return build_job(
        request_id=f"request:icml:2026:{suffix}",
        job_type=JobType.SCRAPE_EXISTING,
        venue_id="icml",
        year=2026,
        requested_by="human",
        input_artifact_ids=(f"evidence:icml:2026:{suffix}",),
        payload={
            "completeness_level": "archival",
            "download_pdfs": True,
            "expected_count": expected_count,
        },
    )


def paper(*, title="Fixture title"):
    return {
        "id": "paper-1",
        "title": title,
        "authors": ["Fixture Author"],
        "abstract": "Fixture abstract",
        "year": 2026,
        "conference": "icml",
        "url": "https://example.invalid/paper-1",
        "bibtex": "@inproceedings{paper-1}",
        "pdf_url": "https://example.invalid/paper-1.pdf",
        "pdf_path": "papers/icml/2026/paper-1.pdf",
    }


class TickClock:
    def __init__(self, value=BASE_TIME):
        self.value = value

    def __call__(self):
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class FakeHandle:
    def __init__(self, exit_code, *, stopped=True):
        self.exit_code = exit_code
        self.stopped = stopped
        self.terminate_calls = 0

    def wait(self, *, timeout_seconds, cancellation):
        return self.exit_code

    def terminate(self):
        self.terminate_calls += 1

    def wait_stopped(self, *, timeout_seconds):
        return self.stopped


class FakeCancellation:
    def __init__(self, cancelled):
        self.cancelled = cancelled

    def is_cancelled(self):
        return self.cancelled


class FakeLauncher:
    def __init__(self, *, exit_code=0, papers=None, start_error=None, symlink=False):
        self.exit_code = exit_code
        self.papers = [paper()] if papers is None else papers
        self.start_error = start_error
        self.symlink = symlink
        self.requests = []

    def start(self, request):
        self.requests.append(request)
        if self.start_error is not None:
            raise self.start_error
        if self.exit_code == 0:
            metadata = request.data_root / "metadata/icml/icml_2026.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps(self.papers), encoding="utf-8")
            pdf = request.data_root / "papers/icml/2026/paper-1.pdf"
            pdf.parent.mkdir(parents=True)
            if self.symlink:
                pdf.symlink_to(metadata)
            else:
                pdf.write_bytes(b"%PDF-1.7\n" + b"x" * 2048)
        return FakeHandle(self.exit_code)


class FakePublisher:
    def __init__(self, *, fail_after_manifest_once=False):
        self.fail_after_manifest_once = fail_after_manifest_once
        self.manifests = {}
        self.results = {}
        self.calls = []

    def publish(self, job, manifest, result):
        validate_result_bundle(job, manifest, result)
        job_id = job["job_id"]
        manifest_copy = deepcopy(dict(manifest))
        result_copy = deepcopy(dict(result))
        retained = self.manifests.setdefault(job_id, manifest_copy)
        if retained != manifest_copy:
            raise AssertionError("manifest replay changed")
        self.calls.append((deepcopy(dict(job)), manifest_copy, result_copy))
        if self.fail_after_manifest_once:
            self.fail_after_manifest_once = False
            raise RuntimeError("fixture publisher outage")
        retained_result = self.results.setdefault(job_id, result_copy)
        if retained_result != result_copy:
            raise AssertionError("result replay changed")
        return PublishedResultBundle(
            job_id=job_id,
            manifest_name=f"manifests/{job_id}.json",
            manifest_generation=1,
            result_name=f"job-results/{job_id}.json",
            result_generation=2,
        )


class FixturePipeline:
    def __init__(self, *, job=None):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.runtime = self.root / "runtime"
        self.staging = self.root / "staging"
        self.artifacts = self.root / "artifacts"
        self.canonical = self.root / "canonical"
        self.state = self.root / "state"
        for path in (
            self.repository,
            self.runtime,
            self.staging,
            self.artifacts,
            self.canonical,
        ):
            path.mkdir(mode=0o700)
        (self.repository / "main.py").write_text("# fixture\n", encoding="utf-8")
        self.executable = self.runtime / "python"
        self.executable.write_text("#!/bin/sh\n", encoding="utf-8")
        self.executable.chmod(0o700)
        self.job = job or build_scrape_job()
        safety = WorkerSafetyConfig(
            state_root=self.state,
            data_root=self.staging,
            timeout_seconds=60,
            cancellation_grace_seconds=5,
            disk_policy=DiskSpacePolicy(
                minimum_free_bytes=100,
                minimum_free_fraction=0.1,
            ),
        )
        executor = StagingExecutorConfig(
            repository_root=self.repository,
            python_executable=self.executable,
            staging_root=self.staging,
            canonical_data_root=self.canonical,
            timeout_seconds=60,
            cancellation_grace_seconds=5,
        )
        validation = StagingValidationConfig(
            staging_root=self.staging,
            artifact_root=self.artifacts,
            canonical_data_root=self.canonical,
        )
        self.config = P5ExecutionConfig(
            worker_safety=safety,
            staging_executor=executor,
            staging_validation=validation,
            worker_id="worker:mac-mini:fixture",
        )

    def close(self):
        self.temporary.cleanup()

    @staticmethod
    def enough_disk(path):
        return SimpleNamespace(total=10_000, used=1_000, free=9_000)

    @staticmethod
    def low_disk(path):
        return SimpleNamespace(total=10_000, used=9_950, free=50)

    def run(self, launcher, publisher, **kwargs):
        return run_existing_scraper_pipeline(
            self.job,
            self.config,
            launcher,
            publisher,
            disk_usage=kwargs.pop("disk_usage", self.enough_disk),
            clock=kwargs.pop("clock", TickClock()),
            **kwargs,
        )


class ExecutionPipelineTests(unittest.TestCase):
    def fixture(self, **kwargs):
        fixture = FixturePipeline(**kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def test_valid_candidate_routes_ready_publishes_and_suppresses_replay(self):
        fixture = self.fixture()
        launcher = FakeLauncher()
        publisher = FakePublisher()

        outcome = fixture.run(launcher, publisher)
        replay = fixture.run(
            launcher,
            publisher,
            clock=TickClock(BASE_TIME + timedelta(days=1)),
        )

        self.assertEqual(outcome.status, P5ExecutionStatus.READY)
        self.assertIsNone(outcome.failure_class)
        self.assertEqual(outcome.reason_code, P5Reason.VALIDATED_READY)
        self.assertEqual((outcome.paper_count, outcome.valid_pdf_count), (1, 1))
        self.assertTrue(outcome.published)
        self.assertEqual(replay.status, P5ExecutionStatus.SKIPPED)
        self.assertEqual(len(launcher.requests), 1)
        self.assertEqual(len(publisher.calls), 1)
        published_job, manifest, result = publisher.calls[0]
        self.assertEqual(published_job["job_type"], "validate_candidate")
        self.assertEqual(result["status"], "succeeded")
        validate_result_bundle(published_job, manifest, result)
        self.assertFalse(any(fixture.canonical.iterdir()))
        self.assertEqual(
            LocalJobJournal(fixture.state).inspect(fixture.job),
            JournalState.COMPLETED,
        )

    def test_invalid_candidate_is_structural_partial_and_never_ready(self):
        fixture = self.fixture()
        publisher = FakePublisher()

        outcome = fixture.run(FakeLauncher(papers=[paper(title="")]), publisher)

        self.assertEqual(outcome.status, P5ExecutionStatus.PARTIAL)
        self.assertEqual(outcome.failure_class, P5FailureClass.STRUCTURAL)
        self.assertEqual(outcome.reason_code, P5Reason.CANDIDATE_INVALID)
        self.assertEqual(outcome.paper_count, 1)
        self.assertEqual(publisher.calls[0][2]["status"], "failed")
        self.assertEqual(
            publisher.calls[0][2]["error_code"], "structural_candidate_invalid"
        )
        self.assertFalse(any(fixture.canonical.iterdir()))

    def test_validation_safety_failure_is_bounded_structural_failure(self):
        fixture = self.fixture()
        publisher = FakePublisher()

        outcome = fixture.run(FakeLauncher(symlink=True), publisher)

        self.assertEqual(outcome.status, P5ExecutionStatus.FAILED)
        self.assertEqual(outcome.failure_class, P5FailureClass.STRUCTURAL)
        self.assertEqual(outcome.reason_code, P5Reason.VALIDATION_FAILED_CLOSED)
        result = publisher.calls[0][2]
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(str(fixture.root), json.dumps(outcome.as_dict()))
        self.assertNotIn(str(fixture.root), json.dumps(result))

    def test_confirmed_process_failure_is_transient_and_same_job_resumes(self):
        fixture = self.fixture()
        publisher = FakePublisher()
        failed = fixture.run(FakeLauncher(exit_code=7), publisher)

        resumed_launcher = FakeLauncher()
        resumed = fixture.run(
            resumed_launcher,
            publisher,
            clock=TickClock(BASE_TIME + timedelta(minutes=1)),
        )

        self.assertEqual(failed.status, P5ExecutionStatus.RETRY)
        self.assertEqual(failed.failure_class, P5FailureClass.TRANSIENT)
        self.assertEqual(failed.reason_code, P5Reason.PROCESS_FAILED)
        self.assertTrue(failed.retry_permitted)
        self.assertFalse(failed.published)
        self.assertEqual(resumed.status, P5ExecutionStatus.READY)
        self.assertEqual(len(resumed_launcher.requests), 1)

    def test_disk_pressure_is_operational_and_starts_nothing(self):
        fixture = self.fixture()
        launcher = FakeLauncher()
        publisher = FakePublisher()

        outcome = fixture.run(
            launcher, publisher, disk_usage=fixture.low_disk
        )

        self.assertEqual(outcome.status, P5ExecutionStatus.RETRY)
        self.assertEqual(outcome.failure_class, P5FailureClass.OPERATIONAL)
        self.assertEqual(outcome.reason_code, P5Reason.INSUFFICIENT_DISK)
        self.assertEqual(launcher.requests, [])
        self.assertEqual(publisher.calls, [])
        self.assertEqual(
            LocalJobJournal(fixture.state).inspect(fixture.job), JournalState.ABSENT
        )

    def test_confirmed_timeout_and_prestart_cancellation_are_resumable(self):
        for suffix, launcher, cancellation, expected_status, expected_reason in (
            (
                "timeout",
                FakeLauncher(exit_code=None),
                FakeCancellation(False),
                P5ExecutionStatus.RETRY,
                P5Reason.PROCESS_TIMED_OUT,
            ),
            (
                "cancelled",
                FakeLauncher(),
                FakeCancellation(True),
                P5ExecutionStatus.CANCELLED,
                P5Reason.PROCESS_CANCELLED,
            ),
        ):
            with self.subTest(suffix=suffix):
                fixture = self.fixture(job=build_scrape_job(suffix=suffix))
                outcome = fixture.run(
                    launcher,
                    FakePublisher(),
                    cancellation=cancellation,
                )
                self.assertEqual(outcome.status, expected_status)
                self.assertEqual(outcome.reason_code, expected_reason)
                self.assertEqual(outcome.failure_class, P5FailureClass.TRANSIENT)
                self.assertTrue(outcome.retry_permitted)
                self.assertEqual(
                    LocalJobJournal(fixture.state).inspect(fixture.job),
                    JournalState.ABSENT,
                )

    def test_ambiguous_start_retains_claim_and_blocks_replay(self):
        fixture = self.fixture()
        publisher = FakePublisher()

        ambiguous = fixture.run(
            FakeLauncher(start_error=RuntimeError("fixture uncertainty")), publisher
        )
        replay_launcher = FakeLauncher()
        replay = fixture.run(replay_launcher, publisher)

        self.assertEqual(ambiguous.status, P5ExecutionStatus.RECOVERY_REQUIRED)
        self.assertEqual(ambiguous.failure_class, P5FailureClass.OPERATIONAL)
        self.assertEqual(ambiguous.reason_code, P5Reason.PROCESS_AMBIGUOUS)
        self.assertEqual(replay.reason_code, P5Reason.ACTIVE_CLAIM)
        self.assertEqual(replay_launcher.requests, [])
        self.assertEqual(
            LocalJobJournal(fixture.state).inspect(fixture.job), JournalState.ACTIVE
        )

    def test_manifest_only_publish_failure_replays_byte_identically(self):
        fixture = self.fixture()
        launcher = FakeLauncher()
        publisher = FakePublisher(fail_after_manifest_once=True)

        first = fixture.run(launcher, publisher)
        replay = fixture.run(
            launcher,
            publisher,
            clock=TickClock(BASE_TIME + timedelta(days=2)),
        )

        self.assertEqual(first.status, P5ExecutionStatus.RETRY)
        self.assertEqual(first.failure_class, P5FailureClass.OPERATIONAL)
        self.assertEqual(first.reason_code, P5Reason.RESULT_PUBLISH_FAILED)
        self.assertEqual(replay.status, P5ExecutionStatus.READY)
        self.assertEqual(len(launcher.requests), 1)
        self.assertEqual(publisher.calls[0], publisher.calls[1])

    def test_busy_lock_and_incoherent_roots_fail_before_effects(self):
        fixture = self.fixture()
        launcher = FakeLauncher()
        publisher = FakePublisher()
        journal = LocalJobJournal(fixture.state)
        with journal.try_venue_year_lock(fixture.job) as acquired:
            self.assertTrue(acquired)
            busy = fixture.run(launcher, publisher)
        self.assertEqual(busy.reason_code, P5Reason.VENUE_YEAR_BUSY)
        self.assertEqual(busy.failure_class, P5FailureClass.OPERATIONAL)

        overlapping_validation = StagingValidationConfig(
            staging_root=fixture.staging,
            artifact_root=fixture.canonical,
            canonical_data_root=fixture.canonical,
        )
        bad_config = P5ExecutionConfig(
            worker_safety=fixture.config.worker_safety,
            staging_executor=fixture.config.staging_executor,
            staging_validation=overlapping_validation,
            worker_id=fixture.config.worker_id,
        )
        with self.assertRaisesRegex(P5ExecutionError, "disjoint"):
            run_existing_scraper_pipeline(
                fixture.job,
                bad_config,
                launcher,
                publisher,
                disk_usage=fixture.enough_disk,
            )
        self.assertEqual(launcher.requests, [])
        self.assertEqual(publisher.calls, [])

    def test_validation_job_is_deterministic_and_scope_stays_unwired(self):
        fixture = self.fixture()
        manifest = {
            "manifest_id": "manifest:" + "a" * 64,
        }
        first = build_candidate_validation_job(fixture.job, manifest)
        second = build_candidate_validation_job(
            deepcopy(fixture.job), deepcopy(manifest)
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first["payload"]["candidate_manifest_id"], manifest["manifest_id"]
        )

        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("automation.local_service", imports)
        self.assertNotIn("subprocess", imports)
        self.assertNotIn("google.cloud", source)
        self.assertNotIn("generate_statistics", source)
        self.assertNotIn("postprocessing", source)


if __name__ == "__main__":
    unittest.main()
