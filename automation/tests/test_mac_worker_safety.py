import json
import multiprocessing
import tempfile
import unittest
from collections import namedtuple
from copy import deepcopy
from pathlib import Path

from automation.job_queue import JobType, build_job, build_queue_envelope
from automation.mac_worker.safety import (
    DiskSpacePolicy,
    FixtureExecutionOutcome,
    FixtureOutcomeStatus,
    JournalCorruptionError,
    JournalState,
    LocalJobJournal,
    OfflineQueuePolicy,
    WorkerJobReason,
    WorkerJobStatus,
    WorkerSafetyConfig,
    WorkerSafetyError,
    offline_queue_policy,
    run_guarded_fixture_job,
)


DiskUsage = namedtuple("DiskUsage", "total used free")


def fixture_envelope(*, venue_id="icml", year=2026, suffix="scrape"):
    job = build_job(
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
    return build_queue_envelope(job).as_dict()


def sufficient_disk(_path):
    return DiskUsage(total=100_000, used=20_000, free=80_000)


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
        outcome,
        *,
        stopped=True,
        wait_error=None,
        cancel_error=None,
        stop_error=None,
        on_wait=None,
    ):
        self.outcome = outcome
        self.stopped = stopped
        self.wait_error = wait_error
        self.cancel_error = cancel_error
        self.stop_error = stop_error
        self.on_wait = on_wait
        self.wait_calls = []
        self.cancel_calls = 0
        self.stop_calls = []

    def wait(self, *, timeout_seconds, cancellation):
        self.wait_calls.append((timeout_seconds, cancellation))
        if self.on_wait is not None:
            self.on_wait()
        if self.wait_error is not None:
            raise self.wait_error
        return self.outcome

    def cancel(self):
        self.cancel_calls += 1
        if self.cancel_error is not None:
            raise self.cancel_error

    def wait_stopped(self, *, timeout_seconds):
        self.stop_calls.append(timeout_seconds)
        if self.stop_error is not None:
            raise self.stop_error
        return self.stopped


class FakeStarter:
    def __init__(self, handles, *, error=None, on_start=None):
        if not isinstance(handles, list):
            handles = [handles]
        self.handles = list(handles)
        self.error = error
        self.on_start = on_start
        self.calls = []

    def start(self, job):
        self.calls.append(deepcopy(job))
        if self.on_start is not None:
            self.on_start(job)
        if self.error is not None:
            raise self.error
        return self.handles.pop(0)


SUCCESS = FixtureExecutionOutcome(
    FixtureOutcomeStatus.SUCCEEDED,
    WorkerJobReason.FIXTURE_SUCCEEDED,
)
FAILURE = FixtureExecutionOutcome(
    FixtureOutcomeStatus.FAILED,
    WorkerJobReason.FIXTURE_FAILED,
)


def _try_lock_in_child(root, job, output):
    journal = LocalJobJournal(Path(root))
    with journal.try_venue_year_lock(job) as acquired:
        output.put(acquired)


class MacWorkerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        self.state_root = root / "state"
        self.data_root = root / "data"
        self.data_root.mkdir(mode=0o700)
        self.config = WorkerSafetyConfig(
            state_root=self.state_root,
            data_root=self.data_root,
            timeout_seconds=120,
            cancellation_grace_seconds=7,
            disk_policy=DiskSpacePolicy(
                minimum_free_bytes=20_000,
                minimum_free_fraction=0.20,
            ),
        )
        self.envelope = fixture_envelope()

    def tearDown(self):
        self.temporary.cleanup()

    def run_job(
        self,
        starter,
        *,
        envelope=None,
        cancellation=None,
        disk_usage=sufficient_disk,
    ):
        return run_guarded_fixture_job(
            self.envelope if envelope is None else envelope,
            self.config,
            starter,
            cancellation=cancellation,
            disk_usage=disk_usage,
        )

    def test_success_is_claimed_before_start_and_completed_replay_is_suppressed(self):
        job = self.envelope["job"]
        original = deepcopy(self.envelope)

        def assert_claimed(started_job):
            journal = LocalJobJournal(self.state_root)
            self.assertEqual(started_job, job)
            self.assertEqual(journal.inspect(job), JournalState.ACTIVE)

        handle = FakeHandle(SUCCESS)
        starter = FakeStarter(handle, on_start=assert_claimed)

        first = self.run_job(starter)
        replay = self.run_job(starter)

        self.assertEqual(first.status, WorkerJobStatus.COMPLETED)
        self.assertEqual(first.reason_code, WorkerJobReason.FIXTURE_SUCCEEDED)
        self.assertTrue(first.started)
        self.assertFalse(first.retry_permitted)
        self.assertEqual(replay.status, WorkerJobStatus.SKIPPED)
        self.assertEqual(replay.reason_code, WorkerJobReason.DUPLICATE_COMPLETED)
        self.assertFalse(replay.started)
        self.assertEqual(len(starter.calls), 1)
        self.assertEqual(self.envelope, original)
        journal = LocalJobJournal(self.state_root)
        self.assertEqual(journal.inspect(job), JournalState.COMPLETED)
        self.assertEqual(list(journal.claims_root.glob("*.json")), [])
        completed = list(journal.completed_root.glob("*.json"))
        self.assertEqual(len(completed), 1)
        retained = completed[0].read_text(encoding="utf-8")
        serialized = json.dumps(first.as_dict(), sort_keys=True) + retained
        for forbidden in (
            str(self.state_root),
            str(self.data_root),
            "command",
            "artifact_ids",
            "result_fingerprint",
            "completed_at",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_insufficient_disk_and_pre_start_cancellation_create_no_claim(self):
        starter = FakeStarter(FakeHandle(SUCCESS))
        disk_refusals = [
            self.run_job(
                starter,
                disk_usage=lambda _path: DiskUsage(
                    total=50_000, used=35_000, free=15_000
                ),
            ),
            self.run_job(
                starter,
                disk_usage=lambda _path: DiskUsage(
                    total=200_000, used=170_000, free=30_000
                ),
            ),
        ]
        cancelled = self.run_job(
            starter,
            cancellation=FakeCancellation(cancelled=True),
        )

        for disk_refusal in disk_refusals:
            self.assertEqual(disk_refusal.status, WorkerJobStatus.REFUSED)
            self.assertEqual(
                disk_refusal.reason_code,
                WorkerJobReason.INSUFFICIENT_DISK,
            )
        self.assertEqual(cancelled.status, WorkerJobStatus.CANCELLED)
        self.assertEqual(
            cancelled.reason_code,
            WorkerJobReason.CANCELLED_BEFORE_START,
        )
        self.assertEqual(starter.calls, [])
        journal = LocalJobJournal(self.state_root)
        self.assertEqual(journal.inspect(self.envelope["job"]), JournalState.ABSENT)

    def test_confirmed_failure_clears_claim_and_same_id_can_retry(self):
        starter = FakeStarter([FakeHandle(FAILURE), FakeHandle(SUCCESS)])

        failed = self.run_job(starter)
        retried = self.run_job(starter)

        self.assertEqual(failed.status, WorkerJobStatus.FAILED)
        self.assertTrue(failed.retry_permitted)
        self.assertEqual(retried.status, WorkerJobStatus.COMPLETED)
        self.assertEqual(len(starter.calls), 2)
        self.assertEqual(
            LocalJobJournal(self.state_root).inspect(self.envelope["job"]),
            JournalState.COMPLETED,
        )

    def test_confirmed_timeout_and_inflight_cancellation_clear_claim_for_retry(self):
        timeout_handle = FakeHandle(None, stopped=True)
        timeout_starter = FakeStarter(timeout_handle)

        timed_out = self.run_job(timeout_starter)

        self.assertEqual(timed_out.status, WorkerJobStatus.TIMED_OUT)
        self.assertEqual(timed_out.reason_code, WorkerJobReason.RUNTIME_EXCEEDED)
        self.assertEqual(timeout_handle.cancel_calls, 1)
        self.assertEqual(timeout_handle.stop_calls, [7.0])
        self.assertEqual(timeout_handle.wait_calls[0][0], 120.0)
        self.assertEqual(
            LocalJobJournal(self.state_root).inspect(self.envelope["job"]),
            JournalState.ABSENT,
        )

        signal = FakeCancellation()
        cancelled_handle = FakeHandle(
            None,
            stopped=True,
            on_wait=lambda: setattr(signal, "cancelled", True),
        )
        cancelled = self.run_job(
            FakeStarter(cancelled_handle),
            cancellation=signal,
        )

        self.assertEqual(cancelled.status, WorkerJobStatus.CANCELLED)
        self.assertEqual(
            cancelled.reason_code,
            WorkerJobReason.CANCELLATION_REQUESTED,
        )
        self.assertTrue(cancelled.retry_permitted)
        self.assertEqual(cancelled_handle.cancel_calls, 1)

    def test_unconfirmed_stop_and_supervision_failure_leave_ambiguous_claim(self):
        cases = (
            FakeStarter(FakeHandle(None, stopped=False)),
            FakeStarter(FakeHandle(None, cancel_error=RuntimeError("token=secret"))),
            FakeStarter(FakeHandle(None, wait_error=RuntimeError("password=secret"))),
            FakeStarter(FakeHandle("invalid outcome")),
            FakeStarter(FakeHandle(SUCCESS), error=RuntimeError("api_key=secret")),
        )
        for index, starter in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                data_root = root / "data"
                data_root.mkdir(mode=0o700)
                config = WorkerSafetyConfig(
                    state_root=root / "state",
                    data_root=data_root,
                    timeout_seconds=1,
                    cancellation_grace_seconds=1,
                    disk_policy=DiskSpacePolicy(1, 0.01),
                )
                envelope = fixture_envelope(suffix=f"ambiguous-{index}")

                observation = run_guarded_fixture_job(
                    envelope,
                    config,
                    starter,
                    disk_usage=sufficient_disk,
                )
                replay = run_guarded_fixture_job(
                    envelope,
                    config,
                    FakeStarter(FakeHandle(SUCCESS)),
                    disk_usage=sufficient_disk,
                )

                self.assertEqual(
                    observation.status,
                    WorkerJobStatus.RECOVERY_REQUIRED,
                )
                self.assertFalse(observation.retry_permitted)
                self.assertNotIn("secret", json.dumps(observation.as_dict()))
                self.assertEqual(replay.status, WorkerJobStatus.RECOVERY_REQUIRED)
                self.assertEqual(
                    replay.reason_code,
                    WorkerJobReason.ACTIVE_CLAIM_EXISTS,
                )
                self.assertEqual(
                    LocalJobJournal(config.state_root).inspect(envelope["job"]),
                    JournalState.ACTIVE,
                )

    def test_existing_active_claim_and_corrupt_completed_record_fail_closed(self):
        journal = LocalJobJournal(self.state_root)
        job = self.envelope["job"]
        journal.create_claim(job)
        starter = FakeStarter(FakeHandle(SUCCESS))

        active = self.run_job(starter)

        self.assertEqual(active.status, WorkerJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(active.reason_code, WorkerJobReason.ACTIVE_CLAIM_EXISTS)
        self.assertEqual(starter.calls, [])

        journal.mark_completed(job)
        completed_path = next(journal.completed_root.glob("*.json"))
        payload = json.loads(completed_path.read_text(encoding="utf-8"))
        payload["unexpected"] = True
        completed_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(JournalCorruptionError, "conflicts"):
            self.run_job(starter)

    def test_ambiguous_claim_blocks_a_different_job_for_the_same_venue_year(self):
        journal = LocalJobJournal(self.state_root)
        first_job = self.envelope["job"]
        journal.create_claim(first_job)
        other_envelope = fixture_envelope(suffix="different-delivery")
        starter = FakeStarter(FakeHandle(SUCCESS))

        blocked = self.run_job(starter, envelope=other_envelope)
        different_year = self.run_job(
            starter,
            envelope=fixture_envelope(year=2027),
        )

        self.assertEqual(blocked.status, WorkerJobStatus.RECOVERY_REQUIRED)
        self.assertEqual(blocked.reason_code, WorkerJobReason.ACTIVE_CLAIM_EXISTS)
        self.assertFalse(blocked.started)
        self.assertEqual(different_year.status, WorkerJobStatus.COMPLETED)
        self.assertEqual(len(starter.calls), 1)

    def test_venue_year_lock_is_process_safe_and_other_year_is_independent(self):
        journal = LocalJobJournal(self.state_root)
        context = multiprocessing.get_context("spawn")
        same_job = self.envelope["job"]
        other_job = fixture_envelope(year=2027)["job"]

        with journal.try_venue_year_lock(same_job) as acquired:
            self.assertTrue(acquired)
            busy = self.run_job(FakeStarter(FakeHandle(SUCCESS)))
            self.assertEqual(busy.status, WorkerJobStatus.REFUSED)
            self.assertEqual(busy.reason_code, WorkerJobReason.VENUE_YEAR_BUSY)
            same_output = context.Queue()
            same = context.Process(
                target=_try_lock_in_child,
                args=(str(self.state_root), same_job, same_output),
            )
            other_output = context.Queue()
            other = context.Process(
                target=_try_lock_in_child,
                args=(str(self.state_root), other_job, other_output),
            )
            same.start()
            other.start()
            same.join(timeout=5)
            other.join(timeout=5)
            self.assertFalse(same.is_alive())
            self.assertFalse(other.is_alive())
            self.assertEqual(same.exitcode, 0)
            self.assertEqual(other.exitcode, 0)
            self.assertFalse(same_output.get(timeout=1))
            self.assertTrue(other_output.get(timeout=1))

    def test_invalid_input_disk_or_private_state_fails_before_fake_start(self):
        starter = FakeStarter(FakeHandle(SUCCESS))
        forged = deepcopy(self.envelope)
        forged["job"]["job_id"] = "job:" + "0" * 64
        unused_root = self.state_root.parent / "unused"
        invalid_config = WorkerSafetyConfig(
            state_root=unused_root,
            data_root=self.data_root,
            disk_policy=DiskSpacePolicy(1, 0.01),
        )
        with self.assertRaises(ValueError):
            run_guarded_fixture_job(
                forged,
                invalid_config,
                starter,
                disk_usage=sufficient_disk,
            )
        self.assertFalse(unused_root.exists())

        for provider in (
            lambda _path: DiskUsage(total=0, used=0, free=0),
            lambda _path: (_ for _ in ()).throw(RuntimeError("token=secret")),
        ):
            with self.subTest(provider=provider):
                with self.assertRaisesRegex(
                    WorkerSafetyError, "disk usage check"
                ) as caught:
                    self.run_job(starter, disk_usage=provider)
                self.assertIsNone(caught.exception.__cause__)
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn(str(self.data_root), str(caught.exception))
        self.assertEqual(starter.calls, [])

        unsafe_root = self.state_root.parent / "unsafe"
        unsafe_root.mkdir(mode=0o755)
        unsafe_root.chmod(0o755)
        unsafe_config = WorkerSafetyConfig(
            state_root=unsafe_root,
            data_root=self.data_root,
            disk_policy=DiskSpacePolicy(1, 0.01),
        )
        with self.assertRaisesRegex(WorkerSafetyError, "metadata is unsafe"):
            run_guarded_fixture_job(
                self.envelope,
                unsafe_config,
                starter,
                disk_usage=sufficient_disk,
            )

        traversal = deepcopy(self.envelope["job"])
        traversal["job_fingerprint"] = "../unsafe"
        with self.assertRaises(ValueError):
            LocalJobJournal(self.state_root).inspect(traversal)

    def test_configuration_outcomes_and_offline_policy_are_closed(self):
        with self.assertRaisesRegex(ValueError, "absolute Path"):
            WorkerSafetyConfig(
                state_root=Path("relative"),
                data_root=self.data_root,
            )
        for args in ((0, 0.1), (1, 0), (True, 0.1), (1, 1.1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                DiskSpacePolicy(*args)
        with self.assertRaises(ValueError):
            WorkerSafetyConfig(
                state_root=self.state_root,
                data_root=self.data_root,
                timeout_seconds=0,
            )
        with self.assertRaises(ValueError):
            FixtureExecutionOutcome(
                FixtureOutcomeStatus.SUCCEEDED,
                WorkerJobReason.FIXTURE_FAILED,
            )

        offline_root = self.state_root.parent / "offline-unused"
        policy = offline_queue_policy()

        self.assertEqual(policy.queue_owner, "prefect")
        self.assertEqual(policy.delivery_mode, "pull")
        self.assertEqual(policy.unavailable_job_state, "queued")
        self.assertFalse(policy.local_buffering)
        self.assertFalse(policy.local_expiry)
        self.assertFalse(policy.local_resubmission)
        self.assertTrue(policy.preserve_job_id)
        self.assertFalse(offline_root.exists())
        with self.assertRaisesRegex(ValueError, "fixed"):
            OfflineQueuePolicy(queue_owner="local")


if __name__ == "__main__":
    unittest.main()
