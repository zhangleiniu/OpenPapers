"""Credential-free, one-shot host boundary for the P4.L3 local service."""

from __future__ import annotations

import os
import platform
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from automation.local_service.records import (
    BoundedServiceRecords,
    ServiceRecordError,
)


LOCAL_SERVICE_LABEL = "org.openpapers.local-control"
MAX_RUN_RECORDS = 256
MAX_SELECTION_COUNT = 100
_ROLE_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class HealthCheckName(str, Enum):
    OPERATING_SYSTEM = "operating_system"
    PYTHON_EXECUTABLE = "python_executable"
    REPOSITORY = "repository"
    INTERNAL_STATE = "internal_state"
    EXTERNAL_VOLUME = "external_volume"


class HealthCheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class HealthCheckCode(str, Enum):
    READY = "ready"
    UNSUPPORTED_OPERATING_SYSTEM = "unsupported_operating_system"
    PYTHON_EXECUTABLE_UNAVAILABLE = "python_executable_unavailable"
    INVALID_REPOSITORY = "invalid_repository"
    INTERNAL_STATE_UNAVAILABLE = "internal_state_unavailable"
    EXTERNAL_VOLUME_UNAVAILABLE = "external_volume_unavailable"
    EXTERNAL_VOLUME_PROBE_FAILED = "external_volume_probe_failed"


class LocalEffectStatus(str, Enum):
    COMPLETED = "completed"
    NO_DUE_WORK = "no_due_work"


class LocalServiceRunStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class LocalServiceRunCode(str, Enum):
    COMPLETED = "completed"
    NO_DUE_WORK = "no_due_work"
    HEALTH_FAILED = "health_failed"
    RECORDS_UNAVAILABLE = "records_unavailable"
    EFFECT_UNCONFIGURED = "effect_unconfigured"
    EFFECT_FAILED = "effect_failed"
    INVALID_EFFECT_OUTCOME = "invalid_effect_outcome"


@dataclass(frozen=True)
class HealthSignal:
    name: HealthCheckName
    status: HealthCheckStatus
    code: HealthCheckCode

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name.value,
            "status": self.status.value,
            "code": self.code.value,
        }


@dataclass(frozen=True)
class LocalServiceHealthReport:
    ready: bool
    checks: tuple[HealthSignal, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "ready": self.ready,
            "checks": [item.as_dict() for item in self.checks],
        }


@dataclass(frozen=True)
class LocalEffectOutcome:
    status: LocalEffectStatus
    selection_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.status, LocalEffectStatus):
            raise TypeError("effect status must be LocalEffectStatus")
        if (
            isinstance(self.selection_count, bool)
            or not isinstance(self.selection_count, int)
            or not 0 <= self.selection_count <= MAX_SELECTION_COUNT
        ):
            raise ValueError("effect selection count is outside the supported range")
        if self.status is LocalEffectStatus.NO_DUE_WORK and self.selection_count != 0:
            raise ValueError("no-due-work outcome must have zero selections")


@dataclass(frozen=True)
class LocalServiceRunReport:
    status: LocalServiceRunStatus
    code: LocalServiceRunCode
    scheduled_for: datetime
    observed_at: datetime
    selection_count: int
    health_ready: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "code": self.code.value,
            "scheduled_for": _timestamp(self.scheduled_for),
            "observed_at": _timestamp(self.observed_at),
            "selection_count": self.selection_count,
            "health_ready": self.health_ready,
        }


@dataclass(frozen=True)
class LocalServiceConfig:
    repository_root: Path
    python_executable: Path
    internal_root: Path
    external_volume_root: Path
    role_user: str
    schedule_minute: int = 17
    record_limit: int = 128

    def __post_init__(self) -> None:
        paths = (
            self.repository_root,
            self.python_executable,
            self.internal_root,
            self.external_volume_root,
        )
        if any(not isinstance(item, Path) or not item.is_absolute() for item in paths):
            raise ValueError("local service paths must be absolute Path values")
        if any(Path(os.path.normpath(item)) != item for item in paths):
            raise ValueError("local service paths must be normalized")
        internal = self.internal_root
        external = self.external_volume_root
        if (
            internal == external
            or internal.is_relative_to(external)
            or external.is_relative_to(internal)
        ):
            raise ValueError("internal and external roots must be disjoint")
        if not isinstance(self.role_user, str) or not _ROLE_USER.fullmatch(
            self.role_user
        ) or self.role_user == "root":
            raise ValueError("local service role user is invalid")
        if (
            isinstance(self.schedule_minute, bool)
            or not isinstance(self.schedule_minute, int)
            or not 0 <= self.schedule_minute <= 59
        ):
            raise ValueError("local service schedule minute is invalid")
        if (
            isinstance(self.record_limit, bool)
            or not isinstance(self.record_limit, int)
            or not 1 <= self.record_limit <= MAX_RUN_RECORDS
        ):
            raise ValueError("local service record limit is outside the supported range")

    @property
    def control_root(self) -> Path:
        return self.internal_root / "control"

    @property
    def state_path(self) -> Path:
        return self.control_root / "state.sqlite3"

    @property
    def service_root(self) -> Path:
        return self.internal_root / "service"

    @property
    def health_path(self) -> Path:
        return self.service_root / "health.v1.json"

    @property
    def run_records_path(self) -> Path:
        return self.service_root / "runs.v1.json"

    def public_summary(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "label": LOCAL_SERVICE_LABEL,
            "schedule_minute": self.schedule_minute,
            "record_limit": self.record_limit,
        }


class VolumeAvailabilityProbe(Protocol):
    def is_available(self, root: Path) -> bool:
        """Return whether the configured execution volume is mounted and usable."""


class LocalWakeupEffect(Protocol):
    def run(
        self,
        *,
        state_path: Path,
        execution_root: Path,
        scheduled_for: datetime,
        observed_at: datetime,
    ) -> LocalEffectOutcome:
        """Run one bounded wakeup after local host gates pass."""


class LocalMountProbe:
    """Check an execution directory on a non-root mounted filesystem."""

    def is_available(self, root: Path) -> bool:
        if (
            not root.is_dir()
            or root.is_symlink()
            or not os.access(root, os.R_OK | os.W_OK | os.X_OK)
        ):
            return False
        candidate = root
        while candidate != candidate.parent and not os.path.ismount(candidate):
            candidate = candidate.parent
            if candidate.is_symlink():
                return False
        return candidate != Path("/") and os.path.ismount(candidate)


def _utc(value: datetime, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def scheduled_slot(observed_at: datetime, minute: int) -> datetime:
    if (
        isinstance(minute, bool)
        or not isinstance(minute, int)
        or not 0 <= minute <= 59
    ):
        raise ValueError("service schedule minute is invalid")
    observed = _utc(observed_at, field="service clock")
    candidate = observed.replace(minute=minute, second=0, microsecond=0)
    if candidate > observed:
        candidate -= timedelta(hours=1)
    return candidate


def _signal(
    name: HealthCheckName,
    passed: bool,
    failure: HealthCheckCode,
) -> HealthSignal:
    return HealthSignal(
        name=name,
        status=HealthCheckStatus.PASS if passed else HealthCheckStatus.FAIL,
        code=HealthCheckCode.READY if passed else failure,
    )


def _python_signal(path: Path) -> HealthSignal:
    try:
        metadata = path.stat()
        available = (
            stat.S_ISREG(metadata.st_mode)
            and os.access(path, os.X_OK)
        )
    except OSError:
        available = False
    return _signal(
        HealthCheckName.PYTHON_EXECUTABLE,
        available,
        HealthCheckCode.PYTHON_EXECUTABLE_UNAVAILABLE,
    )


def _repository_signal(root: Path) -> HealthSignal:
    markers = (
        root / "main.py",
        root / "automation" / "local_scheduler.py",
        root / "automation" / "local_control_plane.py",
    )
    available = root.is_dir() and not root.is_symlink() and all(
        item.is_file() for item in markers
    )
    return _signal(
        HealthCheckName.REPOSITORY,
        available,
        HealthCheckCode.INVALID_REPOSITORY,
    )


def _private_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
        return (
            stat.S_ISDIR(metadata.st_mode)
            and not path.is_symlink()
            and metadata.st_uid == os.geteuid()
            and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
            and os.access(path, os.R_OK | os.W_OK | os.X_OK)
        )
    except OSError:
        return False


def _internal_signal(root: Path, control_root: Path) -> HealthSignal:
    available = _private_directory(root) and _private_directory(control_root)
    return _signal(
        HealthCheckName.INTERNAL_STATE,
        available,
        HealthCheckCode.INTERNAL_STATE_UNAVAILABLE,
    )


def _external_signal(
    root: Path,
    probe: VolumeAvailabilityProbe,
) -> HealthSignal:
    try:
        available = probe.is_available(root)
    except Exception:
        return _signal(
            HealthCheckName.EXTERNAL_VOLUME,
            False,
            HealthCheckCode.EXTERNAL_VOLUME_PROBE_FAILED,
        )
    if not isinstance(available, bool):
        return _signal(
            HealthCheckName.EXTERNAL_VOLUME,
            False,
            HealthCheckCode.EXTERNAL_VOLUME_PROBE_FAILED,
        )
    return _signal(
        HealthCheckName.EXTERNAL_VOLUME,
        available,
        HealthCheckCode.EXTERNAL_VOLUME_UNAVAILABLE,
    )


def collect_local_service_health(
    config: LocalServiceConfig,
    volume_probe: VolumeAvailabilityProbe,
    *,
    platform_name: str | None = None,
) -> LocalServiceHealthReport:
    if not isinstance(config, LocalServiceConfig):
        raise TypeError("config must be LocalServiceConfig")
    if not callable(getattr(volume_probe, "is_available", None)):
        raise TypeError("volume probe must provide is_available()")
    checks = (
        _signal(
            HealthCheckName.OPERATING_SYSTEM,
            (platform_name or platform.system()) == "Darwin",
            HealthCheckCode.UNSUPPORTED_OPERATING_SYSTEM,
        ),
        _python_signal(config.python_executable),
        _repository_signal(config.repository_root),
        _internal_signal(config.internal_root, config.control_root),
        _external_signal(config.external_volume_root, volume_probe),
    )
    return LocalServiceHealthReport(
        ready=all(item.status is HealthCheckStatus.PASS for item in checks),
        checks=checks,
    )


def _report(
    status: LocalServiceRunStatus,
    code: LocalServiceRunCode,
    *,
    scheduled_for: datetime,
    observed_at: datetime,
    selection_count: int = 0,
    health_ready: bool,
) -> LocalServiceRunReport:
    return LocalServiceRunReport(
        status=status,
        code=code,
        scheduled_for=scheduled_for,
        observed_at=observed_at,
        selection_count=selection_count,
        health_ready=health_ready,
    )


def run_local_service_once(
    config: LocalServiceConfig,
    *,
    effect: LocalWakeupEffect | None,
    volume_probe: VolumeAvailabilityProbe,
    clock: Callable[[], datetime] | None = None,
    platform_name: str | None = None,
) -> LocalServiceRunReport:
    """Run one bounded host check and injected wakeup, then exit."""
    resolved_clock = clock or (lambda: datetime.now(timezone.utc))
    observed_at = _utc(resolved_clock(), field="service clock")
    scheduled_for = scheduled_slot(observed_at, config.schedule_minute)
    health = collect_local_service_health(
        config, volume_probe, platform_name=platform_name
    )
    records = BoundedServiceRecords(
        service_root=config.service_root,
        health_path=config.health_path,
        run_records_path=config.run_records_path,
        record_limit=config.record_limit,
    )
    try:
        records.prepare(health.as_dict())
    except ServiceRecordError:
        return _report(
            LocalServiceRunStatus.BLOCKED,
            LocalServiceRunCode.RECORDS_UNAVAILABLE,
            scheduled_for=scheduled_for,
            observed_at=observed_at,
            health_ready=health.ready,
        )
    if not health.ready:
        result = _report(
            LocalServiceRunStatus.BLOCKED,
            LocalServiceRunCode.HEALTH_FAILED,
            scheduled_for=scheduled_for,
            observed_at=observed_at,
            health_ready=False,
        )
    elif effect is None:
        result = _report(
            LocalServiceRunStatus.BLOCKED,
            LocalServiceRunCode.EFFECT_UNCONFIGURED,
            scheduled_for=scheduled_for,
            observed_at=observed_at,
            health_ready=True,
        )
    elif not callable(getattr(effect, "run", None)):
        result = _report(
            LocalServiceRunStatus.BLOCKED,
            LocalServiceRunCode.INVALID_EFFECT_OUTCOME,
            scheduled_for=scheduled_for,
            observed_at=observed_at,
            health_ready=True,
        )
    else:
        try:
            outcome = effect.run(
                state_path=config.state_path,
                execution_root=config.external_volume_root,
                scheduled_for=scheduled_for,
                observed_at=observed_at,
            )
        except Exception:
            result = _report(
                LocalServiceRunStatus.FAILED,
                LocalServiceRunCode.EFFECT_FAILED,
                scheduled_for=scheduled_for,
                observed_at=observed_at,
                health_ready=True,
            )
        else:
            if not isinstance(outcome, LocalEffectOutcome):
                result = _report(
                    LocalServiceRunStatus.FAILED,
                    LocalServiceRunCode.INVALID_EFFECT_OUTCOME,
                    scheduled_for=scheduled_for,
                    observed_at=observed_at,
                    health_ready=True,
                )
            else:
                result = _report(
                    LocalServiceRunStatus.COMPLETED,
                    LocalServiceRunCode.COMPLETED
                    if outcome.status is LocalEffectStatus.COMPLETED
                    else LocalServiceRunCode.NO_DUE_WORK,
                    scheduled_for=scheduled_for,
                    observed_at=observed_at,
                    selection_count=outcome.selection_count,
                    health_ready=True,
                )
    try:
        records.append(result.as_dict())
    except ServiceRecordError:
        return _report(
            LocalServiceRunStatus.BLOCKED,
            LocalServiceRunCode.RECORDS_UNAVAILABLE,
            scheduled_for=scheduled_for,
            observed_at=observed_at,
            health_ready=health.ready,
        )
    return result
