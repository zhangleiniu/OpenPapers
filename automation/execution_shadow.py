"""P5.S manual existing-scraper shadow primitives.

This module adds a Mac-only, explicitly invoked boundary around P5.4.  It
provides a canonical-denying sandbox launcher and a private create-only local
result store.  It is not imported by the installed service and has no
promotion, scheduler, cloud-client, or canonical-write capability.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from automation.execution_pipeline import P5ExecutionConfig
from automation.job_queue import JobType, build_job
from automation.job_results import (
    ImmutableObjectConflictError,
    PublishedResultBundle,
    manifest_object_name,
    result_object_name,
    validate_result_bundle,
)
from automation.mac_worker.safety import DiskSpacePolicy, WorkerSafetyConfig
from automation.staging_executor import (
    CancellationSignal,
    ProcessHandle,
    StagedProcessRequest,
    StagingExecutorConfig,
)
from automation.staging_validation import StagingValidationConfig


_MARKER_NAME = ".p5s-existing-scraper-shadow.v1.json"
_MARKER_VERSION = 1
_SANDBOX_PROFILE = "sandbox/write-isolated.v2.sb"
_MAX_OBJECT_BYTES = 8 * 1024 * 1024


class ExecutionShadowError(ValueError):
    """Raised when a P5.S host, root, or retained artifact fails closed."""


@dataclass(frozen=True)
class ExecutionShadowConfig:
    """Explicit paths and bounds for one private P5.S shadow root."""

    repository_root: Path
    python_executable: Path
    canonical_data_root: Path
    shadow_root: Path
    timeout_seconds: float
    cancellation_grace_seconds: float = 30.0
    minimum_free_bytes: int = 10 * 1024 * 1024 * 1024
    minimum_free_fraction: float = 0.10
    minimum_pdf_size: int = 1024
    worker_id: str = "worker:mac-mini:p5s-shadow"

    def __post_init__(self) -> None:
        for value in (
            self.repository_root,
            self.python_executable,
            self.canonical_data_root,
            self.shadow_root,
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError("P5.S paths must be absolute Path values")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or float(self.timeout_seconds) <= 0
        ):
            raise ValueError("P5.S timeout must be positive")


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExecutionShadowError("P5.S artifact is not canonical JSON") from exc
    return encoded + b"\n"


def _normalized(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        raise ExecutionShadowError("P5.S path cannot be normalized") from None


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _private_directory(path: Path, *, create: bool = True) -> None:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError:
        raise ExecutionShadowError("P5.S private directory is unavailable") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise ExecutionShadowError("P5.S private directory metadata is unsafe")


def _safe_runtime_path(path: Path, *, directory: bool, executable: bool = False) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise ExecutionShadowError("P5.S trusted runtime path is unavailable") from None
    right_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(
        metadata.st_mode
    )
    if (
        not right_type
        or path.is_symlink()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (executable and not metadata.st_mode & stat.S_IXUSR)
    ):
        raise ExecutionShadowError("P5.S trusted runtime path metadata is unsafe")


def _existing_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise ExecutionShadowError("P5.S guarded directory is unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise ExecutionShadowError("P5.S guarded directory metadata is unsafe")


def _retain_create_once(path: Path, content: bytes) -> None:
    if len(content) > _MAX_OBJECT_BYTES:
        raise ExecutionShadowError("P5.S immutable object exceeds its bound")
    _private_directory(path.parent)
    if path.exists() or path.is_symlink():
        try:
            metadata = path.lstat()
            existing = path.read_bytes()
        except OSError:
            raise ExecutionShadowError("P5.S immutable object is unreadable") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or existing != content
        ):
            raise ImmutableObjectConflictError(
                "P5.S immutable object already has different or unsafe content"
            )
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError:
        if not path.exists():
            raise ExecutionShadowError("P5.S immutable create raced") from None
        _retain_create_once(path, content)
    except OSError:
        raise ExecutionShadowError("P5.S immutable object could not be retained") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def prepare_shadow_root(
    config: ExecutionShadowConfig,
    *,
    venue_id: str,
    year: int,
    expected_count: int,
) -> None:
    """Validate and exactly mark a private repository-external shadow root."""
    roots = {
        "repository": _normalized(config.repository_root),
        "python": _normalized(config.python_executable),
        "canonical": _normalized(config.canonical_data_root),
        "shadow": _normalized(config.shadow_root),
    }
    if roots["repository"] != config.repository_root:
        raise ExecutionShadowError("P5.S repository root must be normalized")
    if roots["python"] != config.python_executable:
        raise ExecutionShadowError("P5.S interpreter path must be normalized")
    if roots["canonical"] != config.canonical_data_root:
        raise ExecutionShadowError("P5.S canonical root must be normalized")
    if roots["shadow"] != config.shadow_root:
        raise ExecutionShadowError("P5.S shadow root must be normalized")
    for guarded in (roots["repository"], roots["canonical"]):
        if _within(roots["shadow"], guarded) or _within(guarded, roots["shadow"]):
            raise ExecutionShadowError("P5.S shadow overlaps a guarded root")
    _safe_runtime_path(config.repository_root, directory=True)
    _safe_runtime_path(config.repository_root / "main.py", directory=False)
    _safe_runtime_path(config.python_executable, directory=False, executable=True)
    _existing_directory(config.canonical_data_root)
    _private_directory(config.shadow_root)
    marker_path = config.shadow_root / _MARKER_NAME
    try:
        existing_names = {item.name for item in config.shadow_root.iterdir()}
    except OSError:
        raise ExecutionShadowError("P5.S shadow root cannot be inspected") from None
    allowed_names = {
        _MARKER_NAME,
        "state",
        "staging",
        "artifacts",
        "results",
        "sandbox",
        "review",
    }
    if existing_names and _MARKER_NAME not in existing_names:
        raise ExecutionShadowError("P5.S nonempty shadow root is not marked")
    if not existing_names <= allowed_names:
        raise ExecutionShadowError("P5.S shadow root contains unknown entries")
    marker = {
        "schema_version": _MARKER_VERSION,
        "purpose": "p5s_existing_scraper_shadow",
        "venue_id": venue_id,
        "year": year,
        "completeness_level": "archival",
        "download_pdfs": True,
        "expected_count": expected_count,
    }
    _retain_create_once(marker_path, _canonical_bytes(marker))
    for name in ("state", "staging", "artifacts", "results", "sandbox", "review"):
        _private_directory(config.shadow_root / name)


def build_shadow_job(*, venue_id: str, year: int, expected_count: int) -> dict[str, Any]:
    """Build the one deterministic archival job used by a P5.S review."""
    return build_job(
        request_id=f"p5s-shadow:{venue_id}:{year}:archival:v1",
        job_type=JobType.SCRAPE_EXISTING,
        venue_id=venue_id,
        year=year,
        requested_by="human",
        input_artifact_ids=(f"shadow-authorization:p5s:{venue_id}:{year}:v1",),
        payload={
            "completeness_level": "archival",
            "download_pdfs": True,
            "expected_count": expected_count,
        },
    )


def build_pipeline_config(config: ExecutionShadowConfig) -> P5ExecutionConfig:
    """Bind P5.4 only to the private P5.S subtrees."""
    state = config.shadow_root / "state"
    staging = config.shadow_root / "staging"
    artifacts = config.shadow_root / "artifacts"
    policy = DiskSpacePolicy(
        minimum_free_bytes=config.minimum_free_bytes,
        minimum_free_fraction=config.minimum_free_fraction,
    )
    return P5ExecutionConfig(
        worker_safety=WorkerSafetyConfig(
            state_root=state,
            data_root=staging,
            timeout_seconds=config.timeout_seconds,
            cancellation_grace_seconds=config.cancellation_grace_seconds,
            disk_policy=policy,
        ),
        staging_executor=StagingExecutorConfig(
            repository_root=config.repository_root,
            python_executable=config.python_executable,
            staging_root=staging,
            canonical_data_root=config.canonical_data_root,
            timeout_seconds=config.timeout_seconds,
            cancellation_grace_seconds=config.cancellation_grace_seconds,
        ),
        staging_validation=StagingValidationConfig(
            staging_root=staging,
            artifact_root=artifacts,
            canonical_data_root=config.canonical_data_root,
            minimum_pdf_size=config.minimum_pdf_size,
        ),
        worker_id=config.worker_id,
    )


def _sandbox_string(path: Path) -> str:
    value = str(path)
    if any(character in value for character in ('"', "\n", "\r", "\x00")):
        raise ExecutionShadowError("P5.S guarded path cannot enter sandbox profile")
    return value.replace("\\", "\\\\")


def retain_sandbox_profile(config: ExecutionShadowConfig) -> Path:
    """Create or replay the fixed write-only-below-shadow profile."""
    profile = (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        f'(allow file-write* (subpath "{_sandbox_string(config.shadow_root)}"))\n'
        f'(deny file-write* (subpath "{_sandbox_string(config.repository_root)}"))\n'
        f'(deny file-write* (subpath "{_sandbox_string(config.canonical_data_root)}"))\n'
    ).encode("utf-8")
    path = config.shadow_root / _SANDBOX_PROFILE
    _retain_create_once(path, profile)
    return path


class _SandboxedHandle:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    def wait(
        self, *, timeout_seconds: float, cancellation: CancellationSignal
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
        first = timeout_seconds / 2
        try:
            self._process.wait(timeout=first)
            return True
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self._process.wait(timeout=timeout_seconds - first)
            return True
        except subprocess.TimeoutExpired:
            return False


class SandboxedSubprocessLauncher:
    """No-shell P5.2 launcher with OS-enforced guarded-root write denial."""

    def __init__(self, profile_path: Path, *, sandbox_executable: Path) -> None:
        self._profile = _normalized(profile_path)
        self._sandbox = _normalized(sandbox_executable)
        _safe_runtime_path(self._profile, directory=False)
        _safe_runtime_path(self._sandbox, directory=False, executable=True)

    def start(self, request: StagedProcessRequest) -> ProcessHandle:
        if not isinstance(request, StagedProcessRequest):
            raise TypeError("request must be StagedProcessRequest")
        _private_directory(request.log_path.parent, create=False)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = None
        try:
            descriptor = os.open(request.log_path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            ):
                raise ExecutionShadowError("P5.S process log metadata is unsafe")
            with os.fdopen(descriptor, "ab", buffering=0, closefd=True) as output:
                descriptor = None
                process = subprocess.Popen(
                    (
                        str(self._sandbox),
                        "-f",
                        str(self._profile),
                        *request.argv,
                    ),
                    cwd=request.cwd,
                    env=request.environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    start_new_session=True,
                    close_fds=True,
                )
        except ExecutionShadowError:
            raise
        except OSError:
            raise ExecutionShadowError("P5.S sandboxed process could not start") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return _SandboxedHandle(process)


class LocalImmutableResultStore:
    """Private create-only filesystem implementation of the P4.4 publisher."""

    def __init__(self, root: Path) -> None:
        self._root = _normalized(root)
        _private_directory(self._root)

    def publish(
        self,
        job: Mapping[str, Any],
        manifest: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> PublishedResultBundle:
        validate_result_bundle(job, manifest, result)
        manifest_name = manifest_object_name(job["job_id"])
        result_name = result_object_name(job["job_id"])
        _retain_create_once(
            self._root / manifest_name, _canonical_bytes(manifest)
        )
        _retain_create_once(self._root / result_name, _canonical_bytes(result))
        return PublishedResultBundle(
            job_id=job["job_id"],
            manifest_name=manifest_name,
            manifest_generation=1,
            result_name=result_name,
            result_generation=1,
        )
