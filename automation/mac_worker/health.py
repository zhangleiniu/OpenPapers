"""Secret-safe local health checks for the optional P4.2 Mac package."""

from __future__ import annotations

import os
import platform
import stat
import sys
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Protocol, Sequence

from automation.job_queue import PREFECT_WORK_POOL_NAME


class HealthCheckName(str, Enum):
    """Closed health-check vocabulary for stable local reports."""

    PYTHON_RUNTIME = "python_runtime"
    OPERATING_SYSTEM = "operating_system"
    REPOSITORY = "repository"
    DATA_ROOT = "data_root"
    PREFECT_PACKAGE = "prefect_package"
    PREFECT_CONFIGURATION = "prefect_configuration"
    CODEX_LOGIN_MARKER = "codex_login_marker"


class HealthCheckStatus(str, Enum):
    """One bounded outcome for a required health check."""

    PASS = "pass"
    FAIL = "fail"


class HealthCheckCode(str, Enum):
    """Secret-free reason codes; arbitrary exception text is never retained."""

    READY = "ready"
    UNSUPPORTED_PYTHON = "unsupported_python"
    UNSUPPORTED_OPERATING_SYSTEM = "unsupported_operating_system"
    INVALID_REPOSITORY = "invalid_repository"
    DATA_ROOT_UNAVAILABLE = "data_root_unavailable"
    PREFECT_PACKAGE_MISSING = "prefect_package_missing"
    PREFECT_VERSION_UNSUPPORTED = "prefect_version_unsupported"
    PREFECT_CONFIGURATION_MISSING = "prefect_configuration_missing"
    PREFECT_PROBE_FAILED = "prefect_probe_failed"
    CODEX_LOGIN_MISSING = "codex_login_missing"
    CODEX_LOGIN_UNSAFE = "codex_login_unsafe"


@dataclass(frozen=True)
class HealthSignal:
    """One required check result without path, credential, or exception text."""

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
class WorkerHealthReport:
    """Deterministic aggregate of all required local worker checks."""

    ready: bool
    checks: tuple[HealthSignal, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class WorkerHealthConfig:
    """Explicit local paths used by health checks; values are never reported."""

    repository_root: Path
    data_root: Path
    codex_auth_path: Path

    def __post_init__(self) -> None:
        for value in (
            self.repository_root,
            self.data_root,
            self.codex_auth_path,
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError("worker health paths must be absolute Path values")


class PrefectConfigurationProbe(Protocol):
    """Injected local-only probe used instead of constructing a live client."""

    def is_configured(self, *, work_pool_name: str) -> bool:
        """Return whether local Prefect settings are present for this worker."""


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


def _python_signal(version: Sequence[int]) -> HealthSignal:
    supported = len(version) >= 2 and tuple(version[:2]) == (3, 12)
    return _signal(
        HealthCheckName.PYTHON_RUNTIME,
        supported,
        HealthCheckCode.UNSUPPORTED_PYTHON,
    )


def _operating_system_signal(name: str) -> HealthSignal:
    return _signal(
        HealthCheckName.OPERATING_SYSTEM,
        name == "Darwin",
        HealthCheckCode.UNSUPPORTED_OPERATING_SYSTEM,
    )


def _repository_signal(root: Path) -> HealthSignal:
    markers = (
        root / "main.py",
        root / "automation" / "job_queue.py",
        root / "automation" / "mac_worker" / "runtime.py",
    )
    valid = root.is_dir() and all(marker.is_file() for marker in markers)
    return _signal(
        HealthCheckName.REPOSITORY,
        valid,
        HealthCheckCode.INVALID_REPOSITORY,
    )


def _data_root_signal(root: Path) -> HealthSignal:
    available = root.is_dir() and os.access(root, os.R_OK | os.W_OK | os.X_OK)
    return _signal(
        HealthCheckName.DATA_ROOT,
        available,
        HealthCheckCode.DATA_ROOT_UNAVAILABLE,
    )


def _prefect_package_signal(version: str | None) -> HealthSignal:
    if version is None:
        return _signal(
            HealthCheckName.PREFECT_PACKAGE,
            False,
            HealthCheckCode.PREFECT_PACKAGE_MISSING,
        )
    try:
        parts = version.split(".")
        major = int(parts[0])
        minor = int(parts[1])
    except (ValueError, AttributeError, IndexError):
        major, minor = -1, -1
    return _signal(
        HealthCheckName.PREFECT_PACKAGE,
        major == 3 and minor >= 7,
        HealthCheckCode.PREFECT_VERSION_UNSUPPORTED,
    )


def _prefect_configuration_signal(
    probe: PrefectConfigurationProbe,
) -> HealthSignal:
    try:
        configured = probe.is_configured(work_pool_name=PREFECT_WORK_POOL_NAME)
    except Exception:
        return _signal(
            HealthCheckName.PREFECT_CONFIGURATION,
            False,
            HealthCheckCode.PREFECT_PROBE_FAILED,
        )
    if not isinstance(configured, bool):
        return _signal(
            HealthCheckName.PREFECT_CONFIGURATION,
            False,
            HealthCheckCode.PREFECT_PROBE_FAILED,
        )
    return _signal(
        HealthCheckName.PREFECT_CONFIGURATION,
        configured,
        HealthCheckCode.PREFECT_CONFIGURATION_MISSING,
    )


def _codex_login_signal(path: Path) -> HealthSignal:
    try:
        path_stat = path.lstat()
    except OSError:
        return _signal(
            HealthCheckName.CODEX_LOGIN_MARKER,
            False,
            HealthCheckCode.CODEX_LOGIN_MISSING,
        )
    safe = (
        stat.S_ISREG(path_stat.st_mode)
        and not path.is_symlink()
        and path_stat.st_uid == os.getuid()
        and bool(path_stat.st_mode & stat.S_IRUSR)
        and path_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    )
    return _signal(
        HealthCheckName.CODEX_LOGIN_MARKER,
        safe,
        HealthCheckCode.CODEX_LOGIN_UNSAFE,
    )


def collect_worker_health(
    config: WorkerHealthConfig,
    prefect_probe: PrefectConfigurationProbe,
    *,
    python_version: Sequence[int] | None = None,
    platform_name: str | None = None,
    prefect_version: str | None = None,
) -> WorkerHealthReport:
    """Collect required local checks without network, subprocess, or secret reads."""
    if not isinstance(config, WorkerHealthConfig):
        raise TypeError("config must be WorkerHealthConfig")
    resolved_python = tuple(python_version or sys.version_info[:3])
    if prefect_version is None:
        try:
            resolved_prefect = metadata.version("prefect")
        except metadata.PackageNotFoundError:
            resolved_prefect = None
    else:
        resolved_prefect = prefect_version
    checks = (
        _python_signal(resolved_python),
        _operating_system_signal(platform_name or platform.system()),
        _repository_signal(config.repository_root),
        _data_root_signal(config.data_root),
        _prefect_package_signal(resolved_prefect),
        _prefect_configuration_signal(prefect_probe),
        _codex_login_signal(config.codex_auth_path),
    )
    return WorkerHealthReport(
        ready=all(check.status is HealthCheckStatus.PASS for check in checks),
        checks=checks,
    )
