"""Local P4.3 safety supervision for fake Mac worker jobs.

This module owns only Mac-local delivery safety state. It does not select or
run commands, publish job results, contact Prefect or GCS, or mutate the cloud
control-plane database.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

from automation.domain import assert_secret_free
from automation.job_queue import (
    JobType,
    validate_job_identity,
    validate_queue_envelope,
)


_JOURNAL_SCHEMA_VERSION = 1
_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "job_id",
        "job_fingerprint",
        "job_type",
        "venue_id",
        "year",
    }
)
_MAX_RUNTIME_SECONDS = 24 * 60 * 60
_MAX_CANCELLATION_GRACE_SECONDS = 15 * 60
_JOB_TYPE_VALUES = frozenset(item.value for item in JobType)


class WorkerSafetyError(ValueError):
    """Raised when local safety configuration or state fails closed."""


class JournalCorruptionError(WorkerSafetyError):
    """Raised when retained local job safety state is malformed or conflicting."""


class JournalState(str, Enum):
    """Local delivery state; this is not a P4.4 job-result status."""

    ABSENT = "absent"
    ACTIVE = "active"
    COMPLETED = "completed"


class FixtureOutcomeStatus(str, Enum):
    """Closed fake-executor terminal vocabulary used before Phase 5."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkerJobStatus(str, Enum):
    """Bounded local observation states that cannot be mistaken for results."""

    COMPLETED = "fixture_completed"
    SKIPPED = "skipped"
    REFUSED = "refused"
    FAILED = "fixture_failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    RECOVERY_REQUIRED = "recovery_required"


class WorkerJobReason(str, Enum):
    """Secret-free reasons for local P4.3 observations."""

    FIXTURE_SUCCEEDED = "fixture_succeeded"
    FIXTURE_FAILED = "fixture_failed"
    DUPLICATE_COMPLETED = "duplicate_completed"
    VENUE_YEAR_BUSY = "venue_year_busy"
    INSUFFICIENT_DISK = "insufficient_disk"
    CANCELLED_BEFORE_START = "cancelled_before_start"
    CANCELLATION_REQUESTED = "cancellation_requested"
    RUNTIME_EXCEEDED = "runtime_exceeded"
    ACTIVE_CLAIM_EXISTS = "active_claim_exists"
    CANCELLATION_UNCONFIRMED = "cancellation_unconfirmed"
    SUPERVISION_FAILED = "supervision_failed"


@dataclass(frozen=True)
class DiskSpacePolicy:
    """Minimum free capacity required before a local job may start."""

    minimum_free_bytes: int = 10 * 1024 * 1024 * 1024
    minimum_free_fraction: float = 0.10

    def __post_init__(self) -> None:
        if (
            not isinstance(self.minimum_free_bytes, int)
            or isinstance(self.minimum_free_bytes, bool)
            or not 1 <= self.minimum_free_bytes <= (1 << 63) - 1
        ):
            raise ValueError("minimum_free_bytes must be a positive bounded integer")
        if (
            not isinstance(self.minimum_free_fraction, (int, float))
            or isinstance(self.minimum_free_fraction, bool)
            or not 0 < float(self.minimum_free_fraction) <= 1
        ):
            raise ValueError("minimum_free_fraction must be in (0, 1]")


@dataclass(frozen=True)
class WorkerSafetyConfig:
    """Explicit local-only safety configuration; paths are never reported."""

    state_root: Path
    data_root: Path
    timeout_seconds: float = 60 * 60
    cancellation_grace_seconds: float = 30
    disk_policy: DiskSpacePolicy = DiskSpacePolicy()

    def __post_init__(self) -> None:
        for value in (self.state_root, self.data_root):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError("worker safety roots must be absolute Path values")
        if not isinstance(self.disk_policy, DiskSpacePolicy):
            raise TypeError("disk_policy must be DiskSpacePolicy")
        _validate_seconds(
            self.timeout_seconds,
            maximum=_MAX_RUNTIME_SECONDS,
            field="timeout_seconds",
        )
        _validate_seconds(
            self.cancellation_grace_seconds,
            maximum=_MAX_CANCELLATION_GRACE_SECONDS,
            field="cancellation_grace_seconds",
        )


@dataclass(frozen=True)
class FixtureExecutionOutcome:
    """A terminal fake outcome, deliberately not a versioned job result."""

    status: FixtureOutcomeStatus
    reason_code: WorkerJobReason

    def __post_init__(self) -> None:
        if not isinstance(self.status, FixtureOutcomeStatus):
            raise TypeError("fixture outcome status must be FixtureOutcomeStatus")
        if not isinstance(self.reason_code, WorkerJobReason):
            raise TypeError("fixture outcome reason must be WorkerJobReason")
        expected = {
            FixtureOutcomeStatus.SUCCEEDED: WorkerJobReason.FIXTURE_SUCCEEDED,
            FixtureOutcomeStatus.FAILED: WorkerJobReason.FIXTURE_FAILED,
        }
        if self.reason_code is not expected[self.status]:
            raise ValueError("fixture outcome status and reason do not match")


@dataclass(frozen=True)
class WorkerJobObservation:
    """Bounded local supervision observation with no artifact/result claim."""

    status: WorkerJobStatus
    reason_code: WorkerJobReason
    job_id: str
    job_type: str
    venue_id: str
    year: int
    started: bool
    retry_permitted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "job_id": self.job_id,
            "job_type": self.job_type,
            "venue_id": self.venue_id,
            "year": self.year,
            "started": self.started,
            "retry_permitted": self.retry_permitted,
        }


@dataclass(frozen=True)
class OfflineQueuePolicy:
    """Fixed no-delivery behavior for the future Prefect pull worker."""

    queue_owner: str = "prefect"
    delivery_mode: str = "pull"
    unavailable_job_state: str = "queued"
    local_buffering: bool = False
    local_expiry: bool = False
    local_resubmission: bool = False
    preserve_job_id: bool = True

    def __post_init__(self) -> None:
        expected = (
            "prefect",
            "pull",
            "queued",
            False,
            False,
            False,
            True,
        )
        actual = (
            self.queue_owner,
            self.delivery_mode,
            self.unavailable_job_state,
            self.local_buffering,
            self.local_expiry,
            self.local_resubmission,
            self.preserve_job_id,
        )
        if actual != expected:
            raise ValueError("offline queue policy is fixed at the P4.3 boundary")


class CancellationSignal(Protocol):
    """Injected cancellation state observed by a concrete execution handle."""

    def is_cancelled(self) -> bool:
        """Return whether the current delivery has been asked to stop."""


class ExecutionHandle(Protocol):
    """Minimal supervision surface for a future approved local executor."""

    def wait(
        self,
        *,
        timeout_seconds: float,
        cancellation: CancellationSignal,
    ) -> FixtureExecutionOutcome | None:
        """Return a terminal outcome, or ``None`` on timeout/cancellation."""

    def cancel(self) -> None:
        """Request termination without assuming it has completed."""

    def wait_stopped(self, *, timeout_seconds: float) -> bool:
        """Return true only when termination is confirmed within the grace."""


class ExecutionStarter(Protocol):
    """Injected typed boundary; no starter or command comes from the job."""

    def start(self, job: Mapping[str, Any]) -> ExecutionHandle:
        """Start handling one already validated closed typed job."""


class _NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def _validate_seconds(value: float, *, maximum: float, field: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 < float(value) <= maximum
    ):
        raise ValueError(f"{field} must be positive and at most {maximum:g}")


def offline_queue_policy() -> OfflineQueuePolicy:
    """Return the fixed Prefect-owned pull/offline contract."""
    return OfflineQueuePolicy()


def _job_record(job: Mapping[str, Any]) -> dict[str, Any]:
    validate_job_identity(job)
    record = {
        "schema_version": _JOURNAL_SCHEMA_VERSION,
        "job_id": job["job_id"],
        "job_fingerprint": job["job_fingerprint"],
        "job_type": job["job_type"],
        "venue_id": job["venue_id"],
        "year": job["year"],
    }
    assert_secret_free(record)
    return record


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path_stat = path.lstat()
    except OSError:
        raise WorkerSafetyError("worker safety directory is unavailable") from None
    safe = (
        stat.S_ISDIR(path_stat.st_mode)
        and not path.is_symlink()
        and path_stat.st_uid == os.getuid()
        and path_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    )
    if not safe:
        raise WorkerSafetyError("worker safety directory metadata is unsafe")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class LocalJobJournal:
    """Mac-owned claim/completion journal distinct from P4.4 job results."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("journal root must be an absolute Path")
        self.root = root
        self.claims_root = root / "claims"
        self.completed_root = root / "completed"
        self.locks_root = root / "locks"
        for path in (
            self.root,
            self.claims_root,
            self.completed_root,
            self.locks_root,
        ):
            _ensure_private_directory(path)

    @staticmethod
    def _fingerprint(job: Mapping[str, Any]) -> str:
        return str(job["job_fingerprint"])

    def _claim_path(self, job: Mapping[str, Any]) -> Path:
        return self.claims_root / f"{self._fingerprint(job)}.json"

    def _completed_path(self, job: Mapping[str, Any]) -> Path:
        return self.completed_root / f"{self._fingerprint(job)}.json"

    def _read_record(
        self,
        path: Path,
        expected: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            path_stat = path.lstat()
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or path.is_symlink()
                or path_stat.st_uid != os.getuid()
                or path_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            ):
                raise JournalCorruptionError(
                    "worker journal record metadata is unsafe"
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
        except JournalCorruptionError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise JournalCorruptionError(
                "worker journal record is unreadable"
            ) from None
        valid_shape = (
            isinstance(payload, dict)
            and frozenset(payload) == _RECORD_KEYS
            and isinstance(payload.get("schema_version"), int)
            and not isinstance(payload["schema_version"], bool)
            and payload["schema_version"] == _JOURNAL_SCHEMA_VERSION
            and isinstance(payload.get("job_fingerprint"), str)
            and re.fullmatch(r"[a-f0-9]{64}", payload["job_fingerprint"])
            is not None
            and payload.get("job_id") == f"job:{payload['job_fingerprint']}"
            and isinstance(payload.get("job_type"), str)
            and payload["job_type"] in _JOB_TYPE_VALUES
            and isinstance(payload.get("venue_id"), str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", payload["venue_id"])
            is not None
            and isinstance(payload.get("year"), int)
            and not isinstance(payload["year"], bool)
            and 1900 <= payload["year"] <= 2200
        )
        if (
            not valid_shape
            or path.name != f"{payload['job_fingerprint']}.json"
            or (expected is not None and payload != dict(expected))
        ):
            raise JournalCorruptionError("worker journal record conflicts with the job")
        assert_secret_free(payload)
        return payload

    def inspect(self, job: Mapping[str, Any]) -> JournalState:
        """Return strict local state for one already validated job."""
        expected = _job_record(job)
        claim_path = self._claim_path(job)
        completed_path = self._completed_path(job)
        claim_exists = claim_path.exists() or claim_path.is_symlink()
        completed_exists = completed_path.exists() or completed_path.is_symlink()
        if claim_exists and completed_exists:
            self._read_record(claim_path, expected)
            self._read_record(completed_path, expected)
            raise JournalCorruptionError("job has both active and completed records")
        if completed_exists:
            self._read_record(completed_path, expected)
            return JournalState.COMPLETED
        if claim_exists:
            self._read_record(claim_path, expected)
            return JournalState.ACTIVE
        return JournalState.ABSENT

    def has_active_venue_year_claim(self, job: Mapping[str, Any]) -> bool:
        """Return whether any exact validated claim blocks this venue/year."""
        expected = _job_record(job)
        try:
            paths = sorted(self.claims_root.iterdir())
        except OSError:
            raise WorkerSafetyError("worker claims could not be inspected") from None
        for path in paths:
            if path.suffix != ".json":
                raise JournalCorruptionError("worker claims contain an unknown record")
            payload = self._read_record(path)
            if (
                payload["venue_id"] == expected["venue_id"]
                and payload["year"] == expected["year"]
            ):
                return True
        return False

    def create_claim(self, job: Mapping[str, Any]) -> None:
        """Durably claim a job before an injected executor may start."""
        if self.inspect(job) is not JournalState.ABSENT:
            raise WorkerSafetyError("job already has local journal state")
        path = self._claim_path(job)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as file_obj:
                file_obj.write(_canonical_bytes(_job_record(job)))
                file_obj.flush()
                os.fsync(file_obj.fileno())
            _fsync_directory(self.claims_root)
        except FileExistsError:
            raise WorkerSafetyError("job claim already exists") from None
        except OSError:
            raise WorkerSafetyError("job claim could not be retained") from None

    def clear_claim(self, job: Mapping[str, Any]) -> None:
        """Remove only an exact active claim after confirmed safe stop."""
        if self.inspect(job) is not JournalState.ACTIVE:
            raise WorkerSafetyError("job does not have one active claim")
        try:
            self._claim_path(job).unlink()
            _fsync_directory(self.claims_root)
        except OSError:
            raise WorkerSafetyError("job claim could not be cleared") from None

    def mark_completed(self, job: Mapping[str, Any]) -> None:
        """Atomically promote one exact claim to a local completion marker."""
        if self.inspect(job) is not JournalState.ACTIVE:
            raise WorkerSafetyError("job does not have one active claim")
        completed_path = self._completed_path(job)
        if completed_path.exists() or completed_path.is_symlink():
            raise JournalCorruptionError("job completion marker already exists")
        try:
            os.replace(self._claim_path(job), completed_path)
            _fsync_directory(self.claims_root)
            _fsync_directory(self.completed_root)
        except OSError:
            raise WorkerSafetyError(
                "job completion marker could not be retained"
            ) from None
        if self.inspect(job) is not JournalState.COMPLETED:
            raise JournalCorruptionError("job completion promotion did not persist")

    @contextmanager
    def try_venue_year_lock(
        self,
        job: Mapping[str, Any],
    ) -> Iterator[bool]:
        """Try one process-safe venue/year lease without waiting."""
        validate_job_identity(job)
        venue_id = job["venue_id"]
        year = job["year"]
        lock_path = self.locks_root / f"{venue_id}-{year}.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            descriptor_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or descriptor_stat.st_uid != os.getuid()
                or descriptor_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            ):
                os.close(descriptor)
                raise WorkerSafetyError("venue/year lock metadata is unsafe")
            handle = os.fdopen(descriptor, "a+b", buffering=0)
        except OSError:
            raise WorkerSafetyError("venue/year lock is unavailable") from None
        acquired = False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            except OSError:
                raise WorkerSafetyError("venue/year lock failed") from None
            yield acquired
        finally:
            if acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    raise WorkerSafetyError("venue/year unlock failed") from None
            handle.close()


def _disk_is_sufficient(
    data_root: Path,
    policy: DiskSpacePolicy,
    disk_usage: Callable[[Path], Any],
) -> bool:
    try:
        usage = disk_usage(data_root)
        total = usage.total
        free = usage.free
    except Exception:
        raise WorkerSafetyError("disk usage check failed") from None
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or not isinstance(free, int)
        or isinstance(free, bool)
        or total <= 0
        or not 0 <= free <= total
    ):
        raise WorkerSafetyError("disk usage check returned invalid values")
    return (
        free >= policy.minimum_free_bytes
        and free / total >= float(policy.minimum_free_fraction)
    )


def _observation(
    job: Mapping[str, Any],
    status: WorkerJobStatus,
    reason: WorkerJobReason,
    *,
    started: bool,
    retry_permitted: bool,
) -> WorkerJobObservation:
    return WorkerJobObservation(
        status=status,
        reason_code=reason,
        job_id=job["job_id"],
        job_type=job["job_type"],
        venue_id=job["venue_id"],
        year=job["year"],
        started=started,
        retry_permitted=retry_permitted,
    )


def _recovery_observation(
    job: Mapping[str, Any],
    reason: WorkerJobReason,
    *,
    started: bool,
) -> WorkerJobObservation:
    return _observation(
        job,
        WorkerJobStatus.RECOVERY_REQUIRED,
        reason,
        started=started,
        retry_permitted=False,
    )


def run_guarded_fixture_job(
    queue_envelope: Mapping[str, Any],
    config: WorkerSafetyConfig,
    starter: ExecutionStarter,
    *,
    cancellation: CancellationSignal | None = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> WorkerJobObservation:
    """Apply P4.3 safety semantics around an injected fake execution handle."""
    envelope = deepcopy(dict(queue_envelope))
    validate_queue_envelope(envelope)
    if not isinstance(config, WorkerSafetyConfig):
        raise TypeError("config must be WorkerSafetyConfig")
    signal = cancellation if cancellation is not None else _NeverCancelled()
    journal = LocalJobJournal(config.state_root)
    job = envelope["job"]

    with journal.try_venue_year_lock(job) as acquired:
        if not acquired:
            return _observation(
                job,
                WorkerJobStatus.REFUSED,
                WorkerJobReason.VENUE_YEAR_BUSY,
                started=False,
                retry_permitted=True,
            )

        state = journal.inspect(job)
        if state is JournalState.COMPLETED:
            return _observation(
                job,
                WorkerJobStatus.SKIPPED,
                WorkerJobReason.DUPLICATE_COMPLETED,
                started=False,
                retry_permitted=False,
            )
        if state is JournalState.ACTIVE or journal.has_active_venue_year_claim(job):
            return _recovery_observation(
                job,
                WorkerJobReason.ACTIVE_CLAIM_EXISTS,
                started=False,
            )
        if not _disk_is_sufficient(
            config.data_root,
            config.disk_policy,
            disk_usage,
        ):
            return _observation(
                job,
                WorkerJobStatus.REFUSED,
                WorkerJobReason.INSUFFICIENT_DISK,
                started=False,
                retry_permitted=True,
            )
        try:
            cancelled = signal.is_cancelled()
        except Exception:
            return _observation(
                job,
                WorkerJobStatus.REFUSED,
                WorkerJobReason.SUPERVISION_FAILED,
                started=False,
                retry_permitted=True,
            )
        if not isinstance(cancelled, bool):
            return _observation(
                job,
                WorkerJobStatus.REFUSED,
                WorkerJobReason.SUPERVISION_FAILED,
                started=False,
                retry_permitted=True,
            )
        if cancelled:
            return _observation(
                job,
                WorkerJobStatus.CANCELLED,
                WorkerJobReason.CANCELLED_BEFORE_START,
                started=False,
                retry_permitted=True,
            )

        journal.create_claim(job)
        try:
            handle = starter.start(deepcopy(job))
            outcome = handle.wait(
                timeout_seconds=float(config.timeout_seconds),
                cancellation=signal,
            )
        except Exception:
            return _recovery_observation(
                job,
                WorkerJobReason.SUPERVISION_FAILED,
                started=True,
            )

        if outcome is not None:
            if not isinstance(outcome, FixtureExecutionOutcome):
                return _recovery_observation(
                    job,
                    WorkerJobReason.SUPERVISION_FAILED,
                    started=True,
                )
            if outcome.status is FixtureOutcomeStatus.SUCCEEDED:
                journal.mark_completed(job)
                return _observation(
                    job,
                    WorkerJobStatus.COMPLETED,
                    outcome.reason_code,
                    started=True,
                    retry_permitted=False,
                )
            journal.clear_claim(job)
            return _observation(
                job,
                WorkerJobStatus.FAILED,
                outcome.reason_code,
                started=True,
                retry_permitted=True,
            )

        try:
            cancelled = signal.is_cancelled()
            if not isinstance(cancelled, bool):
                raise TypeError("cancellation signal returned a non-boolean")
            handle.cancel()
            stopped = handle.wait_stopped(
                timeout_seconds=float(config.cancellation_grace_seconds)
            )
        except Exception:
            return _recovery_observation(
                job,
                WorkerJobReason.SUPERVISION_FAILED,
                started=True,
            )
        if stopped is not True:
            return _recovery_observation(
                job,
                WorkerJobReason.CANCELLATION_UNCONFIRMED,
                started=True,
            )

        journal.clear_claim(job)
        if cancelled:
            return _observation(
                job,
                WorkerJobStatus.CANCELLED,
                WorkerJobReason.CANCELLATION_REQUESTED,
                started=True,
                retry_permitted=True,
            )
        return _observation(
            job,
            WorkerJobStatus.TIMED_OUT,
            WorkerJobReason.RUNTIME_EXCEEDED,
            started=True,
            retry_permitted=True,
        )
