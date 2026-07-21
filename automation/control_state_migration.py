"""Read-only audit and isolated rehearsal for local control-state migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from automation.control_state import CONTROL_SCHEMA_VERSION, ControlStateRepository
from automation.domain import Writer


_PRESERVED_TABLES = (
    "control_lease",
    "control_ownership",
    "event_date_schedule",
    "event_date_attempt",
    "agent_schedule",
    "agent_run_attempt",
    "agent_execution_artifact",
    "agent_run_report",
    "agent_run_report_attempt",
)


class ControlStateMigrationError(RuntimeError):
    """Raised when audit, backup, or rehearsal cannot prove a safe result."""


@dataclass(frozen=True)
class ControlStateAudit:
    schema_version: int
    current_schema_version: int
    quick_check_ok: bool
    owner_kind: str | None
    journal_mode: str
    active_event_date_attempts: int
    active_agent_runs: int
    active_artifacts: int
    in_flight_reports: int
    active_report_attempts: int
    preserved_counts: tuple[tuple[str, int], ...]
    migration_ready: bool


@dataclass(frozen=True)
class ControlStateRehearsal:
    source_schema_version: int
    migrated_schema_version: int
    backup_path: Path
    source_unchanged: bool
    preserved_counts: tuple[tuple[str, int], ...]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


def _count_where(
    connection: sqlite3.Connection,
    tables: set[str],
    table: str,
    predicate: str,
) -> int:
    if table not in tables:
        return 0
    return int(connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {predicate}"
    ).fetchone()[0])


def audit_control_state(path: Path) -> ControlStateAudit:
    """Inspect bounded schema/lifecycle facts without opening a writer."""
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ControlStateMigrationError("control state is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ControlStateMigrationError("control state is not a regular file")
    try:
        with _readonly_connection(path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            quick_rows = connection.execute("PRAGMA quick_check").fetchall()
            quick_ok = quick_rows == [("ok",)] or (
                len(quick_rows) == 1 and tuple(quick_rows[0]) == ("ok",)
            )
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            tables = _tables(connection)
            owner = None
            if "control_ownership" in tables:
                rows = connection.execute(
                    "SELECT owner_kind FROM control_ownership"
                ).fetchall()
                if len(rows) == 1:
                    owner = str(rows[0][0])
            counts = tuple(
                (table, int(connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]))
                for table in _PRESERVED_TABLES if table in tables
            )
            active_dates = _count_where(
                connection, tables, "event_date_attempt", "outcome = 'active'"
            )
            active_runs = _count_where(
                connection, tables, "agent_run_attempt", "disposition = 'active'"
            )
            active_artifacts = _count_where(
                connection, tables, "agent_execution_artifact", "lifecycle = 'active'"
            )
            in_flight = _count_where(
                connection, tables, "agent_run_report", "status = 'in_flight'"
            )
            active_report_attempts = _count_where(
                connection, tables, "agent_run_report_attempt", "outcome = 'active'"
            )
    except sqlite3.Error as exc:
        raise ControlStateMigrationError("control state audit failed") from exc
    ready = (
        quick_ok
        and owner == Writer.LOCAL_CONTROL_PLANE.value
        and 5 <= version <= CONTROL_SCHEMA_VERSION
        and journal_mode != "wal"
        and not any((
            active_dates, active_runs, active_artifacts, in_flight,
            active_report_attempts,
        ))
    )
    return ControlStateAudit(
        version, CONTROL_SCHEMA_VERSION, quick_ok, owner, journal_mode,
        active_dates, active_runs, active_artifacts, in_flight,
        active_report_attempts, counts, ready,
    )


def create_control_state_backup(source: Path, destination: Path) -> Path:
    """Create one new private SQLite backup, refusing overwrite or symlinks."""
    source = Path(source)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ControlStateMigrationError("control backup destination exists")
    try:
        parent = destination.parent.lstat()
    except OSError as exc:
        raise ControlStateMigrationError("control backup directory is unavailable") from exc
    # Group read/traverse allowed (2026-07-19, matching
    # local_service/production.py); group write and any "other" access
    # remain forbidden.
    if not stat.S_ISDIR(parent.st_mode) or destination.parent.is_symlink() \
            or parent.st_uid != os.geteuid() \
            or parent.st_mode & (stat.S_IWGRP | stat.S_IRWXO):
        raise ControlStateMigrationError("control backup directory is unsafe")
    audit = audit_control_state(source)
    if not audit.quick_check_ok:
        raise ControlStateMigrationError("control source integrity check failed")
    try:
        with sqlite3.connect(
            f"file:{quote(str(source.resolve()))}?mode=ro", uri=True
        ) as source_connection:
            with sqlite3.connect(destination) as destination_connection:
                source_connection.backup(destination_connection)
                if destination_connection.execute(
                    "PRAGMA quick_check"
                ).fetchone() != ("ok",):
                    raise ControlStateMigrationError(
                        "control backup integrity check failed"
                    )
        destination.chmod(0o600)
    except (OSError, sqlite3.Error, ControlStateMigrationError) as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, ControlStateMigrationError):
            raise
        raise ControlStateMigrationError("control backup failed") from exc
    return destination


def rehearse_control_state_migration(
    source: Path,
    rehearsal_root: Path,
    *,
    clock: Callable[[], datetime],
) -> ControlStateRehearsal:
    """Back up and migrate only an isolated copy, proving source invariance."""
    source = Path(source)
    rehearsal_root = Path(rehearsal_root)
    audit = audit_control_state(source)
    if not audit.migration_ready:
        raise ControlStateMigrationError("control state is not migration-ready")
    before_hash = _file_sha256(source)
    destination = rehearsal_root / "control-state-rehearsal.sqlite3"
    create_control_state_backup(source, destination)
    with ControlStateRepository(
        destination, writer=Writer.LOCAL_CONTROL_PLANE, clock=clock
    ) as repository:
        if repository.schema_version != CONTROL_SCHEMA_VERSION:
            raise ControlStateMigrationError("control rehearsal did not migrate")
    migrated = audit_control_state(destination)
    after_hash = _file_sha256(source)
    before_counts = dict(audit.preserved_counts)
    after_counts = dict(migrated.preserved_counts)
    for table, count in before_counts.items():
        if after_counts.get(table) != count:
            raise ControlStateMigrationError("control rehearsal changed retained rows")
    if before_hash != after_hash:
        raise ControlStateMigrationError("control rehearsal changed source state")
    return ControlStateRehearsal(
        audit.schema_version,
        migrated.schema_version,
        destination,
        True,
        migrated.preserved_counts,
    )


def _audit_payload(audit: ControlStateAudit) -> dict[str, object]:
    return {
        "schema_version": audit.schema_version,
        "current_schema_version": audit.current_schema_version,
        "quick_check_ok": audit.quick_check_ok,
        "owner_kind": audit.owner_kind,
        "journal_mode": audit.journal_mode,
        "active_event_date_attempts": audit.active_event_date_attempts,
        "active_agent_runs": audit.active_agent_runs,
        "active_artifacts": audit.active_artifacts,
        "in_flight_reports": audit.in_flight_reports,
        "active_report_attempts": audit.active_report_attempts,
        "preserved_counts": dict(audit.preserved_counts),
        "migration_ready": audit.migration_ready,
    }


def main(argv: list[str] | None = None) -> int:
    """Run an explicit safe-summary audit or isolated rehearsal command."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--state", type=Path, required=True)
    rehearsal_parser = subparsers.add_parser("rehearse")
    rehearsal_parser.add_argument("--state", type=Path, required=True)
    rehearsal_parser.add_argument("--rehearsal-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            payload = {"status": "ok", "audit": _audit_payload(
                audit_control_state(args.state)
            )}
        else:
            result = rehearse_control_state_migration(
                args.state,
                args.rehearsal_root,
                clock=lambda: datetime.now().astimezone(),
            )
            payload = {
                "status": "ok",
                "source_schema_version": result.source_schema_version,
                "migrated_schema_version": result.migrated_schema_version,
                "source_unchanged": result.source_unchanged,
                "preserved_counts": dict(result.preserved_counts),
            }
    except (ControlStateMigrationError, PermissionError):
        print(json.dumps({"status": "blocked", "reason": "state_unavailable"}))
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
