"""Atomic bounded records for the uninstalled P4.L3 local service."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


MAX_RECORD_FILE_BYTES = 262_144
MAX_STORED_RUN_RECORDS = 256
RUN_RECORD_KEYS = frozenset(
    {
        "status",
        "code",
        "scheduled_for",
        "observed_at",
        "selection_count",
        "health_ready",
    }
)
# Optional, present only on failed runs written by newer service revisions;
# absent from legacy records, so it is never required.
OPTIONAL_RUN_RECORD_KEYS = frozenset({"failure_category"})
RUN_STATUS_CODES = {
    "completed": frozenset({"completed", "no_due_work"}),
    "blocked": frozenset(
        {"health_failed", "effect_unconfigured", "invalid_effect_outcome"}
    ),
    "failed": frozenset({"effect_failed", "invalid_effect_outcome"}),
}


class ServiceRecordError(ValueError):
    """Raised when bounded service records cannot be trusted or retained."""


def _safe_directory(path: Path, *, create: bool) -> None:
    if create and not path.exists():
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise ServiceRecordError("service record directory is unavailable") from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ServiceRecordError("service record directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        or not os.access(path, os.R_OK | os.W_OK | os.X_OK)
    ):
        raise ServiceRecordError("service record directory is unsafe")


def _safe_target(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ServiceRecordError("service record target is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise ServiceRecordError("service record target is unsafe")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_RECORD_FILE_BYTES:
        raise ServiceRecordError("service record exceeds its byte bound")
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as file_obj:
            descriptor = -1
            file_obj.write(encoded)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise ServiceRecordError("service record write failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _validate_run_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict) \
            or not RUN_RECORD_KEYS <= set(record) \
            or set(record) - RUN_RECORD_KEYS - OPTIONAL_RUN_RECORD_KEYS:
        raise ServiceRecordError("stored service run record is invalid")
    category = record.get("failure_category")
    if category is not None and (
        not isinstance(category, str)
        or not 1 <= len(category) <= 200
        or any(not 32 <= ord(character) < 127 for character in category)
    ):
        raise ServiceRecordError("stored service run record is invalid")
    if not all(
        isinstance(record[field], str)
        for field in ("status", "code", "scheduled_for", "observed_at")
    ):
        raise ServiceRecordError("stored service run record is invalid")
    if (
        record["status"] not in RUN_STATUS_CODES
        or record["code"] not in RUN_STATUS_CODES[record["status"]]
    ):
        raise ServiceRecordError("stored service run record is invalid")
    parsed_times = []
    for field in ("scheduled_for", "observed_at"):
        try:
            parsed = datetime.fromisoformat(record[field].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ServiceRecordError("stored service run record is invalid") from exc
        if (
            parsed.tzinfo is None
            or parsed.utcoffset() is None
            or parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            != record[field]
        ):
            raise ServiceRecordError("stored service run record is invalid")
        parsed_times.append(parsed.astimezone(timezone.utc))
    if parsed_times[0] > parsed_times[1]:
        raise ServiceRecordError("stored service run record is invalid")
    selection_count = record["selection_count"]
    if (
        isinstance(selection_count, bool)
        or not isinstance(selection_count, int)
        or not 0 <= selection_count <= 100
        or not isinstance(record["health_ready"], bool)
    ):
        raise ServiceRecordError("stored service run record is invalid")
    return dict(record)


def read_service_run_records(
    path: Path, *, limit: int = 3
) -> tuple[dict[str, object], ...]:
    """Read a bounded tail of service records without preparing a writer."""
    if isinstance(limit, bool) or not isinstance(limit, int) \
            or not 1 <= limit <= MAX_STORED_RUN_RECORDS:
        raise ServiceRecordError("service record read limit is invalid")
    target = Path(path)
    _safe_target(target)
    try:
        with target.open("rb") as file_obj:
            raw = file_obj.read(MAX_RECORD_FILE_BYTES + 1)
    except OSError as exc:
        raise ServiceRecordError("stored service records are unavailable") from exc
    if len(raw) > MAX_RECORD_FILE_BYTES:
        raise ServiceRecordError("stored service records exceed the byte bound")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceRecordError("stored service records are invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "records"}
        or document.get("schema_version") != 1
        or not isinstance(document.get("records"), list)
        or len(document["records"]) > MAX_STORED_RUN_RECORDS
    ):
        raise ServiceRecordError("stored service records are invalid")
    records = tuple(_validate_run_record(item) for item in document["records"])
    return records[-limit:]


class BoundedServiceRecords:
    """Retain one health snapshot and a fixed number of closed run records."""

    def __init__(
        self,
        *,
        service_root: Path,
        health_path: Path,
        run_records_path: Path,
        record_limit: int,
    ) -> None:
        self._service_root = service_root
        self._health_path = health_path
        self._run_records_path = run_records_path
        self._record_limit = record_limit
        self._records: list[dict[str, object]] | None = None

    def prepare(self, health: Mapping[str, object]) -> None:
        _safe_directory(self._service_root.parent, create=False)
        _safe_directory(self._service_root, create=True)
        _safe_target(self._health_path)
        _safe_target(self._run_records_path)
        self._records = self._read_records()
        _atomic_json(self._health_path, dict(health))

    def append(self, record: Mapping[str, object]) -> None:
        if self._records is None:
            raise ServiceRecordError("service records were not prepared")
        validated = _validate_run_record(dict(record))
        retained = [*self._records, validated][-self._record_limit :]
        _atomic_json(
            self._run_records_path,
            {"schema_version": 1, "records": retained},
        )
        self._records = retained

    def _read_records(self) -> list[dict[str, object]]:
        if not self._run_records_path.exists():
            return []
        try:
            with self._run_records_path.open("rb") as file_obj:
                raw = file_obj.read(MAX_RECORD_FILE_BYTES + 1)
        except OSError as exc:
            raise ServiceRecordError("stored service records are unavailable") from exc
        if len(raw) > MAX_RECORD_FILE_BYTES:
            raise ServiceRecordError("stored service records exceed the byte bound")
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceRecordError("stored service records are invalid") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "records"}
            or document["schema_version"] != 1
            or not isinstance(document["records"], list)
            or len(document["records"]) > self._record_limit
        ):
            raise ServiceRecordError("stored service records are invalid")
        return [_validate_run_record(item) for item in document["records"]]
