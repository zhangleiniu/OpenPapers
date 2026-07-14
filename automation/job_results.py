"""P4.4 immutable job-result contracts and GCS-compatible object protocol.

This module does not construct a cloud client, read credentials, run a job, or
mutate cloud control state.  It operates on an explicitly injected bucket-like
object so tests can prove GCS generation-precondition semantics with fakes.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from automation.contracts import (
    ContractName,
    artifact_fingerprint,
    validate_contract,
)
from automation.domain import (
    ArtifactKind,
    Writer,
    assert_secret_free,
    assert_writer_allowed,
)
from automation.job_queue import validate_job_identity


class JobResultError(ValueError):
    """Raised when a result bundle or immutable-object operation fails closed."""


class ImmutableObjectConflictError(JobResultError):
    """Raised when a fixed object name already contains different bytes."""


@dataclass(frozen=True)
class StoredObject:
    """One immutable JSON object read at an exact positive generation."""

    name: str
    generation: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class PublishedResultBundle:
    """Stable publication receipt for an immutable manifest/result pair."""

    job_id: str
    manifest_name: str
    manifest_generation: int
    result_name: str
    result_generation: int


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
        raise JobResultError(f"artifact is not canonical JSON: {exc}") from exc
    return (serialized + "\n").encode("utf-8")


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise JobResultError(f"{field} must be a datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JobResultError(f"{field} must be a valid datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JobResultError(f"{field} must include a timezone")
    return parsed


def _without(payload: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in payload.items()
        if key not in keys
    }


def manifest_object_name(job_id: str) -> str:
    """Return the fixed immutable manifest object name for one v2 job ID."""
    if not isinstance(job_id, str) or not job_id.startswith("job:"):
        raise JobResultError("manifest object name requires a job ID")
    return f"manifests/{job_id}.json"


def result_object_name(job_id: str) -> str:
    """Return the fixed immutable result object name for one v2 job ID."""
    if not isinstance(job_id, str) or not job_id.startswith("job:"):
        raise JobResultError("result object name requires a job ID")
    return f"job-results/{job_id}.json"


def build_job_manifest(
    job: Mapping[str, Any],
    *,
    created_at: str,
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a strict deterministic manifest for already-produced artifacts."""
    validate_job_identity(job)
    artifact_copies = [deepcopy(dict(item)) for item in artifacts]
    artifact_copies.sort(key=lambda item: str(item.get("artifact_id", "")))
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "job_id": job["job_id"],
        "job_fingerprint": job["job_fingerprint"],
        "job_type": job["job_type"],
        "venue_id": job["venue_id"],
        "year": job["year"],
        "created_at": created_at,
        "artifacts": artifact_copies,
    }
    fingerprint = artifact_fingerprint(manifest)
    manifest["manifest_id"] = f"manifest:{fingerprint}"
    manifest["manifest_fingerprint"] = fingerprint
    validate_job_manifest(manifest, job)
    return deepcopy(manifest)


def validate_job_manifest(
    manifest: Mapping[str, Any],
    job: Mapping[str, Any],
) -> None:
    """Validate a manifest and bind every identity field to its v2 job."""
    validate_job_identity(job)
    assert_secret_free(manifest)
    validate_contract(ContractName.JOB_MANIFEST, manifest)
    expected_identity = (
        job["job_id"],
        job["job_fingerprint"],
        job["job_type"],
        job["venue_id"],
        job["year"],
    )
    actual_identity = (
        manifest["job_id"],
        manifest["job_fingerprint"],
        manifest["job_type"],
        manifest["venue_id"],
        manifest["year"],
    )
    if actual_identity != expected_identity:
        raise JobResultError("manifest identity does not match the immutable job")
    fingerprint = artifact_fingerprint(
        _without(manifest, "manifest_id", "manifest_fingerprint")
    )
    if manifest["manifest_fingerprint"] != fingerprint:
        raise JobResultError("manifest_fingerprint does not match manifest fields")
    if manifest["manifest_id"] != f"manifest:{fingerprint}":
        raise JobResultError("manifest_id does not match manifest fingerprint")
    artifact_ids = [item["artifact_id"] for item in manifest["artifacts"]]
    object_names = [item["object_name"] for item in manifest["artifacts"]]
    if len(set(artifact_ids)) != len(artifact_ids):
        raise JobResultError("manifest artifact IDs must be unique")
    if len(set(object_names)) != len(object_names):
        raise JobResultError("manifest artifact object names must be unique")
    if artifact_ids != sorted(artifact_ids):
        raise JobResultError("manifest artifacts must be ordered by artifact ID")
    _parse_timestamp(manifest["created_at"], field="manifest created_at")


def build_job_result(
    job: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    worker_id: str,
    completed_at: str,
    status: str,
    error_code: str | None,
    error_summary: str | None,
    duration_seconds: float,
    paper_count: int | None,
    valid_pdf_count: int | None,
) -> dict[str, Any]:
    """Build a terminal result for an already-produced strict manifest."""
    validate_job_manifest(manifest, job)
    result: dict[str, Any] = {
        "schema_version": 2,
        "job_id": job["job_id"],
        "job_fingerprint": job["job_fingerprint"],
        "job_type": job["job_type"],
        "venue_id": job["venue_id"],
        "year": job["year"],
        "worker_id": worker_id,
        "completed_at": completed_at,
        "status": status,
        "manifest_id": manifest["manifest_id"],
        "error_code": error_code,
        "error_summary": error_summary,
        "metrics": {
            "duration_seconds": duration_seconds,
            "paper_count": paper_count,
            "valid_pdf_count": valid_pdf_count,
        },
    }
    result["result_fingerprint"] = artifact_fingerprint(result)
    validate_job_result(result, manifest, job)
    return deepcopy(result)


def validate_job_result(
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    job: Mapping[str, Any],
) -> None:
    """Cross-validate a v2 result, its manifest, and immutable job."""
    validate_job_manifest(manifest, job)
    assert_secret_free(result)
    validate_contract(ContractName.JOB_RESULT, result)
    if result["schema_version"] != 2:
        raise JobResultError("the P4.4 boundary accepts only v2 job results")
    expected_identity = (
        job["job_id"],
        job["job_fingerprint"],
        job["job_type"],
        job["venue_id"],
        job["year"],
        manifest["manifest_id"],
    )
    actual_identity = (
        result["job_id"],
        result["job_fingerprint"],
        result["job_type"],
        result["venue_id"],
        result["year"],
        result["manifest_id"],
    )
    if actual_identity != expected_identity:
        raise JobResultError("result identity does not match its job and manifest")
    fingerprint = artifact_fingerprint(_without(result, "result_fingerprint"))
    if result["result_fingerprint"] != fingerprint:
        raise JobResultError("result_fingerprint does not match result fields")
    created = _parse_timestamp(manifest["created_at"], field="manifest created_at")
    completed = _parse_timestamp(result["completed_at"], field="result completed_at")
    if completed < created:
        raise JobResultError("result cannot complete before its manifest was created")
    if result["status"] == "succeeded" and not manifest["artifacts"]:
        raise JobResultError("a succeeded result requires at least one artifact")
    if result["status"] != "succeeded" and (
        result["metrics"]["paper_count"] is not None
        or result["metrics"]["valid_pdf_count"] is not None
    ):
        raise JobResultError("non-success results cannot claim paper/PDF counts")


def validate_result_bundle(
    job: Mapping[str, Any],
    manifest: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    """Public cross-artifact validation entry point."""
    validate_job_result(result, manifest, job)


def _default_precondition_errors() -> tuple[type[BaseException], ...]:
    try:
        from google.api_core.exceptions import PreconditionFailed
    except ImportError:
        return ()
    return (PreconditionFailed,)


class GcsImmutableResultStore:
    """GCS bucket adapter using create-only and exact-generation operations."""

    def __init__(
        self,
        bucket: Any,
        *,
        precondition_error_types: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        if bucket is None or not callable(getattr(bucket, "blob", None)):
            raise TypeError("GCS result store requires a bucket-like object")
        self._bucket = bucket
        self._precondition_errors = (
            _default_precondition_errors()
            if precondition_error_types is None
            else precondition_error_types
        )

    @staticmethod
    def _generation(blob: Any) -> int:
        raw = getattr(blob, "generation", None)
        try:
            generation = int(raw)
        except (TypeError, ValueError) as exc:
            raise JobResultError("GCS object has no valid generation") from exc
        if generation < 1:
            raise JobResultError("GCS object generation must be positive")
        return generation

    def _read_bytes(self, name: str) -> tuple[int, bytes]:
        blob = self._bucket.blob(name)
        blob.reload()
        generation = self._generation(blob)
        data = blob.download_as_bytes(if_generation_match=generation)
        if not isinstance(data, bytes):
            raise JobResultError("GCS object download did not return bytes")
        return generation, data

    def _create_or_replay(self, name: str, data: bytes) -> int:
        blob = self._bucket.blob(name)
        try:
            blob.upload_from_string(
                data,
                content_type="application/json",
                if_generation_match=0,
            )
        except self._precondition_errors:
            generation, existing = self._read_bytes(name)
            if existing != data:
                raise ImmutableObjectConflictError(
                    f"immutable object {name!r} already has different content"
                ) from None
            return generation
        return self._generation(blob)

    def publish(
        self,
        job: Mapping[str, Any],
        manifest: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        writer: Writer | str = Writer.MAC_WORKER,
    ) -> PublishedResultBundle:
        """Create manifest then result, accepting only byte-identical replay."""
        assert_writer_allowed(writer, ArtifactKind.MANIFEST)
        assert_writer_allowed(writer, ArtifactKind.JOB_RESULT)
        validate_result_bundle(job, manifest, result)
        manifest_name = manifest_object_name(job["job_id"])
        result_name = result_object_name(job["job_id"])
        manifest_generation = self._create_or_replay(
            manifest_name, _canonical_bytes(manifest)
        )
        result_generation = self._create_or_replay(
            result_name, _canonical_bytes(result)
        )
        return PublishedResultBundle(
            job_id=job["job_id"],
            manifest_name=manifest_name,
            manifest_generation=manifest_generation,
            result_name=result_name,
            result_generation=result_generation,
        )

    def read(self, name: str) -> StoredObject:
        """Read and decode one canonical JSON object at its observed generation."""
        generation, data = self._read_bytes(name)
        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JobResultError("immutable GCS object is not valid JSON") from exc
        if not isinstance(payload, dict) or _canonical_bytes(payload) != data:
            raise JobResultError("immutable GCS object is not canonical JSON")
        assert_secret_free(payload)
        return StoredObject(name=name, generation=generation, payload=payload)

    def read_bundle(
        self,
        job: Mapping[str, Any],
    ) -> tuple[StoredObject, StoredObject]:
        """Read and revalidate the fixed manifest/result pair for one job."""
        validate_job_identity(job)
        manifest = self.read(manifest_object_name(job["job_id"]))
        result = self.read(result_object_name(job["job_id"]))
        validate_result_bundle(job, manifest.payload, result.payload)
        return manifest, result
