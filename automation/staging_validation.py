"""P5.3 safe staged-output validation and local manifest generation.

This module is an explicitly called, local-only boundary.  It reads a
confirmed P5.2 staging tree, retains strict artifacts below a separate private
root, and never schedules work, starts a process, publishes a result, or
touches canonical data.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from automation.contracts import (
    ContractName,
    artifact_fingerprint,
    validate_contract,
)
from automation.domain import assert_secret_free
from automation.job_queue import JobType, validate_job_identity
from automation.job_results import (
    JobResultError,
    build_job_manifest,
    validate_job_manifest,
)
from automation.staging_executor import (
    StagingCheckpointStatus,
    StagingCheckpointStore,
)
from postprocessing.validate_year import validate as validate_year


_INVENTORY_SCHEMA_VERSION = 1
_REPORT_SCHEMA_VERSION = 1
_INVENTORY_NAME = "inventory.v1.json"
_CANDIDATE_MANIFEST_NAME = "manifest.v1.json"
_VALIDATION_REPORT_NAME = "report.v1.json"
_VALIDATION_MANIFEST_NAME = "manifest.v1.json"
_MAX_FILES = 100_000
_MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_ALLOWED_PDF_ISSUES = frozenset(
    {
        "missing_pdf_path",
        "missing_pdf_file",
        "undersized_pdf",
        "invalid_pdf_signature",
        "unreadable_pdf",
    }
)


class StagingValidationError(ValueError):
    """Raised when P5.3 input, identity, or filesystem safety fails closed."""


class StagingArtifactConflictError(StagingValidationError):
    """Raised when a create-once local artifact has conflicting content."""


@dataclass(frozen=True)
class StagingValidationConfig:
    """Explicit pairwise-disjoint roots and bounded PDF policy."""

    staging_root: Path
    artifact_root: Path
    canonical_data_root: Path
    minimum_pdf_size: int = 1024

    def __post_init__(self) -> None:
        paths = (self.staging_root, self.artifact_root, self.canonical_data_root)
        if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
            raise ValueError("P5.3 roots must be absolute Path values")
        if (
            not isinstance(self.minimum_pdf_size, int)
            or isinstance(self.minimum_pdf_size, bool)
            or not 1 <= self.minimum_pdf_size <= _MAX_FILE_BYTES
        ):
            raise ValueError("minimum_pdf_size is outside the safe bound")


@dataclass(frozen=True)
class CandidateBundle:
    """Defensive candidate inventory and strict scrape-job manifest."""

    inventory: dict[str, Any]
    manifest: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "inventory": deepcopy(self.inventory),
            "manifest": deepcopy(self.manifest),
        }


@dataclass(frozen=True)
class ValidationBundle:
    """Defensive independent report and strict validation-job manifest."""

    report: dict[str, Any]
    manifest: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "report": deepcopy(self.report),
            "manifest": deepcopy(self.manifest),
        }


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StagingValidationError("artifact is not canonical JSON") from exc
    return (serialized + "\n").encode("utf-8")


def _format_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StagingValidationError("P5.3 clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise StagingValidationError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise StagingValidationError(f"{field} is invalid") from None
    if parsed.tzinfo is None:
        raise StagingValidationError(f"{field} is invalid")
    return parsed.astimezone(timezone.utc)


def _normalized(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        raise StagingValidationError("configured root could not be normalized") from None


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _validate_config(config: StagingValidationConfig) -> None:
    if not isinstance(config, StagingValidationConfig):
        raise TypeError("config must be StagingValidationConfig")
    roots = (config.staging_root, config.artifact_root, config.canonical_data_root)
    normalized = tuple(_normalized(path) for path in roots)
    if normalized != roots:
        raise StagingValidationError("P5.3 roots must be normalized")
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if _within(left, right) or _within(right, left):
                raise StagingValidationError("P5.3 roots must be pairwise disjoint")


def _safe_owned_directory(path: Path, *, create: bool = False) -> None:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError:
        raise StagingValidationError("private P5.3 directory is unavailable") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise StagingValidationError("private P5.3 directory metadata is unsafe")


def _private_owned_directory(path: Path, *, create: bool = False) -> None:
    _safe_owned_directory(path, create=create)
    try:
        metadata = path.lstat()
    except OSError:
        raise StagingValidationError("private P5.3 directory is unavailable") from None
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise StagingValidationError("private P5.3 directory is not private")


def _ensure_private_descendant(root: Path, target: Path) -> None:
    _private_owned_directory(root)
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise StagingValidationError("artifact directory escaped its private root") from None
    current = root
    for part in relative.parts:
        current = current / part
        _private_owned_directory(current, create=True)


def _job_paths(
    scrape_job: Mapping[str, Any], config: StagingValidationConfig
) -> tuple[Path, Path, Path]:
    staging_job_root = config.staging_root / scrape_job["job_fingerprint"]
    data_root = staging_job_root / "data"
    artifact_job_root = config.artifact_root / scrape_job["job_fingerprint"]
    return staging_job_root, data_root, artifact_job_root


def _require_succeeded_checkpoint(
    scrape_job: Mapping[str, Any], staging_job_root: Path
) -> datetime:
    checkpoint = StagingCheckpointStore(staging_job_root, scrape_job).read()
    if checkpoint.status is not StagingCheckpointStatus.PROCESS_SUCCEEDED:
        raise StagingValidationError("candidate requires confirmed process success")
    return _parse_time(checkpoint.updated_at, field="process-success checkpoint time")


def _open_regular_for_read(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise StagingValidationError("staged file is unavailable or unsafe") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or metadata.st_size < 0
        or metadata.st_size > _MAX_FILE_BYTES
    ):
        os.close(descriptor)
        raise StagingValidationError("staged file metadata is unsafe")
    return descriptor, metadata


def _hash_regular_file(path: Path) -> tuple[str, int]:
    descriptor, before = _open_regular_for_read(path)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(descriptor, "rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_FILE_BYTES:
                    raise StagingValidationError("staged file exceeds the safe bound")
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except StagingValidationError:
        raise
    except OSError:
        raise StagingValidationError("staged file could not be read safely") from None
    stable = (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size == total
        and before.st_mtime_ns == after.st_mtime_ns
    )
    if not stable:
        raise StagingValidationError("staged file changed while being inventoried")
    return digest.hexdigest(), total


def _read_regular_bytes(path: Path, *, maximum: int) -> bytes:
    descriptor, before = _open_regular_for_read(path)
    if before.st_size > maximum:
        os.close(descriptor)
        raise StagingValidationError("staged metadata exceeds the safe bound")
    try:
        with os.fdopen(descriptor, "rb") as handle:
            content = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
    except OSError:
        raise StagingValidationError("staged metadata could not be read safely") from None
    if len(content) > maximum:
        raise StagingValidationError("staged metadata exceeds the safe bound")
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(content) != before.st_size
    ):
        raise StagingValidationError("staged metadata changed while being read")
    return content


def _inventory_files(data_root: Path) -> list[dict[str, Any]]:
    _safe_owned_directory(data_root)
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    pending = [data_root]
    while pending:
        directory = pending.pop()
        _safe_owned_directory(directory)
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            raise StagingValidationError("staged directory could not be inspected") from None
        for child in children:
            path = Path(child.path)
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError:
                raise StagingValidationError("staged entry could not be inspected") from None
            if child.is_symlink():
                raise StagingValidationError("staged tree contains a symlink")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise StagingValidationError("staged tree contains a special file")
            fingerprint, size_bytes = _hash_regular_file(path)
            try:
                relative = path.relative_to(data_root).as_posix()
            except ValueError:
                raise StagingValidationError("staged file escaped the data root") from None
            entries.append(
                {
                    "object_name": relative,
                    "content_fingerprint": fingerprint,
                    "size_bytes": size_bytes,
                }
            )
            total_bytes += size_bytes
            if len(entries) > _MAX_FILES or total_bytes > _MAX_TOTAL_BYTES:
                raise StagingValidationError("staged tree exceeds the inventory bound")
    entries.sort(key=lambda item: item["object_name"])
    return entries


def _metadata_name(job: Mapping[str, Any]) -> str:
    venue = job["venue_id"]
    return f"metadata/{venue}/{venue}_{job['year']}.json"


def _build_inventory(
    scrape_job: Mapping[str, Any], data_root: Path, *, captured_at: str
) -> dict[str, Any]:
    files = _inventory_files(data_root)
    names = [item["object_name"] for item in files]
    metadata_name = _metadata_name(scrape_job)
    if metadata_name not in names:
        raise StagingValidationError("staged metadata file is missing")
    payload: dict[str, Any] = {
        "schema_version": _INVENTORY_SCHEMA_VERSION,
        "job_id": scrape_job["job_id"],
        "job_fingerprint": scrape_job["job_fingerprint"],
        "venue_id": scrape_job["venue_id"],
        "year": scrape_job["year"],
        "captured_at": captured_at,
        "metadata_object_name": metadata_name,
        "file_count": len(files),
        "total_size_bytes": sum(item["size_bytes"] for item in files),
        "files": files,
    }
    payload["inventory_fingerprint"] = artifact_fingerprint(payload)
    _validate_inventory(payload, scrape_job)
    return payload


def _validate_inventory(inventory: Mapping[str, Any], scrape_job: Mapping[str, Any]) -> None:
    validate_job_identity(scrape_job)
    expected_keys = {
        "schema_version",
        "job_id",
        "job_fingerprint",
        "venue_id",
        "year",
        "captured_at",
        "metadata_object_name",
        "file_count",
        "total_size_bytes",
        "files",
        "inventory_fingerprint",
    }
    if not isinstance(inventory, Mapping) or set(inventory) != expected_keys:
        raise StagingValidationError("candidate inventory shape is invalid")
    if (
        inventory["schema_version"] != _INVENTORY_SCHEMA_VERSION
        or inventory["job_id"] != scrape_job["job_id"]
        or inventory["job_fingerprint"] != scrape_job["job_fingerprint"]
        or inventory["venue_id"] != scrape_job["venue_id"]
        or inventory["year"] != scrape_job["year"]
        or inventory["metadata_object_name"] != _metadata_name(scrape_job)
        or not isinstance(inventory["files"], list)
        or not isinstance(inventory["file_count"], int)
        or isinstance(inventory["file_count"], bool)
        or not isinstance(inventory["total_size_bytes"], int)
        or isinstance(inventory["total_size_bytes"], bool)
    ):
        raise StagingValidationError("candidate inventory conflicts with its job")
    _parse_time(inventory["captured_at"], field="inventory captured_at")
    names: list[str] = []
    total = 0
    for item in inventory["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"object_name", "content_fingerprint", "size_bytes"}
            or not _safe_object_name(item["object_name"])
            or not isinstance(item["content_fingerprint"], str)
            or len(item["content_fingerprint"]) != 64
            or any(character not in "0123456789abcdef" for character in item["content_fingerprint"])
            or not isinstance(item["size_bytes"], int)
            or isinstance(item["size_bytes"], bool)
            or not 0 <= item["size_bytes"] <= _MAX_FILE_BYTES
        ):
            raise StagingValidationError("candidate inventory file entry is invalid")
        names.append(item["object_name"])
        total += item["size_bytes"]
    if (
        names != sorted(names)
        or len(names) != len(set(names))
        or inventory["file_count"] != len(names)
        or inventory["total_size_bytes"] != total
        or not 1 <= len(names) <= _MAX_FILES
        or total > _MAX_TOTAL_BYTES
        or inventory["metadata_object_name"] not in names
    ):
        raise StagingValidationError("candidate inventory summary is invalid")
    expected_fingerprint = artifact_fingerprint(
        {key: deepcopy(value) for key, value in inventory.items() if key != "inventory_fingerprint"}
    )
    if inventory["inventory_fingerprint"] != expected_fingerprint:
        raise StagingValidationError("candidate inventory fingerprint is invalid")
    assert_secret_free(inventory)


def _safe_object_name(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _artifact_summary(
    *, artifact_id: str, artifact_kind: str, object_name: str, content: bytes
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "artifact_kind": artifact_kind,
        "object_name": object_name,
        "content_fingerprint": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _read_json_artifact(path: Path) -> dict[str, Any]:
    content = _read_regular_bytes(path, maximum=64 * 1024 * 1024)
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StagingArtifactConflictError("retained P5.3 artifact is corrupt") from None
    if not isinstance(payload, dict) or _canonical_bytes(payload) != content:
        raise StagingArtifactConflictError("retained P5.3 artifact is not canonical")
    return payload


def _retain_create_once(path: Path, payload: Mapping[str, Any]) -> None:
    content = _canonical_bytes(payload)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or _read_regular_bytes(path, maximum=len(content)) != content:
            raise StagingArtifactConflictError("retained P5.3 artifact conflicts")
        return
    _private_owned_directory(path.parent)
    temporary = path.parent / f".{path.name}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if path.is_symlink() or _read_regular_bytes(path, maximum=len(content)) != content:
                raise StagingArtifactConflictError("concurrent P5.3 artifact conflicts") from None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except StagingArtifactConflictError:
        raise
    except FileExistsError:
        raise StagingArtifactConflictError("P5.3 temporary artifact already exists") from None
    except OSError:
        raise StagingValidationError("P5.3 artifact could not be retained") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    if _read_regular_bytes(path, maximum=len(content)) != content:
        raise StagingArtifactConflictError("P5.3 artifact did not retain exactly")


def _validate_manifest_payload(
    manifest: Mapping[str, Any], job: Mapping[str, Any]
) -> None:
    try:
        validate_job_manifest(manifest, job)
    except (JobResultError, ValueError, TypeError, KeyError) as exc:
        raise StagingValidationError("retained manifest is invalid") from exc


def _current_inventory_matches(
    inventory: Mapping[str, Any], scrape_job: Mapping[str, Any], data_root: Path
) -> None:
    _validate_inventory(inventory, scrape_job)
    current = _build_inventory(
        scrape_job, data_root, captured_at=inventory["captured_at"]
    )
    if current != inventory:
        raise StagingArtifactConflictError("staged candidate changed after capture")


def capture_staging_candidate(
    scrape_job: Mapping[str, Any],
    config: StagingValidationConfig,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CandidateBundle:
    """Capture one confirmed P5.2 output as an immutable local candidate."""
    if not isinstance(scrape_job, Mapping):
        raise TypeError("scrape_job must be a mapping")
    if not callable(clock):
        raise TypeError("clock must be callable")
    validate_job_identity(scrape_job)
    if JobType(scrape_job["job_type"]) is not JobType.SCRAPE_EXISTING:
        raise StagingValidationError("candidate capture accepts only scrape jobs")
    _validate_config(config)
    _safe_owned_directory(config.staging_root)
    _private_owned_directory(config.artifact_root, create=True)
    staging_job_root, data_root, artifact_job_root = _job_paths(scrape_job, config)
    _safe_owned_directory(staging_job_root)
    process_succeeded_at = _require_succeeded_checkpoint(
        scrape_job, staging_job_root
    )

    candidate_root = artifact_job_root / "candidate"
    _ensure_private_descendant(config.artifact_root, candidate_root)
    inventory_path = candidate_root / _INVENTORY_NAME
    manifest_path = candidate_root / _CANDIDATE_MANIFEST_NAME
    if inventory_path.exists() or inventory_path.is_symlink():
        inventory = _read_json_artifact(inventory_path)
        try:
            _current_inventory_matches(inventory, scrape_job, data_root)
        except StagingArtifactConflictError:
            raise
        except StagingValidationError as exc:
            raise StagingArtifactConflictError(
                "retained candidate inventory is invalid"
            ) from exc
        if (
            _parse_time(inventory["captured_at"], field="inventory captured_at")
            < process_succeeded_at
        ):
            raise StagingArtifactConflictError(
                "candidate inventory predates process success"
            )
    else:
        captured_time = clock()
        captured_at = _format_time(captured_time)
        if captured_time.astimezone(timezone.utc) < process_succeeded_at:
            raise StagingValidationError("candidate capture predates process success")
        inventory = _build_inventory(scrape_job, data_root, captured_at=captured_at)
        _retain_create_once(inventory_path, inventory)

    inventory_content = _canonical_bytes(inventory)
    artifact = _artifact_summary(
        artifact_id=f"dataset:{hashlib.sha256(inventory_content).hexdigest()}",
        artifact_kind="staging_dataset",
        object_name=(
            f"candidates/{scrape_job['job_fingerprint']}/{_INVENTORY_NAME}"
        ),
        content=inventory_content,
    )
    manifest = build_job_manifest(
        scrape_job, created_at=inventory["captured_at"], artifacts=[artifact]
    )
    _retain_create_once(manifest_path, manifest)
    retained_manifest = _read_json_artifact(manifest_path)
    _validate_manifest_payload(retained_manifest, scrape_job)
    if retained_manifest != manifest:
        raise StagingArtifactConflictError("candidate manifest conflicts")
    return CandidateBundle(deepcopy(inventory), deepcopy(retained_manifest))


def _bind_validation_job(
    validation_job: Mapping[str, Any],
    scrape_job: Mapping[str, Any],
    candidate_manifest: Mapping[str, Any],
) -> tuple[str, bool, int | None]:
    validate_job_identity(validation_job)
    if JobType(validation_job["job_type"]) is not JobType.VALIDATE_CANDIDATE:
        raise StagingValidationError("P5.3 validation accepts only validation jobs")
    payload = validation_job["payload"]
    if (
        validation_job["venue_id"] != scrape_job["venue_id"]
        or validation_job["year"] != scrape_job["year"]
        or payload["candidate_manifest_id"] != candidate_manifest["manifest_id"]
        or candidate_manifest["manifest_id"] not in validation_job["input_artifact_ids"]
        or payload["completeness_level"] != scrape_job["payload"]["completeness_level"]
        or payload["expected_count"] != scrape_job["payload"]["expected_count"]
    ):
        raise StagingValidationError("validation job does not bind the candidate")
    effective_require_pdfs = (
        payload["require_pdfs"] or payload["completeness_level"] == "archival"
    )
    expected_require_pdfs = (
        scrape_job["payload"]["download_pdfs"]
        or scrape_job["payload"]["completeness_level"] == "archival"
    )
    if effective_require_pdfs != expected_require_pdfs:
        raise StagingValidationError("validation PDF policy does not bind the candidate")
    return (
        payload["completeness_level"],
        effective_require_pdfs,
        payload["expected_count"],
    )


def _inventory_index(inventory: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {item["object_name"]: item for item in inventory["files"]}


def _normalize_pdf_name(value: Any) -> str | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise StagingValidationError("pdf_path must be a relative string")
    candidate = value[5:] if value.startswith("data/") else value
    if not _safe_object_name(candidate):
        raise StagingValidationError("pdf_path escapes or is not normalized")
    return candidate


def _validate_pdfs(
    papers: Sequence[Mapping[str, Any]],
    *,
    data_root: Path,
    inventory: Mapping[str, Any],
    minimum_pdf_size: int,
) -> tuple[dict[str, int], int]:
    issues: dict[str, int] = {}
    valid_count = 0
    index = _inventory_index(inventory)
    for paper in papers:
        name = _normalize_pdf_name(paper.get("pdf_path"))
        if name is None:
            issues["missing_pdf_path"] = issues.get("missing_pdf_path", 0) + 1
            continue
        item = index.get(name)
        if item is None:
            issues["missing_pdf_file"] = issues.get("missing_pdf_file", 0) + 1
            continue
        path = data_root.joinpath(*PurePosixPath(name).parts)
        fingerprint, size_bytes = _hash_regular_file(path)
        if (
            fingerprint != item["content_fingerprint"]
            or size_bytes != item["size_bytes"]
        ):
            raise StagingArtifactConflictError("PDF changed after candidate capture")
        if size_bytes < minimum_pdf_size:
            issues["undersized_pdf"] = issues.get("undersized_pdf", 0) + 1
            continue
        descriptor, _ = _open_regular_for_read(path)
        try:
            with os.fdopen(descriptor, "rb") as handle:
                signature = handle.read(5)
        except OSError:
            issues["unreadable_pdf"] = issues.get("unreadable_pdf", 0) + 1
            continue
        if signature != b"%PDF-":
            issues["invalid_pdf_signature"] = issues.get(
                "invalid_pdf_signature", 0
            ) + 1
            continue
        valid_count += 1
    if not set(issues) <= _ALLOWED_PDF_ISSUES:
        raise StagingValidationError("unexpected PDF validation issue")
    return issues, valid_count


def _load_papers(
    inventory: Mapping[str, Any], data_root: Path
) -> list[Mapping[str, Any]]:
    metadata_name = inventory["metadata_object_name"]
    metadata_path = data_root.joinpath(*PurePosixPath(metadata_name).parts)
    content = _read_regular_bytes(metadata_path, maximum=256 * 1024 * 1024)
    item = _inventory_index(inventory)[metadata_name]
    if (
        hashlib.sha256(content).hexdigest() != item["content_fingerprint"]
        or len(content) != item["size_bytes"]
    ):
        raise StagingArtifactConflictError("metadata changed after candidate capture")
    try:
        papers = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StagingValidationError("staged metadata is not valid JSON") from None
    if not isinstance(papers, list) or any(not isinstance(item, dict) for item in papers):
        raise StagingValidationError("staged metadata must be a list of objects")
    return papers


def _build_report(
    validation_job: Mapping[str, Any],
    scrape_job: Mapping[str, Any],
    candidate: CandidateBundle,
    *,
    validated_at: str,
    level: str,
    require_pdfs: bool,
    expected_count: int | None,
    papers: Sequence[Mapping[str, Any]],
    issues: Mapping[str, int],
    valid_pdf_count: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "validation_job_id": validation_job["job_id"],
        "validation_job_fingerprint": validation_job["job_fingerprint"],
        "scrape_job_id": scrape_job["job_id"],
        "candidate_manifest_id": candidate.manifest["manifest_id"],
        "candidate_inventory_fingerprint": candidate.inventory["inventory_fingerprint"],
        "venue_id": validation_job["venue_id"],
        "year": validation_job["year"],
        "validated_at": validated_at,
        "completeness_level": level,
        "require_pdfs": require_pdfs,
        "expected_count": expected_count,
        "status": "valid" if not issues else "invalid",
        "metrics": {
            "paper_count": len(papers),
            "valid_pdf_count": valid_pdf_count if require_pdfs else None,
        },
        "issues": {key: issues[key] for key in sorted(issues) if issues[key]},
    }
    fingerprint = artifact_fingerprint(payload)
    payload["report_id"] = f"validation:{fingerprint}"
    payload["report_fingerprint"] = fingerprint
    _validate_report(payload, validation_job, scrape_job, candidate)
    return payload


def _validate_report(
    report: Mapping[str, Any],
    validation_job: Mapping[str, Any],
    scrape_job: Mapping[str, Any],
    candidate: CandidateBundle,
) -> None:
    expected_keys = {
        "schema_version",
        "validation_job_id",
        "validation_job_fingerprint",
        "scrape_job_id",
        "candidate_manifest_id",
        "candidate_inventory_fingerprint",
        "venue_id",
        "year",
        "validated_at",
        "completeness_level",
        "require_pdfs",
        "expected_count",
        "status",
        "metrics",
        "issues",
        "report_id",
        "report_fingerprint",
    }
    if not isinstance(report, Mapping) or set(report) != expected_keys:
        raise StagingValidationError("validation report shape is invalid")
    try:
        validate_contract(ContractName.VALIDATION_REPORT, report)
    except ValueError as exc:
        raise StagingValidationError("validation report contract is invalid") from exc
    level, require_pdfs, expected_count = _bind_validation_job(
        validation_job, scrape_job, candidate.manifest
    )
    if (
        report["schema_version"] != _REPORT_SCHEMA_VERSION
        or report["validation_job_id"] != validation_job["job_id"]
        or report["validation_job_fingerprint"] != validation_job["job_fingerprint"]
        or report["scrape_job_id"] != scrape_job["job_id"]
        or report["candidate_manifest_id"] != candidate.manifest["manifest_id"]
        or report["candidate_inventory_fingerprint"]
        != candidate.inventory["inventory_fingerprint"]
        or report["venue_id"] != validation_job["venue_id"]
        or report["year"] != validation_job["year"]
        or report["completeness_level"] != level
        or report["require_pdfs"] is not require_pdfs
        or report["expected_count"] != expected_count
        or report["status"] not in {"valid", "invalid"}
        or not isinstance(report["metrics"], dict)
        or set(report["metrics"]) != {"paper_count", "valid_pdf_count"}
        or not isinstance(report["metrics"]["paper_count"], int)
        or isinstance(report["metrics"]["paper_count"], bool)
        or report["metrics"]["paper_count"] < 0
        or (
            require_pdfs
            and (
                not isinstance(report["metrics"]["valid_pdf_count"], int)
                or isinstance(report["metrics"]["valid_pdf_count"], bool)
                or report["metrics"]["valid_pdf_count"] < 0
            )
        )
        or (not require_pdfs and report["metrics"]["valid_pdf_count"] is not None)
        or not isinstance(report["issues"], dict)
        or any(
            not isinstance(key, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for key, value in report["issues"].items()
        )
        or (report["status"] == "valid") != (not report["issues"])
    ):
        raise StagingValidationError("validation report conflicts with its inputs")
    validated_at = _parse_time(report["validated_at"], field="report validated_at")
    captured_at = _parse_time(
        candidate.inventory["captured_at"], field="inventory captured_at"
    )
    if validated_at < captured_at:
        raise StagingValidationError("validation report predates its candidate")
    base = {
        key: deepcopy(value)
        for key, value in report.items()
        if key not in {"report_id", "report_fingerprint"}
    }
    fingerprint = artifact_fingerprint(base)
    if (
        report["report_fingerprint"] != fingerprint
        or report["report_id"] != f"validation:{fingerprint}"
    ):
        raise StagingValidationError("validation report fingerprint is invalid")
    assert_secret_free(report)


def validate_staging_candidate(
    validation_job: Mapping[str, Any],
    scrape_job: Mapping[str, Any],
    candidate: CandidateBundle,
    config: StagingValidationConfig,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ValidationBundle:
    """Independently validate an exact captured candidate and retain reports."""
    if not isinstance(validation_job, Mapping) or not isinstance(scrape_job, Mapping):
        raise TypeError("validation_job and scrape_job must be mappings")
    if not isinstance(candidate, CandidateBundle):
        raise TypeError("candidate must be CandidateBundle")
    if not callable(clock):
        raise TypeError("clock must be callable")
    validate_job_identity(scrape_job)
    if JobType(scrape_job["job_type"]) is not JobType.SCRAPE_EXISTING:
        raise StagingValidationError("candidate source must be a scrape job")
    _validate_config(config)
    _private_owned_directory(config.artifact_root)
    _validate_inventory(candidate.inventory, scrape_job)
    _validate_manifest_payload(candidate.manifest, scrape_job)
    level, require_pdfs, expected_count = _bind_validation_job(
        validation_job, scrape_job, candidate.manifest
    )
    staging_job_root, data_root, artifact_job_root = _job_paths(scrape_job, config)
    _safe_owned_directory(staging_job_root)
    _require_succeeded_checkpoint(scrape_job, staging_job_root)
    _current_inventory_matches(candidate.inventory, scrape_job, data_root)

    candidate_root = artifact_job_root / "candidate"
    retained_inventory = _read_json_artifact(candidate_root / _INVENTORY_NAME)
    retained_candidate_manifest = _read_json_artifact(
        candidate_root / _CANDIDATE_MANIFEST_NAME
    )
    if (
        retained_inventory != candidate.inventory
        or retained_candidate_manifest != candidate.manifest
    ):
        raise StagingArtifactConflictError("supplied candidate is not the retained candidate")

    validation_root = (
        artifact_job_root / "validations" / validation_job["job_fingerprint"]
    )
    _ensure_private_descendant(config.artifact_root, validation_root)
    report_path = validation_root / _VALIDATION_REPORT_NAME
    manifest_path = validation_root / _VALIDATION_MANIFEST_NAME
    if report_path.exists() or report_path.is_symlink():
        report = _read_json_artifact(report_path)
        _validate_report(report, validation_job, scrape_job, candidate)
    else:
        papers = _load_papers(candidate.inventory, data_root)
        issues = validate_year(
            papers,
            data_root,
            level=level,
            require_pdfs=False,
            expected_count=expected_count,
            minimum_pdf_size=config.minimum_pdf_size,
        )
        valid_pdf_count = 0
        if require_pdfs:
            pdf_issues, valid_pdf_count = _validate_pdfs(
                papers,
                data_root=data_root,
                inventory=candidate.inventory,
                minimum_pdf_size=config.minimum_pdf_size,
            )
            for key, value in pdf_issues.items():
                issues[key] = issues.get(key, 0) + value
        report = _build_report(
            validation_job,
            scrape_job,
            candidate,
            validated_at=_format_time(clock()),
            level=level,
            require_pdfs=require_pdfs,
            expected_count=expected_count,
            papers=papers,
            issues=issues,
            valid_pdf_count=valid_pdf_count,
        )
        _retain_create_once(report_path, report)

    report_content = _canonical_bytes(report)
    inventory_content = _canonical_bytes(candidate.inventory)
    artifacts = [
        _artifact_summary(
            artifact_id=f"dataset:{hashlib.sha256(inventory_content).hexdigest()}",
            artifact_kind="staging_dataset",
            object_name=(
                f"candidates/{scrape_job['job_fingerprint']}/{_INVENTORY_NAME}"
            ),
            content=inventory_content,
        ),
        _artifact_summary(
            artifact_id=report["report_id"],
            artifact_kind="validation_report",
            object_name=(
                "validation-reports/"
                f"{validation_job['job_fingerprint']}/{_VALIDATION_REPORT_NAME}"
            ),
            content=report_content,
        ),
    ]
    manifest = build_job_manifest(
        validation_job, created_at=report["validated_at"], artifacts=artifacts
    )
    _retain_create_once(manifest_path, manifest)
    retained_manifest = _read_json_artifact(manifest_path)
    _validate_manifest_payload(retained_manifest, validation_job)
    if retained_manifest != manifest:
        raise StagingArtifactConflictError("validation manifest conflicts")
    _current_inventory_matches(candidate.inventory, scrape_job, data_root)
    return ValidationBundle(deepcopy(report), deepcopy(retained_manifest))
