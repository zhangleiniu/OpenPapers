"""Produce bounded, secret-free read-only production status evidence."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import quote

from automation.agent_activation import (
    AgentActivationError,
    probe_local_service_loaded,
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


class AgentStatusError(ValueError):
    """Raised when read-only production evidence is missing or unsafe."""


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AgentStatusError("status clock is invalid")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
        "targets": read_agent_state_summary(Path(state_path)),
    }
    _validate_safe_summary(payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report")
    report.add_argument("--internal-root", type=Path, required=True)
    report.add_argument("--repository-root", type=Path, required=True)
    report.add_argument("--execution-root", type=Path, required=True)
    report.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = build_production_status(
            internal_root=args.internal_root,
            repository_root=args.repository_root,
            execution_root=args.execution_root,
            state_path=args.state,
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
