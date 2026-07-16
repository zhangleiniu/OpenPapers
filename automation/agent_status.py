"""Produce bounded, secret-free read-only production status evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import quote

from automation.agent_activation import (
    AgentActivationError,
    probe_local_service_loaded,
    read_cloud_drain_proof,
)
from automation.agent_credentials import (
    AgentCredentialError,
    validate_agent_credential_context,
)
from automation.control_state import CONTROL_SCHEMA_VERSION
from automation.control_state_migration import (
    ControlStateMigrationError,
    audit_control_state,
)
from automation.domain import SecretBoundaryError, Writer, assert_secret_free
from automation.local_service.agent_control import (
    validate_agent_production_root,
    validate_agent_source,
)
from automation.local_service.production import ProductionControlError
from automation.local_service.records import (
    ServiceRecordError,
    read_service_run_records,
)
from automation.resend_notifications import recipient_fingerprints


_CANARY_NAMES = ("codex_installed", "icml_2026")
_PROOF_MAX_AGE_SECONDS = 15 * 60
_PROOF_MAX_FUTURE_SKEW_SECONDS = 60


class AgentStatusError(ValueError):
    """Raised when read-only production evidence is missing or unsafe."""


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AgentStatusError("status clock is invalid")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _private_json(path: Path, *, maximum: int = 65_536) -> Mapping[str, object]:
    target = Path(path)
    try:
        metadata = target.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or target.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or not 2 <= metadata.st_size <= maximum
        ):
            raise AgentStatusError("private status evidence is unsafe")
        payload = json.loads(target.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentStatusError("private status evidence is unavailable") from exc
    if not isinstance(payload, dict):
        raise AgentStatusError("private status evidence is invalid")
    return payload


def _parse_fresh_timestamp(
    value: object, *, clock: Callable[[], datetime]
) -> str:
    if not isinstance(value, str):
        raise AgentStatusError("status proof timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        parsed = parsed.astimezone(timezone.utc)
        now = clock().astimezone(timezone.utc)
    except (ValueError, AttributeError) as exc:
        raise AgentStatusError("status proof timestamp is invalid") from exc
    age = (now - parsed).total_seconds()
    if not -_PROOF_MAX_FUTURE_SKEW_SECONDS <= age <= _PROOF_MAX_AGE_SECONDS:
        raise AgentStatusError("status proof is stale")
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise AgentStatusError("status proof timestamp is invalid")
    return canonical


def _git(path: Path, *arguments: str, binary: bool = False):
    command = (
        "git", "-c", f"safe.directory={path}", "-C", str(path), *arguments
    )
    try:
        completed = subprocess.run(
            command,
            text=not binary,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentStatusError("canary Git state is unavailable") from exc
    if completed.returncode != 0:
        raise AgentStatusError("canary Git state is invalid")
    return completed.stdout


def create_canary_proof(
    baseline_path: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Compare two retained worktrees without emitting their paths or status."""
    payload = _private_json(baseline_path)
    if set(payload) != {"schema_version", "canaries"} \
            or payload.get("schema_version") != 1 \
            or not isinstance(payload.get("canaries"), list) \
            or len(payload["canaries"]) != 2:
        raise AgentStatusError("canary baseline is invalid")
    results: list[dict[str, object]] = []
    names: list[str] = []
    for item in payload["canaries"]:
        if not isinstance(item, dict) or set(item) != {
            "name", "path", "head", "branch", "status_sha256", "remote_count"
        }:
            raise AgentStatusError("canary baseline is invalid")
        name, raw_path = item["name"], item["path"]
        if name not in _CANARY_NAMES or not isinstance(raw_path, str):
            raise AgentStatusError("canary baseline is invalid")
        path = Path(raw_path)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise AgentStatusError("canary baseline is invalid")
        head = _git(path, "rev-parse", "HEAD").strip()
        branch = _git(path, "symbolic-ref", "--short", "HEAD").strip()
        status = _git(path, "status", "--porcelain=v1", "-z", binary=True)
        remotes = tuple(filter(None, _git(path, "remote").splitlines()))
        expected_head = item["head"]
        expected_status = item["status_sha256"]
        expected_remotes = item["remote_count"]
        if not (
            isinstance(expected_head, str)
            and len(expected_head) == 40
            and all(character in "0123456789abcdef" for character in expected_head)
            and isinstance(item["branch"], str)
            and item["branch"]
            and isinstance(expected_status, str)
            and len(expected_status) == 64
            and all(character in "0123456789abcdef" for character in expected_status)
            and type(expected_remotes) is int
            and 0 <= expected_remotes <= 10
        ):
            raise AgentStatusError("canary baseline is invalid")
        checks = {
            "head_matches": head == expected_head,
            "branch_matches": branch == item["branch"],
            "status_matches": hashlib.sha256(status).hexdigest() == expected_status,
            "remote_count_matches": len(remotes) == expected_remotes,
        }
        results.append({"name": name, **checks, "drifted": not all(checks.values())})
        names.append(name)
    if tuple(sorted(names)) != _CANARY_NAMES:
        raise AgentStatusError("canary baseline is invalid")
    return {
        "schema_version": 1,
        "checked_at": _utc_text(clock()),
        "canaries": sorted(results, key=lambda item: item["name"]),
    }


def read_canary_proof(
    path: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[str, list[dict[str, object]]]:
    """Validate one fresh address-free proof for the two retained canaries."""
    payload = _private_json(path)
    if set(payload) != {"schema_version", "checked_at", "canaries"} \
            or payload.get("schema_version") != 1 \
            or not isinstance(payload.get("canaries"), list) \
            or len(payload["canaries"]) != 2:
        raise AgentStatusError("canary proof is invalid")
    checked_at = _parse_fresh_timestamp(payload["checked_at"], clock=clock)
    expected_fields = {
        "name", "head_matches", "branch_matches", "status_matches",
        "remote_count_matches", "drifted",
    }
    resolved: list[dict[str, object]] = []
    for item in payload["canaries"]:
        if not isinstance(item, dict) or set(item) != expected_fields \
                or item.get("name") not in _CANARY_NAMES \
                or any(type(item[field]) is not bool for field in expected_fields - {"name"}):
            raise AgentStatusError("canary proof is invalid")
        matches = all(item[field] for field in (
            "head_matches", "branch_matches", "status_matches",
            "remote_count_matches",
        ))
        if item["drifted"] is matches:
            raise AgentStatusError("canary proof is inconsistent")
        resolved.append(dict(item))
    resolved.sort(key=lambda item: item["name"])
    if tuple(item["name"] for item in resolved) != _CANARY_NAMES:
        raise AgentStatusError("canary proof is invalid")
    return checked_at, resolved


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, object]]:
    return [dict(row) for row in connection.execute(query).fetchall()]


def _validate_safe_summary(payload: Mapping[str, object]) -> None:
    """Reject credential-shaped keys and address/path-shaped string values."""
    try:
        assert_secret_free(payload)
    except SecretBoundaryError as exc:
        raise AgentStatusError("agent status summary is unsafe") from exc

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested)
        elif isinstance(value, str) and (
            len(value) > 256 or any(character in value for character in "@/\\\r\n\x00")
        ):
            raise AgentStatusError("agent status summary is unsafe")

    visit(payload)


def read_agent_state_summary(path: Path) -> list[dict[str, object]]:
    """Read target lifecycle facts while excluding explanations and paths."""
    try:
        with _readonly_connection(path) as connection:
            dates = _rows(connection, "SELECT venue_id, year, status, next_check_at, "
                "estimated_event_date, attempt_count, last_failure_category, updated_at "
                "FROM event_date_schedule ORDER BY venue_id, year")
            agents = _rows(connection, "SELECT venue_id, year, status, next_check_at, "
                "attempt_count, consecutive_failures, last_disposition, last_run_at, "
                "suggested_retry_at, last_gate_reason, updated_at FROM agent_schedule "
                "ORDER BY venue_id, year")
            attempts = _rows(connection, "SELECT venue_id, year, attempt_number, "
                "started_at, completed_at, disposition, suggested_retry_at, "
                "failure_category FROM agent_run_attempt a WHERE attempt_number = "
                "(SELECT MAX(b.attempt_number) FROM agent_run_attempt b "
                "WHERE b.venue_id = a.venue_id AND b.year = a.year) "
                "ORDER BY venue_id, year")
            reports = _rows(connection, "SELECT a.venue_id, a.year, a.attempt_number, "
                "r.status, r.attempt_count, r.delivered_at, r.last_failure_category "
                "FROM agent_run_report r JOIN agent_run_attempt a ON a.run_id = r.run_id "
                "WHERE a.attempt_number = (SELECT MAX(b.attempt_number) "
                "FROM agent_run_attempt b WHERE b.venue_id = a.venue_id "
                "AND b.year = a.year) ORDER BY a.venue_id, a.year")
            artifacts = _rows(connection, "SELECT a.venue_id, a.year, a.attempt_number, "
                "x.lifecycle, x.changed_files_json, x.timed_out, x.retention_status "
                "FROM agent_execution_artifact x JOIN agent_run_attempt a "
                "ON a.run_id = x.run_id WHERE a.attempt_number = "
                "(SELECT MAX(b.attempt_number) FROM agent_run_attempt b "
                "WHERE b.venue_id = a.venue_id AND b.year = a.year) "
                "ORDER BY a.venue_id, a.year")
    except sqlite3.Error as exc:
        raise AgentStatusError("agent status state is unavailable") from exc
    if any(len(rows) > 100 for rows in (dates, agents, attempts, reports, artifacts)):
        raise AgentStatusError("agent status target bound is exceeded")
    targets: dict[tuple[str, int], dict[str, object]] = {}
    for row in dates:
        key = (str(row.pop("venue_id")), int(row.pop("year")))
        targets[key] = {"venue_id": key[0], "year": key[1], "event_date": row,
                        "agent": None, "latest_attempt": None,
                        "latest_report": None, "latest_artifact": None}
    for field, rows in (("agent", agents), ("latest_attempt", attempts),
                        ("latest_report", reports), ("latest_artifact", artifacts)):
        for row in rows:
            key = (str(row.pop("venue_id")), int(row.pop("year")))
            target = targets.setdefault(key, {
                "venue_id": key[0], "year": key[1], "event_date": None,
                "agent": None, "latest_attempt": None,
                "latest_report": None, "latest_artifact": None,
            })
            if field == "latest_artifact":
                raw_changed = row.pop("changed_files_json")
                if raw_changed is None and row.get("lifecycle") == "active":
                    items = []
                else:
                    try:
                        changed = json.loads(str(raw_changed))
                        items = changed.get("items") if isinstance(changed, dict) else None
                    except json.JSONDecodeError as exc:
                        raise AgentStatusError("agent artifact summary is invalid") from exc
                if not isinstance(items, list) or len(items) > 1000:
                    raise AgentStatusError("agent artifact summary is invalid")
                row["changed_file_count"] = len(items)
                row["timed_out"] = bool(row["timed_out"])
            target[field] = row
    return [targets[key] for key in sorted(targets)]


def build_production_status(
    *,
    internal_root: Path,
    repository_root: Path,
    execution_root: Path,
    state_path: Path,
    cloud_proof_path: Path,
    canary_proof_path: Path,
    service_loaded: bool,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
) -> dict[str, object]:
    """Combine installed evidence without constructing an external adapter."""
    configuration, secrets = validate_agent_production_root(
        Path(internal_root), Path(repository_root)
    )
    if secrets is None or recipient_fingerprints(secrets.email_to) \
            != configuration.agent.resend_recipient_sha256s:
        raise AgentStatusError("agent recipient configuration is invalid")
    credentials = validate_agent_credential_context(
        Path(internal_root), require_codex_auth=True, require_google_adc=True
    )
    state = audit_control_state(Path(state_path))
    if not (
        state.schema_version == CONTROL_SCHEMA_VERSION
        and state.current_schema_version == CONTROL_SCHEMA_VERSION
        and state.quick_check_ok
        and state.owner_kind == Writer.LOCAL_CONTROL_PLANE.value
        and state.journal_mode != "wal"
    ):
        raise AgentStatusError("agent control state is not healthy")
    validate_agent_source(
        Path(execution_root).resolve() / "agent-source",
        configuration.agent_source_commit,
    )
    try:
        disk_ready = int(getattr(disk_usage(Path(execution_root)), "free")) \
            >= configuration.agent.minimum_free_bytes
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise AgentStatusError("agent execution disk is unavailable") from exc
    cloud = read_cloud_drain_proof(Path(cloud_proof_path), clock=clock)
    canary_checked_at, canaries = read_canary_proof(
        Path(canary_proof_path), clock=clock
    )
    wakes = read_service_run_records(
        Path(internal_root) / "service" / "runs.v1.json", limit=3
    )
    payload = {
        "status": "ok",
        "observed_at": _utc_text(clock()),
        "production": {
            "external_effects_enabled": configuration.external_effects_enabled,
            "schema_version": state.schema_version,
            "state_quick_check_ok": state.quick_check_ok,
            "idle": not any((state.active_event_date_attempts,
                             state.active_agent_runs, state.in_flight_reports)),
            "codex_auth_present": (credentials.codex_home / "auth.json").is_file(),
            "google_adc_present": credentials.google_adc.is_file(),
            "recipient_count": len(secrets.email_to),
            "agent_source_ready": True,
            "disk_ready": disk_ready,
        },
        "service": {"loaded": service_loaded, "recent_wakes": list(wakes)},
        "cloud": {
            "schedule_paused": cloud.schedule_paused,
            "active_executions": cloud.active_executions,
            "checked_at": cloud.checked_at.isoformat().replace("+00:00", "Z"),
        },
        "targets": read_agent_state_summary(Path(state_path)),
        "canaries": {"checked_at": canary_checked_at, "items": canaries},
    }
    _validate_safe_summary(payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    proof = commands.add_parser("canary-proof")
    proof.add_argument("--baseline", type=Path, required=True)
    report = commands.add_parser("report")
    report.add_argument("--internal-root", type=Path, required=True)
    report.add_argument("--repository-root", type=Path, required=True)
    report.add_argument("--execution-root", type=Path, required=True)
    report.add_argument("--state", type=Path, required=True)
    report.add_argument("--cloud-proof", type=Path, required=True)
    report.add_argument("--canary-proof", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "canary-proof":
            payload = create_canary_proof(args.baseline)
        else:
            payload = build_production_status(
                internal_root=args.internal_root,
                repository_root=args.repository_root,
                execution_root=args.execution_root,
                state_path=args.state,
                cloud_proof_path=args.cloud_proof,
                canary_proof_path=args.canary_proof,
                service_loaded=probe_local_service_loaded(),
            )
    except (
        AgentStatusError, AgentActivationError, AgentCredentialError,
        ControlStateMigrationError, ProductionControlError, ServiceRecordError,
        PermissionError,
    ):
        print(json.dumps({"status": "blocked", "reason": "status_unavailable"}))
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
