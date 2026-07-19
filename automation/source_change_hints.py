"""Persist trusted monitor changes and advance only existing agent schedules."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from automation.control_state import (
    DEFAULT_LEASE_TTL_SECONDS,
    ControlStateRepository,
)
from automation.domain import Writer
from automation.event_dates import EventDateTarget


SOURCE_HINT_OWNER_ID = "source-change-hint"
_MAX_PENDING_SCAN = 20
_MAX_RETAINED_PENDING = 256
_MAX_RETAINED_CLOSED = 256
_VENUE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class SourceChangeHintError(ValueError):
    """Raised when source-change hint evidence or state is unsafe."""


@dataclass(frozen=True)
class SourceChangeHintApplyOutcome:
    applied_count: int
    pending_count: int
    ignored_count: int


def _timestamp(value: datetime, *, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None \
            or value.utcoffset() is None:
        raise SourceChangeHintError(f"{field} is invalid")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise SourceChangeHintError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceChangeHintError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None \
            or _timestamp(parsed, field=field) != value:
        raise SourceChangeHintError(f"{field} is invalid")
    return parsed.astimezone(timezone.utc)


def _hint_id(venue_id: str, year: int, observed_at: str) -> str:
    return hashlib.sha256(json.dumps({
        "schema_version": 1,
        "venue_id": venue_id,
        "year": year,
        "observed_at": observed_at,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate_journal(path: Path) -> Path:
    journal = Path(path)
    try:
        metadata = journal.lstat()
    except OSError as exc:
        raise SourceChangeHintError("production wakeup journal is unavailable") from exc
    # Group read allowed (2026-07-19, matching local_service/production.py);
    # group write and any "other" access remain forbidden.
    if not stat.S_ISREG(metadata.st_mode) or journal.is_symlink() \
            or metadata.st_uid != os.geteuid() \
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO):
        raise SourceChangeHintError("production wakeup journal is unsafe")
    return journal


def prepare_source_change_hint_journal(connection: sqlite3.Connection) -> None:
    """Create the bounded local hint inbox in the existing wakeup journal."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS production_source_hint ("
        "hint_id TEXT PRIMARY KEY, venue_id TEXT NOT NULL, year INTEGER NOT NULL, "
        "observed_at TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN "
        "('pending', 'applied', 'ignored')), decided_at TEXT, CHECK ("
        "(status = 'pending' AND decided_at IS NULL) OR "
        "(status IN ('applied', 'ignored') AND decided_at IS NOT NULL)))"
    )
    columns = {
        str(row[1]) for row in connection.execute(
            "PRAGMA table_info(production_source_hint)"
        )
    }
    if columns != {"hint_id", "venue_id", "year", "observed_at", "status",
                   "decided_at"}:
        raise SourceChangeHintError("source hint journal is invalid")


def record_source_change_hints(
    journal_path: Path,
    events: Sequence[Mapping[str, object]],
    *,
    observed_at: datetime,
) -> int:
    """Record one de-identified pending hint per changed available target."""
    observed = _timestamp(observed_at, field="source hint observation")
    targets: set[tuple[str, int]] = set()
    for event in events:
        venue = event.get("venue")
        year = event.get("year")
        if event.get("changed") is not True or event.get("status") != "available":
            continue
        if not isinstance(venue, str) or not _VENUE_ID.fullmatch(venue) \
                or not isinstance(year, int) or isinstance(year, bool) \
                or not 2020 <= year <= 2200:
            raise SourceChangeHintError("source hint target is invalid")
        targets.add((venue, year))
    if not targets:
        return 0
    journal = _validate_journal(journal_path)
    inserted = 0
    try:
        with sqlite3.connect(journal) as connection:
            prepare_source_change_hint_journal(connection)
            for venue, year in sorted(targets):
                hint_id = _hint_id(venue, year, observed)
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO production_source_hint VALUES "
                    "(?, ?, ?, ?, 'pending', NULL)",
                    (hint_id, venue, year, observed),
                )
                inserted += cursor.rowcount
            pending_count = int(connection.execute(
                "SELECT COUNT(*) FROM production_source_hint "
                "WHERE status = 'pending'"
            ).fetchone()[0])
            if pending_count > _MAX_RETAINED_PENDING:
                raise SourceChangeHintError("source hint pending bound is exceeded")
            connection.execute(
                "DELETE FROM production_source_hint WHERE status != 'pending' "
                "AND hint_id NOT IN (SELECT hint_id FROM production_source_hint "
                "WHERE status != 'pending' ORDER BY decided_at DESC, hint_id DESC "
                "LIMIT ?)",
                (_MAX_RETAINED_CLOSED,),
            )
    except sqlite3.Error as exc:
        raise SourceChangeHintError("source hint journal write failed") from exc
    return inserted


def apply_pending_source_change_hints(
    journal_path: Path,
    state_path: Path,
    targets: Iterable[EventDateTarget],
    *,
    observed_at: datetime,
    minimum_delay: timedelta,
) -> SourceChangeHintApplyOutcome:
    """Advance at most one existing schedule; never claim an agent run."""
    now_text = _timestamp(observed_at, field="source hint apply time")
    if not isinstance(minimum_delay, timedelta) or minimum_delay <= timedelta(0):
        raise SourceChangeHintError("source hint minimum delay is invalid")
    target_set = {(target.venue_id, target.year) for target in targets}
    if not target_set:
        raise SourceChangeHintError("source hint cohort is empty")
    journal = _validate_journal(journal_path)
    try:
        with sqlite3.connect(journal) as connection:
            prepare_source_change_hint_journal(connection)
            rows = connection.execute(
                "SELECT hint_id, venue_id, year, observed_at "
                "FROM production_source_hint WHERE status = 'pending' "
                "ORDER BY observed_at, hint_id LIMIT ?",
                (_MAX_PENDING_SCAN,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise SourceChangeHintError("source hint journal read failed") from exc
    applied = ignored = 0
    pending = 0
    for hint_id, venue, year, hint_observed in rows:
        if not isinstance(hint_id, str) or not re.fullmatch(r"[0-9a-f]{64}", hint_id) \
                or not isinstance(venue, str) or not _VENUE_ID.fullmatch(venue) \
                or not isinstance(year, int) or isinstance(year, bool) \
                or not 2020 <= year <= 2200:
            raise SourceChangeHintError("stored source hint is invalid")
        hint_time = _parse_timestamp(
            hint_observed, field="stored source hint observation"
        )
        if hint_id != _hint_id(venue, year, str(hint_observed)):
            raise SourceChangeHintError("stored source hint identity is invalid")
        key = (venue, year)
        if key not in target_set:
            reason = "unconfigured"
        else:
            with ControlStateRepository(
                Path(state_path), writer=Writer.LOCAL_CONTROL_PLANE,
                clock=lambda: observed_at,
            ) as repository:
                lease = repository.acquire_lease(
                    SOURCE_HINT_OWNER_ID, ttl_seconds=DEFAULT_LEASE_TTL_SECONDS
                )
                try:
                    result = repository.advance_agent_schedule_from_hint(
                        key[0], key[1], hint_observed_at=hint_time,
                        next_check_at=observed_at + minimum_delay,
                        applied_at=observed_at, lease=lease,
                    )
                finally:
                    repository.release_lease(lease)
            if result.reason == "schedule_missing":
                pending += 1
                continue
            reason = "applied" if result.applied else result.reason
        status = "applied" if reason == "applied" else "ignored"
        try:
            with sqlite3.connect(journal) as connection:
                cursor = connection.execute(
                    "UPDATE production_source_hint SET status = ?, decided_at = ? "
                    "WHERE hint_id = ? AND status = 'pending'",
                    (status, now_text, hint_id),
                )
                if cursor.rowcount != 1:
                    raise SourceChangeHintError("source hint claim changed")
        except sqlite3.Error as exc:
            raise SourceChangeHintError("source hint journal update failed") from exc
        if status == "applied":
            applied += 1
            break
        ignored += 1
    return SourceChangeHintApplyOutcome(applied, pending, ignored)
