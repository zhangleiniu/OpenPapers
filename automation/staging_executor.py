"""P5.2 isolated staging and supervised existing-scraper execution.

The public coordinator is deliberately not connected to the local scheduler,
LaunchDaemon, P4.3 fixture supervisor, validation, results, or promotion. Tests
inject a fake launcher; the concrete subprocess adapter remains dormant until a
later package explicitly wires and authorizes execution.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from automation.command_registry import (
    DataRootPolicy,
    RepositoryEntryPoint,
    resolve_approved_command,
)
from automation.domain import assert_secret_free
from automation.job_queue import JobType, validate_job_identity


_CHECKPOINT_SCHEMA_VERSION = 1
_CHECKPOINT_NAME = "checkpoint.v1.json"
_CHECKPOINT_KEYS = frozenset(
    {
        "schema_version",
        "job_id",
        "job_fingerprint",
        "job_type",
        "venue_id",
        "year",
        "attempt",
        "status",
        "reason_code",
        "updated_at",
    }
)
_MAX_RUNTIME_SECONDS = 24 * 60 * 60
_MAX_CANCELLATION_GRACE_SECONDS = 15 * 60
_MAX_ATTEMPTS = 1_000_000


class StagingExecutorError(ValueError):
    """Raised when P5.2 configuration, staging, or execution fails closed."""


class StagingCheckpointError(StagingExecutorError):
    """Raised when retained staging state is unsafe, corrupt, or conflicting."""


class StagingCheckpointStatus(str, Enum):
    """Closed process-checkpoint states; none is a job result or validation."""

    PREPARED = "prepared"
    RUNNING = "running"
    PROCESS_SUCCEEDED = "process_succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    AMBIGUOUS = "ambiguous"


class StagingReason(str, Enum):
    """Secret-free reasons retained in checkpoints and observations."""

    READY = "ready"
    PROCESS_STARTED = "process_started"
    EXIT_ZERO = "exit_zero"
    EXIT_NONZERO = "exit_nonzero"
    CANCELLED_BEFORE_START = "cancelled_before_start"
    CANCELLATION_CONFIRMED = "cancellation_confirmed"
    TIMEOUT_CONFIRMED = "timeout_confirmed"
    START_UNCERTAIN = "start_uncertain"
    SUPERVISION_FAILED = "supervision_failed"
    STOP_UNCONFIRMED = "stop_unconfirmed"
    DUPLICATE_PROCESS_SUCCESS = "duplicate_process_success"
    ACTIVE_OR_AMBIGUOUS = "active_or_ambiguous"


class StagingExecutionStatus(str, Enum):
    """Bounded execution observation vocabulary, separate from P4.4 results."""

    PROCESS_SUCCEEDED = "process_succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class StagingExecutorConfig:
    """Explicit trusted runtime and disjoint staging/canonical roots."""

    repository_root: Path
    python_executable: Path
    staging_root: Path
    canonical_data_root: Path
    timeout_seconds: float = 60 * 60
    cancellation_grace_seconds: float = 30

    def __post_init__(self) -> None:
        paths = (
            self.repository_root,
            self.python_executable,
            self.staging_root,
            self.canonical_data_root,
        )
        if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
            raise ValueError("staging executor paths must be absolute Path values")
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
class StagingCheckpoint:
    """Strict current state for one immutable staged scrape job."""

    job_id: str
    job_fingerprint: str
    job_type: str
    venue_id: str
    year: int
    attempt: int
    status: StagingCheckpointStatus
    reason_code: StagingReason
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "job_id": self.job_id,
            "job_fingerprint": self.job_fingerprint,
            "job_type": self.job_type,
            "venue_id": self.venue_id,
            "year": self.year,
            "attempt": self.attempt,
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class StagedProcessRequest:
    """Fixed no-shell process binding produced only by reviewed executor code."""

    job_id: str
    argv: tuple[str, ...]
    cwd: Path
    environment_items: tuple[tuple[str, str], ...]
    data_root: Path
    log_path: Path

    def environment(self) -> dict[str, str]:
        """Return a defensive exact child environment."""
        return dict(self.environment_items)


@dataclass(frozen=True)
class StagingExecutionObservation:
    """Bounded process outcome with no artifact, validation, or result claim."""

    status: StagingExecutionStatus
    reason_code: StagingReason
    job_id: str
    job_type: str
    venue_id: str
    year: int
    attempt: int
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
            "attempt": self.attempt,
            "started": self.started,
            "retry_permitted": self.retry_permitted,
        }


class CancellationSignal(Protocol):
    """Injected cancellation state observed during process supervision."""

    def is_cancelled(self) -> bool:
        """Return whether the execution should stop."""


class ProcessHandle(Protocol):
    """Minimal handle required by the P5.2 supervisor."""

    def wait(
        self,
        *,
        timeout_seconds: float,
        cancellation: CancellationSignal,
    ) -> int | None:
        """Return an exit code, or ``None`` on timeout/cancellation."""

    def terminate(self) -> None:
        """Request termination without assuming the child stopped."""

    def wait_stopped(self, *, timeout_seconds: float) -> bool:
        """Return true only after the complete process group is confirmed stopped."""


class ProcessLauncher(Protocol):
    """Injected start boundary; the immutable job cannot supply a launcher."""

    def start(self, request: StagedProcessRequest) -> ProcessHandle:
        """Start one already prepared fixed process request."""


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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StagingExecutorError("checkpoint clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise StagingCheckpointError("checkpoint timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise StagingCheckpointError("checkpoint timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise StagingCheckpointError("checkpoint timestamp is invalid")
    return parsed.astimezone(timezone.utc)


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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _path_is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _normalized(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        raise StagingExecutorError("configured path could not be normalized") from None


def _validate_disjoint_configuration(config: StagingExecutorConfig) -> None:
    repository = _normalized(config.repository_root)
    executable = _normalized(config.python_executable)
    staging = _normalized(config.staging_root)
    canonical = _normalized(config.canonical_data_root)
    if (
        repository != config.repository_root
        or executable != config.python_executable
        or staging != config.staging_root
        or canonical != config.canonical_data_root
    ):
        raise StagingExecutorError("staging executor paths must be normalized")
    for left, right in (
        (staging, canonical),
        (canonical, staging),
        (staging, repository),
        (repository, staging),
    ):
        if _path_is_within(left, right):
            raise StagingExecutorError("staging, canonical, and repository roots overlap")


def _safe_metadata(path: Path, *, directory: bool, executable: bool = False) -> None:
    try:
        path_stat = path.lstat()
    except OSError:
        raise StagingExecutorError("trusted runtime path is unavailable") from None
    expected_type = stat.S_ISDIR(path_stat.st_mode) if directory else stat.S_ISREG(
        path_stat.st_mode
    )
    safe = (
        expected_type
        and not path.is_symlink()
        and path_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH) == 0
        and (not executable or path_stat.st_mode & stat.S_IXUSR != 0)
    )
    if not safe:
        raise StagingExecutorError("trusted runtime path metadata is unsafe")


def _ensure_private_directory(path: Path, *, create: bool = True) -> None:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path_stat = path.lstat()
    except OSError:
        raise StagingExecutorError("private staging directory is unavailable") from None
    safe = (
        stat.S_ISDIR(path_stat.st_mode)
        and not path.is_symlink()
        and path_stat.st_uid == os.getuid()
        and path_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    )
    if not safe:
        raise StagingExecutorError("private staging directory metadata is unsafe")


def _checkpoint_for(
    job: Mapping[str, Any],
    *,
    attempt: int,
    status: StagingCheckpointStatus,
    reason: StagingReason,
    now: datetime,
) -> StagingCheckpoint:
    if not 0 <= attempt <= _MAX_ATTEMPTS:
        raise StagingCheckpointError("checkpoint attempt is out of bounds")
    return StagingCheckpoint(
        job_id=job["job_id"],
        job_fingerprint=job["job_fingerprint"],
        job_type=job["job_type"],
        venue_id=job["venue_id"],
        year=job["year"],
        attempt=attempt,
        status=status,
        reason_code=reason,
        updated_at=_format_time(now),
    )


def _validate_checkpoint_payload(
    payload: Any,
    job: Mapping[str, Any],
) -> StagingCheckpoint:
    valid_shape = (
        isinstance(payload, dict)
        and frozenset(payload) == _CHECKPOINT_KEYS
        and payload.get("schema_version") == _CHECKPOINT_SCHEMA_VERSION
        and not isinstance(payload.get("schema_version"), bool)
        and payload.get("job_id") == job["job_id"]
        and payload.get("job_fingerprint") == job["job_fingerprint"]
        and payload.get("job_type") == job["job_type"]
        and payload.get("venue_id") == job["venue_id"]
        and payload.get("year") == job["year"]
        and isinstance(payload.get("attempt"), int)
        and not isinstance(payload["attempt"], bool)
        and 0 <= payload["attempt"] <= _MAX_ATTEMPTS
        and isinstance(payload.get("status"), str)
        and isinstance(payload.get("reason_code"), str)
    )
    if not valid_shape:
        raise StagingCheckpointError("checkpoint conflicts with the immutable job")
    try:
        status = StagingCheckpointStatus(payload["status"])
        reason = StagingReason(payload["reason_code"])
    except ValueError:
        raise StagingCheckpointError("checkpoint status or reason is invalid") from None
    allowed_reasons = {
        StagingCheckpointStatus.PREPARED: {
            StagingReason.READY,
        },
        StagingCheckpointStatus.RUNNING: {
            StagingReason.PROCESS_STARTED,
        },
        StagingCheckpointStatus.PROCESS_SUCCEEDED: {
            StagingReason.EXIT_ZERO,
        },
        StagingCheckpointStatus.FAILED: {
            StagingReason.EXIT_NONZERO,
        },
        StagingCheckpointStatus.TIMED_OUT: {
            StagingReason.TIMEOUT_CONFIRMED,
        },
        StagingCheckpointStatus.CANCELLED: {
            StagingReason.CANCELLED_BEFORE_START,
            StagingReason.CANCELLATION_CONFIRMED,
        },
        StagingCheckpointStatus.AMBIGUOUS: {
            StagingReason.START_UNCERTAIN,
            StagingReason.SUPERVISION_FAILED,
            StagingReason.STOP_UNCONFIRMED,
        },
    }
    if reason not in allowed_reasons[status]:
        raise StagingCheckpointError("checkpoint status and reason conflict")
    if status is StagingCheckpointStatus.PREPARED and payload["attempt"] != 0:
        raise StagingCheckpointError("prepared checkpoint must precede all attempts")
    if (
        status is not StagingCheckpointStatus.PREPARED
        and reason is not StagingReason.CANCELLED_BEFORE_START
        and payload["attempt"] < 1
    ):
        raise StagingCheckpointError("started checkpoint requires an attempt")
    updated_at = payload.get("updated_at")
    _parse_time(updated_at)
    assert_secret_free(payload)
    return StagingCheckpoint(
        job_id=payload["job_id"],
        job_fingerprint=payload["job_fingerprint"],
        job_type=payload["job_type"],
        venue_id=payload["venue_id"],
        year=payload["year"],
        attempt=payload["attempt"],
        status=status,
        reason_code=reason,
        updated_at=updated_at,
    )


class StagingCheckpointStore:
    """Atomic strict checkpoint storage inside one immutable job root."""

    def __init__(self, job_root: Path, job: Mapping[str, Any]) -> None:
        if not isinstance(job_root, Path) or not job_root.is_absolute():
            raise ValueError("job root must be an absolute Path")
        validate_job_identity(job)
        self.job_root = job_root
        self.job = dict(job)
        self.path = job_root / _CHECKPOINT_NAME

    def read(self) -> StagingCheckpoint:
        try:
            path_stat = self.path.lstat()
            safe = (
                stat.S_ISREG(path_stat.st_mode)
                and not self.path.is_symlink()
                and path_stat.st_uid == os.getuid()
                and path_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
            )
            if not safe:
                raise StagingCheckpointError("checkpoint metadata is unsafe")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except StagingCheckpointError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise StagingCheckpointError("checkpoint is missing or unreadable") from None
        return _validate_checkpoint_payload(payload, self.job)

    def write(
        self,
        checkpoint: StagingCheckpoint,
        *,
        previous: StagingCheckpoint | None,
    ) -> None:
        validated = _validate_checkpoint_payload(checkpoint.as_dict(), self.job)
        if previous is not None:
            retained = self.read()
            if retained != previous:
                raise StagingCheckpointError("checkpoint changed before update")
            if validated.attempt < retained.attempt:
                raise StagingCheckpointError("checkpoint attempt cannot regress")
            if _parse_time(validated.updated_at) < _parse_time(retained.updated_at):
                raise StagingCheckpointError("checkpoint time cannot regress")
            retryable = {
                StagingCheckpointStatus.PREPARED,
                StagingCheckpointStatus.FAILED,
                StagingCheckpointStatus.TIMED_OUT,
                StagingCheckpointStatus.CANCELLED,
            }
            valid_transition = (
                retained.status in retryable
                and validated.status is StagingCheckpointStatus.RUNNING
                and validated.attempt == retained.attempt + 1
            ) or (
                retained.status in retryable
                and validated.status is StagingCheckpointStatus.CANCELLED
                and validated.reason_code is StagingReason.CANCELLED_BEFORE_START
                and validated.attempt == retained.attempt
            ) or (
                retained.status is StagingCheckpointStatus.RUNNING
                and validated.status
                in {
                    StagingCheckpointStatus.PROCESS_SUCCEEDED,
                    StagingCheckpointStatus.FAILED,
                    StagingCheckpointStatus.TIMED_OUT,
                    StagingCheckpointStatus.CANCELLED,
                    StagingCheckpointStatus.AMBIGUOUS,
                }
                and validated.attempt == retained.attempt
            )
            if not valid_transition:
                raise StagingCheckpointError("checkpoint transition is not allowed")
        elif self.path.exists() or self.path.is_symlink():
            raise StagingCheckpointError("checkpoint already exists")
        elif (
            validated.status is not StagingCheckpointStatus.PREPARED
            or validated.attempt != 0
        ):
            raise StagingCheckpointError("initial checkpoint must be prepared")

        temporary = self.job_root / f".{_CHECKPOINT_NAME}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as file_obj:
                file_obj.write(_canonical_bytes(validated.as_dict()))
                file_obj.flush()
                os.fsync(file_obj.fileno())
            if previous is not None and self.read() != previous:
                raise StagingCheckpointError("checkpoint changed during update")
            os.replace(temporary, self.path)
            _fsync_directory(self.job_root)
        except FileExistsError:
            raise StagingCheckpointError("checkpoint temporary file already exists") from None
        except OSError:
            raise StagingCheckpointError("checkpoint could not be retained") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if self.read() != validated:
            raise StagingCheckpointError("checkpoint update did not persist")


@dataclass(frozen=True)
class _PreparedStaging:
    request: StagedProcessRequest
    store: StagingCheckpointStore
    checkpoint: StagingCheckpoint


def _prepare_staging(
    job: Mapping[str, Any],
    config: StagingExecutorConfig,
    *,
    now: datetime,
) -> _PreparedStaging:
    validate_job_identity(job)
    if JobType(job["job_type"]) is not JobType.SCRAPE_EXISTING:
        raise StagingExecutorError("P5.2 accepts only existing-scraper jobs")
    spec = resolve_approved_command(job)
    if (
        spec.job_type is not JobType.SCRAPE_EXISTING
        or spec.entry_point is not RepositoryEntryPoint.SCRAPER
        or spec.data_root_policy is not DataRootPolicy.ISOLATED_STAGING_REQUIRED
    ):
        raise StagingExecutorError("approved command is outside the P5.2 boundary")
    if not isinstance(config, StagingExecutorConfig):
        raise TypeError("config must be StagingExecutorConfig")
    _validate_disjoint_configuration(config)
    _safe_metadata(config.repository_root, directory=True)
    _safe_metadata(config.python_executable, directory=False, executable=True)
    entry_point = config.repository_root / spec.entry_point.value
    _safe_metadata(entry_point, directory=False)
    _ensure_private_directory(config.staging_root)

    job_root = config.staging_root / job["job_fingerprint"]
    data_root = job_root / "data"
    logs_root = job_root / "logs"
    new_root = not (job_root.exists() or job_root.is_symlink())
    if new_root:
        try:
            job_root.mkdir(mode=0o700)
        except OSError:
            raise StagingExecutorError("job staging root could not be created") from None
        _ensure_private_directory(data_root)
        _ensure_private_directory(logs_root)
    else:
        _ensure_private_directory(job_root, create=False)
        _ensure_private_directory(data_root, create=False)
        _ensure_private_directory(logs_root, create=False)
        try:
            root_names = {item.name for item in job_root.iterdir()}
        except OSError:
            raise StagingExecutorError("job staging root could not be inspected") from None
        if not root_names <= {"data", "logs", _CHECKPOINT_NAME}:
            raise StagingCheckpointError("job staging root contains unknown entries")

    store = StagingCheckpointStore(job_root, job)
    if new_root:
        checkpoint = _checkpoint_for(
            job,
            attempt=0,
            status=StagingCheckpointStatus.PREPARED,
            reason=StagingReason.READY,
            now=now,
        )
        store.write(checkpoint, previous=None)
    else:
        checkpoint = store.read()

    log_path = logs_root / "scraper.log"
    environment_items = tuple(
        sorted(
            {
                "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONUNBUFFERED": "1",
                "SCRAPER_DATA_ROOT": str(data_root),
                "SCRAPER_LOG_FILE": str(log_path),
            }.items()
        )
    )
    request = StagedProcessRequest(
        job_id=job["job_id"],
        argv=(
            str(config.python_executable),
            str(entry_point),
            *spec.arguments,
        ),
        cwd=config.repository_root,
        environment_items=environment_items,
        data_root=data_root,
        log_path=log_path,
    )
    return _PreparedStaging(request=request, store=store, checkpoint=checkpoint)


def _observation(
    job: Mapping[str, Any],
    checkpoint: StagingCheckpoint,
    status: StagingExecutionStatus,
    reason: StagingReason,
    *,
    started: bool,
    retry_permitted: bool,
) -> StagingExecutionObservation:
    return StagingExecutionObservation(
        status=status,
        reason_code=reason,
        job_id=job["job_id"],
        job_type=job["job_type"],
        venue_id=job["venue_id"],
        year=job["year"],
        attempt=checkpoint.attempt,
        started=started,
        retry_permitted=retry_permitted,
    )


def _transition(
    prepared: _PreparedStaging,
    job: Mapping[str, Any],
    *,
    attempt: int,
    status: StagingCheckpointStatus,
    reason: StagingReason,
    clock: Callable[[], datetime],
) -> StagingCheckpoint:
    checkpoint = _checkpoint_for(
        job,
        attempt=attempt,
        status=status,
        reason=reason,
        now=clock(),
    )
    prepared.store.write(checkpoint, previous=prepared.checkpoint)
    return checkpoint


def run_staged_scrape(
    job: Mapping[str, Any],
    config: StagingExecutorConfig,
    launcher: ProcessLauncher,
    *,
    cancellation: CancellationSignal | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> StagingExecutionObservation:
    """Run one approved scrape with isolated roots and durable resume state."""
    if not isinstance(job, Mapping):
        raise TypeError("job must be a mapping")
    if not callable(clock):
        raise TypeError("clock must be callable")
    if not callable(getattr(launcher, "start", None)):
        raise TypeError("launcher must provide start")
    if cancellation is not None and not callable(
        getattr(cancellation, "is_cancelled", None)
    ):
        raise TypeError("cancellation must provide is_cancelled")
    now = clock()
    _format_time(now)
    prepared = _prepare_staging(job, config, now=now)
    checkpoint = prepared.checkpoint
    signal_state = cancellation if cancellation is not None else _NeverCancelled()

    if checkpoint.status is StagingCheckpointStatus.PROCESS_SUCCEEDED:
        return _observation(
            job,
            checkpoint,
            StagingExecutionStatus.SKIPPED,
            StagingReason.DUPLICATE_PROCESS_SUCCESS,
            started=False,
            retry_permitted=False,
        )
    if checkpoint.status in {
        StagingCheckpointStatus.RUNNING,
        StagingCheckpointStatus.AMBIGUOUS,
    }:
        return _observation(
            job,
            checkpoint,
            StagingExecutionStatus.RECOVERY_REQUIRED,
            StagingReason.ACTIVE_OR_AMBIGUOUS,
            started=False,
            retry_permitted=False,
        )
    try:
        cancelled = signal_state.is_cancelled()
    except Exception:
        raise StagingExecutorError("cancellation check failed before start") from None
    if not isinstance(cancelled, bool):
        raise StagingExecutorError("cancellation signal returned a non-boolean")
    if cancelled:
        cancelled_checkpoint = _transition(
            prepared,
            job,
            attempt=checkpoint.attempt,
            status=StagingCheckpointStatus.CANCELLED,
            reason=StagingReason.CANCELLED_BEFORE_START,
            clock=clock,
        )
        return _observation(
            job,
            cancelled_checkpoint,
            StagingExecutionStatus.CANCELLED,
            StagingReason.CANCELLED_BEFORE_START,
            started=False,
            retry_permitted=True,
        )

    attempt = checkpoint.attempt + 1
    if attempt > _MAX_ATTEMPTS:
        raise StagingExecutorError("staging attempt limit is exhausted")
    running = _transition(
        prepared,
        job,
        attempt=attempt,
        status=StagingCheckpointStatus.RUNNING,
        reason=StagingReason.PROCESS_STARTED,
        clock=clock,
    )
    prepared = _PreparedStaging(prepared.request, prepared.store, running)
    try:
        handle = launcher.start(prepared.request)
    except Exception:
        ambiguous = _transition(
            prepared,
            job,
            attempt=attempt,
            status=StagingCheckpointStatus.AMBIGUOUS,
            reason=StagingReason.START_UNCERTAIN,
            clock=clock,
        )
        return _observation(
            job,
            ambiguous,
            StagingExecutionStatus.RECOVERY_REQUIRED,
            StagingReason.START_UNCERTAIN,
            started=True,
            retry_permitted=False,
        )

    try:
        exit_code = handle.wait(
            timeout_seconds=float(config.timeout_seconds),
            cancellation=signal_state,
        )
    except Exception:
        ambiguous = _transition(
            prepared,
            job,
            attempt=attempt,
            status=StagingCheckpointStatus.AMBIGUOUS,
            reason=StagingReason.SUPERVISION_FAILED,
            clock=clock,
        )
        return _observation(
            job,
            ambiguous,
            StagingExecutionStatus.RECOVERY_REQUIRED,
            StagingReason.SUPERVISION_FAILED,
            started=True,
            retry_permitted=False,
        )

    if exit_code is not None:
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            ambiguous = _transition(
                prepared,
                job,
                attempt=attempt,
                status=StagingCheckpointStatus.AMBIGUOUS,
                reason=StagingReason.SUPERVISION_FAILED,
                clock=clock,
            )
            return _observation(
                job,
                ambiguous,
                StagingExecutionStatus.RECOVERY_REQUIRED,
                StagingReason.SUPERVISION_FAILED,
                started=True,
                retry_permitted=False,
            )
        succeeded = exit_code == 0
        final = _transition(
            prepared,
            job,
            attempt=attempt,
            status=(
                StagingCheckpointStatus.PROCESS_SUCCEEDED
                if succeeded
                else StagingCheckpointStatus.FAILED
            ),
            reason=StagingReason.EXIT_ZERO if succeeded else StagingReason.EXIT_NONZERO,
            clock=clock,
        )
        return _observation(
            job,
            final,
            (
                StagingExecutionStatus.PROCESS_SUCCEEDED
                if succeeded
                else StagingExecutionStatus.FAILED
            ),
            final.reason_code,
            started=True,
            retry_permitted=not succeeded,
        )

    try:
        cancelled = signal_state.is_cancelled()
        if not isinstance(cancelled, bool):
            raise TypeError("cancellation signal returned a non-boolean")
        handle.terminate()
        stopped = handle.wait_stopped(
            timeout_seconds=float(config.cancellation_grace_seconds)
        )
    except Exception:
        stopped = False
    if stopped is not True:
        ambiguous = _transition(
            prepared,
            job,
            attempt=attempt,
            status=StagingCheckpointStatus.AMBIGUOUS,
            reason=StagingReason.STOP_UNCONFIRMED,
            clock=clock,
        )
        return _observation(
            job,
            ambiguous,
            StagingExecutionStatus.RECOVERY_REQUIRED,
            StagingReason.STOP_UNCONFIRMED,
            started=True,
            retry_permitted=False,
        )
    final_status = (
        StagingCheckpointStatus.CANCELLED
        if cancelled
        else StagingCheckpointStatus.TIMED_OUT
    )
    reason = (
        StagingReason.CANCELLATION_CONFIRMED
        if cancelled
        else StagingReason.TIMEOUT_CONFIRMED
    )
    final = _transition(
        prepared,
        job,
        attempt=attempt,
        status=final_status,
        reason=reason,
        clock=clock,
    )
    return _observation(
        job,
        final,
        (
            StagingExecutionStatus.CANCELLED
            if cancelled
            else StagingExecutionStatus.TIMED_OUT
        ),
        reason,
        started=True,
        retry_permitted=True,
    )


class _SubprocessHandle:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    def wait(
        self,
        *,
        timeout_seconds: float,
        cancellation: CancellationSignal,
    ) -> int | None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            exit_code = self._process.poll()
            if exit_code is not None:
                return exit_code
            cancelled = cancellation.is_cancelled()
            if not isinstance(cancelled, bool):
                raise TypeError("cancellation signal returned a non-boolean")
            if cancelled or time.monotonic() >= deadline:
                return None
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def terminate(self) -> None:
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def wait_stopped(self, *, timeout_seconds: float) -> bool:
        first_wait = timeout_seconds / 2
        try:
            self._process.wait(timeout=first_wait)
            return True
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self._process.wait(timeout=timeout_seconds - first_wait)
            return True
        except subprocess.TimeoutExpired:
            return False


class SubprocessLauncher:
    """Dormant standard-library adapter for one fixed no-shell request."""

    def start(self, request: StagedProcessRequest) -> ProcessHandle:
        if not isinstance(request, StagedProcessRequest):
            raise TypeError("request must be StagedProcessRequest")
        log_parent = request.log_path.parent
        _ensure_private_directory(log_parent, create=False)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(request.log_path, flags, 0o600)
            descriptor_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or descriptor_stat.st_uid != os.getuid()
                or descriptor_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            ):
                os.close(descriptor)
                raise StagingExecutorError("process log metadata is unsafe")
            with os.fdopen(descriptor, "ab", buffering=0) as output:
                process = subprocess.Popen(
                    request.argv,
                    cwd=request.cwd,
                    env=request.environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    start_new_session=True,
                    close_fds=True,
                )
        except StagingExecutorError:
            raise
        except OSError:
            raise StagingExecutorError("approved process could not be started") from None
        return _SubprocessHandle(process)
