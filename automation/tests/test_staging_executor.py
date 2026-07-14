import ast
import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from automation.job_queue import JobType, build_job
from automation.staging_executor import (
    StagedProcessRequest,
    StagingCheckpoint,
    StagingCheckpointError,
    StagingCheckpointStore,
    StagingCheckpointStatus,
    StagingExecutionStatus,
    StagingExecutorConfig,
    StagingExecutorError,
    StagingReason,
    run_staged_scrape,
)


MODULE = Path(__file__).resolve().parents[1] / "staging_executor.py"


def build_scrape_job(*, suffix="scrape", venue_id="icml", year=2026):
    return build_job(
        request_id=f"request:{venue_id}:{year}:{suffix}",
        job_type=JobType.SCRAPE_EXISTING,
        venue_id=venue_id,
        year=year,
        requested_by="human",
        input_artifact_ids=(f"evidence:{venue_id}:{year}:{suffix}",),
        payload={
            "completeness_level": "archival",
            "download_pdfs": True,
            "expected_count": 100,
        },
    )


def build_validation_job():
    return build_job(
        request_id="request:icml:2026:validation",
        job_type=JobType.VALIDATE_CANDIDATE,
        venue_id="icml",
        year=2026,
        requested_by="human",
        input_artifact_ids=("manifest:icml:2026:candidate",),
        payload={
            "candidate_manifest_id": "manifest:icml:2026:candidate",
            "completeness_level": "archival",
            "require_pdfs": True,
            "expected_count": 100,
        },
    )


def build_codex_job():
    return build_job(
        request_id="request:icml:2026:codex",
        job_type=JobType.CODEX_DIAGNOSIS,
        venue_id="icml",
        year=2026,
        requested_by="human",
        input_artifact_ids=("failure:icml:2026:parser",),
        payload={
            "failure_fingerprint": "a" * 64,
            "snapshot_ids": ["snapshot:icml:2026:failure"],
            "allowed_paths": ["scrapers/icml.py"],
            "max_runtime_minutes": 30,
            "mode": "diagnose_only",
        },
    )


class TickClock:
    def __init__(self):
        self.value = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)

    def __call__(self):
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class FakeCancellation:
    def __init__(self, cancelled=False):
        self.cancelled = cancelled
        self.calls = 0

    def is_cancelled(self):
        self.calls += 1
        return self.cancelled


class FakeHandle:
    def __init__(
        self,
        exit_code,
        *,
        stopped=True,
        wait_error=None,
        terminate_error=None,
        stop_error=None,
        on_wait=None,
    ):
        self.exit_code = exit_code
        self.stopped = stopped
        self.wait_error = wait_error
        self.terminate_error = terminate_error
        self.stop_error = stop_error
        self.on_wait = on_wait
        self.wait_calls = []
        self.terminate_calls = 0
        self.stop_calls = []

    def wait(self, *, timeout_seconds, cancellation):
        self.wait_calls.append((timeout_seconds, cancellation))
        if self.on_wait is not None:
            self.on_wait()
        if self.wait_error is not None:
            raise self.wait_error
        return self.exit_code

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error

    def wait_stopped(self, *, timeout_seconds):
        self.stop_calls.append(timeout_seconds)
        if self.stop_error is not None:
            raise self.stop_error
        return self.stopped


class FakeLauncher:
    def __init__(self, handles=None, *, error=None, on_start=None):
        self.handles = list(handles or [])
        self.error = error
        self.on_start = on_start
        self.calls = []

    def start(self, request):
        self.calls.append(request)
        if self.on_start is not None:
            self.on_start(request)
        if self.error is not None:
            raise self.error
        return self.handles.pop(0)


class StagingExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        self.repository_root = root / "repository"
        self.repository_root.mkdir(mode=0o755)
        (self.repository_root / "main.py").write_text(
            "raise AssertionError('fake entry point must never run')\n",
            encoding="utf-8",
        )
        self.python_executable = root / "fake-python"
        self.python_executable.write_text(
            "#!/bin/sh\nexit 99\n",
            encoding="utf-8",
        )
        self.python_executable.chmod(0o700)
        self.staging_root = root / "staging"
        self.canonical_root = root / "canonical"
        self.canonical_root.mkdir(mode=0o700)
        self.config = StagingExecutorConfig(
            repository_root=self.repository_root,
            python_executable=self.python_executable,
            staging_root=self.staging_root,
            canonical_data_root=self.canonical_root,
            timeout_seconds=120,
            cancellation_grace_seconds=7,
        )
        self.clock = TickClock()
        self.job = build_scrape_job()

    def tearDown(self):
        self.temporary.cleanup()

    def run_job(self, launcher, *, job=None, config=None, cancellation=None):
        return run_staged_scrape(
            self.job if job is None else job,
            self.config if config is None else config,
            launcher,
            cancellation=cancellation,
            clock=self.clock,
        )

    def checkpoint_payload(self, job=None):
        selected = self.job if job is None else job
        path = (
            self.staging_root
            / selected["job_fingerprint"]
            / "checkpoint.v1.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_fixed_request_binds_only_private_staging_and_exact_environment(self):
        original = deepcopy(self.job)
        launcher = FakeLauncher([FakeHandle(0)])

        with patch.dict(
            os.environ,
            {"OPENPAPERS_TEST_SECRET": "must-not-be-inherited"},
        ):
            outcome = self.run_job(launcher)

        self.assertEqual(outcome.status, StagingExecutionStatus.PROCESS_SUCCEEDED)
        self.assertEqual(outcome.reason_code, StagingReason.EXIT_ZERO)
        self.assertEqual(self.job, original)
        self.assertEqual(len(launcher.calls), 1)
        request = launcher.calls[0]
        self.assertIsInstance(request, StagedProcessRequest)
        self.assertEqual(
            request.argv,
            (
                str(self.python_executable),
                str(self.repository_root / "main.py"),
                "icml",
                "2026",
                "--require-complete",
                "--completeness-level",
                "archival",
            ),
        )
        self.assertEqual(request.cwd, self.repository_root)
        self.assertEqual(
            request.environment(),
            {
                "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONUNBUFFERED": "1",
                "SCRAPER_DATA_ROOT": str(request.data_root),
                "SCRAPER_LOG_FILE": str(request.log_path),
            },
        )
        self.assertNotIn("OPENPAPERS_TEST_SECRET", request.environment())
        self.assertEqual(request.data_root.parent.parent, self.staging_root)
        self.assertEqual(request.log_path.parent.parent, request.data_root.parent)
        self.assertFalse(
            request.data_root == self.canonical_root
            or self.canonical_root in request.data_root.parents
            or request.data_root in self.canonical_root.parents
        )
        for path in (
            self.staging_root,
            request.data_root.parent,
            request.data_root,
            request.log_path.parent,
        ):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        checkpoint = self.checkpoint_payload()
        self.assertEqual(checkpoint["status"], "process_succeeded")
        self.assertEqual(checkpoint["attempt"], 1)
        self.assertFalse(request.log_path.exists())

    def test_exact_success_replay_skips_the_fake_launcher(self):
        launcher = FakeLauncher([FakeHandle(0)])

        first = self.run_job(launcher)
        replay = self.run_job(launcher)

        self.assertEqual(first.status, StagingExecutionStatus.PROCESS_SUCCEEDED)
        self.assertEqual(replay.status, StagingExecutionStatus.SKIPPED)
        self.assertEqual(
            replay.reason_code, StagingReason.DUPLICATE_PROCESS_SUCCESS
        )
        self.assertFalse(replay.started)
        self.assertFalse(replay.retry_permitted)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(self.checkpoint_payload()["attempt"], 1)

    def test_confirmed_failure_reuses_the_same_data_root_and_increments_attempt(self):
        launcher = FakeLauncher([FakeHandle(4), FakeHandle(0)])

        failed = self.run_job(launcher)
        first_request = launcher.calls[0]
        marker = first_request.data_root / "partial.fixture"
        marker.write_text("partial", encoding="utf-8")
        retried = self.run_job(launcher)

        self.assertEqual(failed.status, StagingExecutionStatus.FAILED)
        self.assertTrue(failed.retry_permitted)
        self.assertEqual(retried.status, StagingExecutionStatus.PROCESS_SUCCEEDED)
        self.assertEqual(failed.attempt, 1)
        self.assertEqual(retried.attempt, 2)
        self.assertEqual(launcher.calls[1].data_root, first_request.data_root)
        self.assertEqual(marker.read_text(encoding="utf-8"), "partial")
        self.assertNotIn("--no-resume", launcher.calls[1].argv)

    def test_timeout_and_inflight_cancellation_require_confirmed_stop_then_resume(self):
        timeout_handle = FakeHandle(None, stopped=True)
        timeout_launcher = FakeLauncher([timeout_handle, FakeHandle(0)])

        timed_out = self.run_job(timeout_launcher)
        resumed = self.run_job(timeout_launcher)

        self.assertEqual(timed_out.status, StagingExecutionStatus.TIMED_OUT)
        self.assertEqual(timed_out.reason_code, StagingReason.TIMEOUT_CONFIRMED)
        self.assertEqual(timeout_handle.terminate_calls, 1)
        self.assertEqual(timeout_handle.stop_calls, [7.0])
        self.assertEqual(resumed.attempt, 2)

        other_job = build_scrape_job(suffix="cancel")
        cancellation = FakeCancellation(cancelled=False)
        cancel_handle = FakeHandle(
            None,
            stopped=True,
            on_wait=lambda: setattr(cancellation, "cancelled", True),
        )
        cancelled = self.run_job(
            FakeLauncher([cancel_handle]),
            job=other_job,
            cancellation=cancellation,
        )
        self.assertEqual(cancelled.status, StagingExecutionStatus.CANCELLED)
        self.assertEqual(
            cancelled.reason_code, StagingReason.CANCELLATION_CONFIRMED
        )
        self.assertTrue(cancelled.retry_permitted)

    def test_prestart_cancellation_creates_no_attempt_and_can_resume(self):
        cancellation = FakeCancellation(cancelled=True)
        launcher = FakeLauncher([FakeHandle(0)])

        cancelled = self.run_job(launcher, cancellation=cancellation)
        cancellation.cancelled = False
        resumed = self.run_job(launcher, cancellation=cancellation)

        self.assertEqual(cancelled.status, StagingExecutionStatus.CANCELLED)
        self.assertEqual(cancelled.attempt, 0)
        self.assertFalse(cancelled.started)
        self.assertEqual(resumed.status, StagingExecutionStatus.PROCESS_SUCCEEDED)
        self.assertEqual(resumed.attempt, 1)
        self.assertEqual(len(launcher.calls), 1)

    def test_start_supervision_and_unconfirmed_stop_leave_recovery_blocker(self):
        scenarios = {
            "start": FakeLauncher(error=RuntimeError("injected start")),
            "wait": FakeLauncher(
                [FakeHandle(None, wait_error=RuntimeError("injected wait"))]
            ),
            "stop": FakeLauncher([FakeHandle(None, stopped=False)]),
        }
        for suffix, launcher in scenarios.items():
            job = build_scrape_job(suffix=suffix)
            with self.subTest(suffix=suffix):
                first = self.run_job(launcher, job=job)
                calls_after_first = len(launcher.calls)
                replay = self.run_job(launcher, job=job)
                self.assertEqual(
                    first.status, StagingExecutionStatus.RECOVERY_REQUIRED
                )
                self.assertFalse(first.retry_permitted)
                self.assertEqual(
                    replay.status, StagingExecutionStatus.RECOVERY_REQUIRED
                )
                self.assertEqual(replay.reason_code, StagingReason.ACTIVE_OR_AMBIGUOUS)
                self.assertEqual(len(launcher.calls), calls_after_first)
                self.assertEqual(
                    self.checkpoint_payload(job)["status"], "ambiguous"
                )

    def test_invalid_exit_code_is_ambiguous_and_not_retryable(self):
        launcher = FakeLauncher([FakeHandle("zero")])

        outcome = self.run_job(launcher)
        replay = self.run_job(launcher)

        self.assertEqual(outcome.status, StagingExecutionStatus.RECOVERY_REQUIRED)
        self.assertEqual(outcome.reason_code, StagingReason.SUPERVISION_FAILED)
        self.assertEqual(replay.status, StagingExecutionStatus.RECOVERY_REQUIRED)
        self.assertEqual(len(launcher.calls), 1)

    def test_validator_codex_forged_and_non_mapping_jobs_fail_before_staging(self):
        forged = build_scrape_job(suffix="forged")
        forged["job_id"] = "job:" + "0" * 64
        launcher = FakeLauncher([FakeHandle(0)])

        for candidate in (build_validation_job(), build_codex_job(), forged):
            with self.subTest(candidate=type(candidate).__name__), self.assertRaises(
                (StagingExecutorError, TypeError, ValueError)
            ):
                self.run_job(launcher, job=candidate)

        with self.assertRaises((StagingExecutorError, TypeError, ValueError)):
            run_staged_scrape(None, self.config, launcher, clock=self.clock)

        self.assertEqual(launcher.calls, [])
        self.assertFalse(self.staging_root.exists())

    def test_invalid_launcher_cancellation_and_clock_fail_before_staging(self):
        invalid_effects = (
            {"launcher": object()},
            {
                "launcher": FakeLauncher([FakeHandle(0)]),
                "cancellation": object(),
            },
            {
                "launcher": FakeLauncher([FakeHandle(0)]),
                "clock": lambda: datetime(2026, 7, 14, 18, 0),
            },
        )
        for kwargs in invalid_effects:
            with self.subTest(keys=sorted(kwargs)), self.assertRaises(
                (StagingExecutorError, TypeError)
            ):
                run_staged_scrape(self.job, self.config, **kwargs)
        self.assertFalse(self.staging_root.exists())

    def test_overlapping_unnormalized_and_unsafe_runtime_paths_fail_before_start(self):
        launcher = FakeLauncher([FakeHandle(0)])
        overlapping = StagingExecutorConfig(
            repository_root=self.repository_root,
            python_executable=self.python_executable,
            staging_root=self.canonical_root / "staging",
            canonical_data_root=self.canonical_root,
        )
        unnormalized = StagingExecutorConfig(
            repository_root=self.repository_root,
            python_executable=self.python_executable,
            staging_root=self.staging_root / ".." / "staging",
            canonical_data_root=self.canonical_root,
        )
        unsafe_entry = self.repository_root / "main.py"
        unsafe_entry.chmod(0o666)
        for name, config in (
            ("overlap", overlapping),
            ("unnormalized", unnormalized),
            ("unsafe", self.config),
        ):
            with self.subTest(name=name), self.assertRaises(StagingExecutorError):
                self.run_job(launcher, config=config)
        self.assertEqual(launcher.calls, [])

    def test_symlinked_staging_and_foreign_uncheckpointed_root_fail_closed(self):
        launcher = FakeLauncher([FakeHandle(0)])
        real_staging = self.staging_root.parent / "real-staging"
        real_staging.mkdir(mode=0o700)
        self.staging_root.symlink_to(real_staging, target_is_directory=True)
        with self.assertRaises(StagingExecutorError):
            self.run_job(launcher)
        self.staging_root.unlink()

        self.staging_root.mkdir(mode=0o700)
        foreign = self.staging_root / self.job["job_fingerprint"]
        foreign.mkdir(mode=0o700)
        (foreign / "data").mkdir(mode=0o700)
        (foreign / "logs").mkdir(mode=0o700)
        with self.assertRaises(StagingCheckpointError):
            self.run_job(launcher)
        self.assertEqual(launcher.calls, [])

    def test_corrupt_conflicting_and_unknown_staging_state_fail_before_replay(self):
        self.run_job(FakeLauncher([FakeHandle(3)]))
        job_root = self.staging_root / self.job["job_fingerprint"]
        checkpoint_path = job_root / "checkpoint.v1.json"
        payload = self.checkpoint_payload()
        payload["venue_id"] = "aistats"
        checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
        checkpoint_path.chmod(0o600)
        launcher = FakeLauncher([FakeHandle(0)])
        with self.assertRaises(StagingCheckpointError):
            self.run_job(launcher)

        checkpoint_path.write_text("not json", encoding="utf-8")
        (job_root / "foreign.txt").write_text("foreign", encoding="utf-8")
        with self.assertRaises(StagingCheckpointError):
            self.run_job(launcher)
        self.assertEqual(launcher.calls, [])

    def test_checkpoint_store_rejects_skipped_or_post_success_transitions(self):
        self.run_job(FakeLauncher([FakeHandle(3)]))
        job_root = self.staging_root / self.job["job_fingerprint"]
        store = StagingCheckpointStore(job_root, self.job)
        failed = store.read()
        forged_success = StagingCheckpoint(
            job_id=failed.job_id,
            job_fingerprint=failed.job_fingerprint,
            job_type=failed.job_type,
            venue_id=failed.venue_id,
            year=failed.year,
            attempt=failed.attempt,
            status=StagingCheckpointStatus.PROCESS_SUCCEEDED,
            reason_code=StagingReason.EXIT_ZERO,
            updated_at="2026-07-14T19:00:00Z",
        )
        with self.assertRaisesRegex(StagingCheckpointError, "transition"):
            store.write(forged_success, previous=failed)

        succeeded = self.run_job(FakeLauncher([FakeHandle(0)]))
        self.assertEqual(succeeded.status, StagingExecutionStatus.PROCESS_SUCCEEDED)
        completed = store.read()
        post_success_failure = StagingCheckpoint(
            job_id=completed.job_id,
            job_fingerprint=completed.job_fingerprint,
            job_type=completed.job_type,
            venue_id=completed.venue_id,
            year=completed.year,
            attempt=completed.attempt,
            status=StagingCheckpointStatus.FAILED,
            reason_code=StagingReason.EXIT_NONZERO,
            updated_at="2026-07-14T19:00:01Z",
        )
        with self.assertRaisesRegex(StagingCheckpointError, "transition"):
            store.write(post_success_failure, previous=completed)

    def test_checkpoint_and_observation_are_bounded_and_path_secret_free(self):
        outcome = self.run_job(FakeLauncher([FakeHandle(9)]))
        retained = json.dumps(self.checkpoint_payload(), sort_keys=True)
        serialized = json.dumps(outcome.as_dict(), sort_keys=True) + retained
        for forbidden in (
            str(self.repository_root),
            str(self.staging_root),
            str(self.canonical_root),
            "SCRAPER_DATA_ROOT",
            "SCRAPER_LOG_FILE",
            "argv",
            "environment",
            "artifact",
            "manifest",
            "validation",
            "command",
            "must-not-be-inherited",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_module_is_unwired_and_tests_use_no_scraper_or_validator_import(self):
        source = MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
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
        for forbidden in (
            "main",
            "scrapers",
            "postprocessing.validate_year",
            "automation.local_service",
            "automation.local_scheduler",
            "automation.mac_worker.runtime",
            "prefect",
            "google",
        ):
            self.assertNotIn(forbidden, imports)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("load_dotenv", source)


if __name__ == "__main__":
    unittest.main()
