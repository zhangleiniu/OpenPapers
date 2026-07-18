"""Read-only, deterministic safety checks for host-local runtime upgrades."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Mapping, Sequence


class UpgradeSafetyError(ValueError):
    """Raised when an upgrade artifact or phase cannot be trusted."""


class UpgradeStage(IntEnum):
    PREFLIGHT = 0
    SERVICES_STOPPED = 1
    BACKUP_READY = 2
    RUNTIME_SWAPPED = 3
    BINDINGS_REPLACED = 4
    SMOKE_PASSED = 5
    SERVICES_RESTARTED = 6


@dataclass(frozen=True)
class RuntimeManifest:
    commit: str
    file_count: int
    sha256: str


@dataclass(frozen=True)
class FreshWakeRecord:
    observed_at: str
    scheduled_for: str
    status: str
    code: str
    health_ready: bool
    selection_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at,
            "scheduled_for": self.scheduled_for,
            "status": self.status,
            "code": self.code,
            "health_ready": self.health_ready,
            "selection_count": self.selection_count,
        }


_MANIFEST_FIELDS = {
    "schema_version", "commit", "runtime_file_count", "runtime_sha256",
}
_HEX = frozenset("0123456789abcdef")
_RUN_RECORD_KEYS = {
    "status", "code", "scheduled_for", "observed_at", "selection_count",
    "health_ready",
}
_RUN_STATUS_CODES = {
    "completed": {"completed", "no_due_work"},
    "blocked": {"health_failed", "effect_unconfigured", "invalid_effect_outcome"},
    "failed": {"effect_failed", "invalid_effect_outcome"},
}


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise UpgradeSafetyError(f"{label} is not a regular file")
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpgradeSafetyError(f"{label} is unavailable or invalid") from exc
    if not isinstance(payload, dict):
        raise UpgradeSafetyError(f"{label} must be a JSON object")
    return payload


def load_runtime_manifest(path: Path) -> RuntimeManifest:
    payload = _json_object(Path(path), label="runtime manifest")
    if set(payload) != _MANIFEST_FIELDS \
            or payload.get("schema_version") != 1 \
            or type(payload.get("runtime_file_count")) is not int \
            or payload["runtime_file_count"] < 1 \
            or not isinstance(payload.get("commit"), str) \
            or len(payload["commit"]) != 40 \
            or any(character not in _HEX for character in payload["commit"]) \
            or not isinstance(payload.get("runtime_sha256"), str) \
            or len(payload["runtime_sha256"]) != 64 \
            or any(character not in _HEX for character in payload["runtime_sha256"]):
        raise UpgradeSafetyError("runtime manifest has an invalid contract")
    return RuntimeManifest(
        payload["commit"], payload["runtime_file_count"],
        payload["runtime_sha256"],
    )


def _runtime_inventory(root: Path) -> tuple[tuple[str, str], ...]:
    runtime = Path(root)
    try:
        metadata = runtime.lstat()
    except OSError as exc:
        raise UpgradeSafetyError("runtime root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or runtime.is_symlink():
        raise UpgradeSafetyError("runtime root must be a real directory")
    items: list[tuple[str, str]] = []
    try:
        paths = sorted(runtime.rglob("*"))
    except OSError as exc:
        raise UpgradeSafetyError("runtime inventory is unavailable") from exc
    for path in paths:
        relative = path.relative_to(runtime)
        if path.is_symlink():
            raise UpgradeSafetyError(f"runtime contains a symlink: {relative}")
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            raise UpgradeSafetyError("runtime contains generated Python bytecode")
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise UpgradeSafetyError("runtime inventory changed during audit") from exc
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise UpgradeSafetyError(f"runtime contains a special file: {relative}")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise UpgradeSafetyError("runtime file is unreadable") from exc
        items.append((relative.as_posix(), digest))
    return tuple(items)


def _inventory_sha256(items: Sequence[tuple[str, str]]) -> str:
    canonical = json.dumps(
        list(items), ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_runtime(
    runtime_root: Path,
    manifest_path: Path,
    *,
    expected_commit: str | None = None,
    require_service_readable: bool = False,
) -> RuntimeManifest:
    """Verify exact bytes and optionally non-owner service-role readability."""
    runtime = Path(runtime_root)
    manifest = load_runtime_manifest(Path(manifest_path))
    if expected_commit is not None and manifest.commit != expected_commit:
        raise UpgradeSafetyError("runtime manifest commit is not the candidate commit")
    items = _runtime_inventory(runtime)
    if len(items) != manifest.file_count \
            or _inventory_sha256(items) != manifest.sha256:
        raise UpgradeSafetyError("runtime inventory does not match its manifest")
    if require_service_readable:
        for path in (runtime, *sorted(runtime.rglob("*"))):
            mode = path.lstat().st_mode
            relative = path.relative_to(runtime) if path != runtime else Path(".")
            if stat.S_ISDIR(mode):
                required = stat.S_IROTH | stat.S_IXOTH
                if mode & required != required:
                    raise UpgradeSafetyError(
                        f"service role cannot traverse runtime directory: {relative}"
                    )
            elif stat.S_ISREG(mode) and not mode & stat.S_IROTH:
                raise UpgradeSafetyError(
                    f"service role cannot read runtime file: {relative}"
                )
    return manifest


def _utc(value: str, *, field: str) -> tuple[datetime, str]:
    if not isinstance(value, str):
        raise UpgradeSafetyError(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpgradeSafetyError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UpgradeSafetyError(f"{field} must include a timezone")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise UpgradeSafetyError(f"{field} must be canonical UTC")
    return parsed.astimezone(timezone.utc), canonical


def fresh_bounded_wake(
    records_path: Path,
    *,
    started_at: str,
    checked_at: str | None = None,
) -> FreshWakeRecord:
    """Return a healthy post-start record without relying on history length."""
    payload = _json_object(Path(records_path), label="service run records")
    if set(payload) != {"schema_version", "records"} \
            or payload.get("schema_version") != 1 \
            or not isinstance(payload.get("records"), list) \
            or not 1 <= len(payload["records"]) <= 256:
        raise UpgradeSafetyError("service run records have an invalid contract")
    started, _ = _utc(started_at, field="upgrade start")
    checked = datetime.now(timezone.utc) if checked_at is None else _utc(
        checked_at, field="upgrade check"
    )[0]
    if checked < started:
        raise UpgradeSafetyError("upgrade check precedes upgrade start")
    candidates: list[tuple[datetime, Mapping[str, Any]]] = []
    for record in payload["records"]:
        if not isinstance(record, dict) \
                or not _RUN_RECORD_KEYS <= set(record) \
                or set(record) - _RUN_RECORD_KEYS - {"failure_category"}:
            raise UpgradeSafetyError("service run record is invalid")
        category = record.get("failure_category")
        if category is not None and (
            not isinstance(category, str)
            or not 1 <= len(category) <= 200
            or any(not 32 <= ord(character) < 127 for character in category)
        ):
            raise UpgradeSafetyError("service run record is invalid")
        observed, _ = _utc(record.get("observed_at"), field="record observed_at")
        scheduled, _ = _utc(
            record.get("scheduled_for"), field="record scheduled_for"
        )
        status = record.get("status")
        selection_count = record.get("selection_count")
        if scheduled > observed \
                or not isinstance(status, str) \
                or status not in _RUN_STATUS_CODES \
                or record.get("code") not in _RUN_STATUS_CODES[status] \
                or type(selection_count) is not int \
                or not 0 <= selection_count <= 100 \
                or not isinstance(record.get("health_ready"), bool):
            raise UpgradeSafetyError("service run record is invalid")
        if observed > checked:
            raise UpgradeSafetyError("service run record is from the future")
        if observed >= started:
            candidates.append((observed, record))
    if not candidates:
        raise UpgradeSafetyError("no fresh service run record exists")
    _, latest = max(candidates, key=lambda item: item[0])
    if latest["status"] != "completed" \
            or latest["code"] not in {"completed", "no_due_work"} \
            or latest["health_ready"] is not True:
        raise UpgradeSafetyError("fresh service run did not complete healthily")
    _, observed_at = _utc(latest["observed_at"], field="record observed_at")
    _, scheduled_for = _utc(latest["scheduled_for"], field="record scheduled_for")
    return FreshWakeRecord(
        observed_at, scheduled_for, latest["status"], latest["code"], True,
        latest["selection_count"],
    )


def validate_stage_transition(current: UpgradeStage, target: UpgradeStage) -> None:
    if target != current + 1:
        raise UpgradeSafetyError("upgrade stages must advance exactly once")


def rollback_plan(
    stage: UpgradeStage,
    *,
    backup_exists: bool,
) -> tuple[str, ...]:
    """Return ordered recovery actions valid for the reached phase."""
    if not isinstance(backup_exists, bool):
        raise UpgradeSafetyError("backup existence must be explicit")
    if stage >= UpgradeStage.BACKUP_READY and not backup_exists:
        raise UpgradeSafetyError("reached upgrade stage requires an exact backup")
    actions = ["quarantine_uninstalled_candidates"]
    if stage >= UpgradeStage.SERVICES_STOPPED:
        actions.insert(0, "stop_candidate_services")
    if stage >= UpgradeStage.BINDINGS_REPLACED:
        actions.append("restore_private_bindings")
    if stage >= UpgradeStage.BACKUP_READY:
        actions.append("restore_state_and_records")
    if stage >= UpgradeStage.RUNTIME_SWAPPED:
        actions.append("restore_runtime_and_source")
    if stage >= UpgradeStage.SERVICES_STOPPED:
        actions.append("restart_original_services")
    return tuple(actions)


def _stage(value: str) -> UpgradeStage:
    try:
        return UpgradeStage[value.upper().replace("-", "_")]
    except KeyError as exc:
        raise argparse.ArgumentTypeError("unknown upgrade stage") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    candidate = commands.add_parser("candidate")
    candidate.add_argument("--runtime", type=Path, required=True)
    candidate.add_argument("--manifest", type=Path, required=True)
    candidate.add_argument("--expected-commit", required=True)
    staged = commands.add_parser("staged-runtime")
    staged.add_argument("--runtime", type=Path, required=True)
    staged.add_argument("--manifest", type=Path, required=True)
    fresh = commands.add_parser("fresh-record")
    fresh.add_argument("--records", type=Path, required=True)
    fresh.add_argument("--started-at", required=True)
    rollback = commands.add_parser("rollback-plan")
    rollback.add_argument("--stage", type=_stage, required=True)
    rollback.add_argument("--backup-exists", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "candidate":
            manifest = verify_runtime(
                args.runtime, args.manifest, expected_commit=args.expected_commit,
            )
            payload: dict[str, object] = {
                "status": "ok", "commit": manifest.commit[:12],
                "runtime_file_count": manifest.file_count,
            }
        elif args.command == "staged-runtime":
            manifest = verify_runtime(
                args.runtime, args.manifest, require_service_readable=True,
            )
            payload = {
                "status": "ok", "service_readable": True,
                "runtime_file_count": manifest.file_count,
            }
        elif args.command == "fresh-record":
            payload = {"status": "ok", "record": fresh_bounded_wake(
                args.records, started_at=args.started_at,
            ).as_dict()}
        else:
            payload = {"status": "ok", "actions": list(rollback_plan(
                args.stage, backup_exists=args.backup_exists,
            ))}
    except UpgradeSafetyError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
