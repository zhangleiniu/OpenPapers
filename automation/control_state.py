"""Durable single-writer storage for the local automation control plane."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from automation.cases import (
    CaseControlRequest,
    CaseObservation,
    case_event_payload,
    control_case,
    observe_case,
    validate_case_event_payload,
    validate_case_state,
)
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
from automation.verification import validate_verification_result


CONTROL_SCHEMA_VERSION = 2
DEFAULT_LEASE_TTL_SECONDS = 300
MAX_LEASE_TTL_SECONDS = 86_400
_CONTROL_LEASE_NAME = "control-state"


class ControlStateError(RuntimeError):
    """Base class for durable control-state failures."""


class SchemaMigrationError(ControlStateError):
    """Raised when the database cannot be migrated or validated safely."""


class LeaseConflictError(ControlStateError):
    """Raised when another unexpired control-plane lease exists."""


class LeaseLostError(ControlStateError):
    """Raised when a lease token is missing, expired, or replaced."""


class VerificationReplayConflictError(ControlStateError):
    """Raised when a stored verification identity changes meaning."""


class StateRevisionConflictError(ControlStateError):
    """Raised when a state write is based on a stale revision."""


class CaseEventConflictError(ControlStateError):
    """Raised when a stable case event ID changes meaning."""


class StoredDataError(ControlStateError):
    """Raised when persisted JSON, identity, or fingerprints are corrupt."""


@dataclass(frozen=True)
class LeaseHandle:
    """Opaque authority for one unexpired control-plane writer."""

    owner_id: str
    token: str
    expires_at: str


@dataclass(frozen=True)
class VerificationRecord:
    """One replayable verification and the inputs that prove its semantics."""

    sequence: int
    received_at: str
    discovery: dict[str, Any]
    request: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class StateRevision:
    """One immutable conference-state snapshot."""

    venue_id: str
    year: int
    revision: int
    stored_at: str
    state_fingerprint: str
    state: dict[str, Any]


@dataclass(frozen=True)
class StateWriteOutcome:
    """Result of an optimistic conference-state write."""

    record: StateRevision
    applied: bool


@dataclass(frozen=True)
class CaseRevision:
    """One immutable unresolved-case state snapshot."""

    case_id: str
    venue_id: str
    year: int
    blocker: str
    revision: int
    stored_at: str
    state_fingerprint: str
    state: dict[str, Any]


@dataclass(frozen=True)
class CaseEventRecord:
    """One immutable observation or human-control event."""

    sequence: int
    event_id: str
    case_id: str
    event_kind: str
    event_at: str
    event_fingerprint: str
    previous_revision: int
    resulting_revision: int
    revision_applied: bool
    meaningful_change: bool
    reactivated: bool
    event: dict[str, Any]


@dataclass(frozen=True)
class CaseWriteOutcome:
    """Result of accepting one durable case event."""

    record: CaseRevision
    event: CaseEventRecord
    applied: bool
    replayed: bool


_MIGRATION_1 = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE control_lease (
        lease_name TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        lease_token TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE verification_history (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        verification_id TEXT NOT NULL UNIQUE,
        evidence_fingerprint TEXT NOT NULL,
        discovery_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        schema_version INTEGER NOT NULL,
        received_at TEXT NOT NULL,
        discovery_fingerprint TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL,
        result_fingerprint TEXT NOT NULL,
        discovery_json TEXT NOT NULL,
        request_json TEXT NOT NULL,
        result_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX verification_history_venue_year_sequence
    ON verification_history (venue_id, year, sequence)
    """,
    """
    CREATE TABLE conference_state_history (
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        state_fingerprint TEXT NOT NULL,
        stored_at TEXT NOT NULL,
        state_json TEXT NOT NULL,
        PRIMARY KEY (venue_id, year, revision)
    )
    """,
    """
    CREATE TABLE conference_state_current (
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        state_fingerprint TEXT NOT NULL,
        stored_at TEXT NOT NULL,
        state_json TEXT NOT NULL,
        PRIMARY KEY (venue_id, year),
        FOREIGN KEY (venue_id, year, revision)
            REFERENCES conference_state_history (venue_id, year, revision)
    )
    """,
)

_MIGRATION_2 = (
    """
    CREATE TABLE case_state_history (
        case_id TEXT NOT NULL,
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        blocker TEXT NOT NULL,
        status TEXT NOT NULL,
        revision INTEGER NOT NULL,
        state_fingerprint TEXT NOT NULL,
        stored_at TEXT NOT NULL,
        state_json TEXT NOT NULL,
        PRIMARY KEY (case_id, revision)
    )
    """,
    """
    CREATE TABLE case_state_current (
        case_id TEXT PRIMARY KEY,
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        blocker TEXT NOT NULL,
        status TEXT NOT NULL,
        revision INTEGER NOT NULL,
        state_fingerprint TEXT NOT NULL,
        stored_at TEXT NOT NULL,
        state_json TEXT NOT NULL,
        UNIQUE (venue_id, year, blocker),
        FOREIGN KEY (case_id, revision)
            REFERENCES case_state_history (case_id, revision)
    )
    """,
    """
    CREATE INDEX case_state_current_status_identity
    ON case_state_current (status, venue_id, year, blocker)
    """,
    """
    CREATE TABLE case_event_history (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        case_id TEXT NOT NULL,
        event_kind TEXT NOT NULL,
        event_at TEXT NOT NULL,
        event_fingerprint TEXT NOT NULL,
        previous_revision INTEGER NOT NULL,
        resulting_revision INTEGER NOT NULL,
        revision_applied INTEGER NOT NULL,
        meaningful_change INTEGER NOT NULL,
        reactivated INTEGER NOT NULL,
        event_json TEXT NOT NULL,
        FOREIGN KEY (case_id, resulting_revision)
            REFERENCES case_state_history (case_id, revision)
    )
    """,
    """
    CREATE INDEX case_event_history_case_sequence
    ON case_event_history (case_id, sequence)
    """,
)

_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
}

_REQUIRED_COLUMNS_V1 = {
    "schema_migrations": {"version", "applied_at"},
    "control_lease": {
        "lease_name", "owner_id", "lease_token", "acquired_at", "expires_at",
    },
    "verification_history": {
        "sequence", "verification_id", "evidence_fingerprint", "discovery_id",
        "request_id", "venue_id", "year", "schema_version", "received_at",
        "discovery_fingerprint", "request_fingerprint", "result_fingerprint",
        "discovery_json", "request_json", "result_json",
    },
    "conference_state_history": {
        "venue_id", "year", "revision", "state_fingerprint", "stored_at",
        "state_json",
    },
    "conference_state_current": {
        "venue_id", "year", "revision", "state_fingerprint", "stored_at",
        "state_json",
    },
}

_REQUIRED_COLUMNS_V2 = {
    "case_state_history": {
        "case_id", "venue_id", "year", "blocker", "status", "revision",
        "state_fingerprint", "stored_at", "state_json",
    },
    "case_state_current": {
        "case_id", "venue_id", "year", "blocker", "status", "revision",
        "state_fingerprint", "stored_at", "state_json",
    },
    "case_event_history": {
        "sequence", "event_id", "case_id", "event_kind", "event_at",
        "event_fingerprint", "previous_revision", "resulting_revision",
        "revision_applied", "meaningful_change", "reactivated", "event_json",
    },
}

_REQUIRED_COLUMNS_BY_VERSION = {
    1: _REQUIRED_COLUMNS_V1,
    2: _REQUIRED_COLUMNS_V2,
}


def _canonical_json(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ControlStateError(f"artifact is not canonical JSON: {exc}") from exc


def _parse_timestamp(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ControlStateError(f"{field} must be a valid datetime") from exc
    else:
        raise ControlStateError(f"{field} must be a datetime or string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ControlStateError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime | str, *, field: str) -> str:
    return _parse_timestamp(value, field=field).isoformat().replace("+00:00", "Z")


def _validate_ttl(ttl_seconds: int) -> None:
    if (
        not isinstance(ttl_seconds, int)
        or isinstance(ttl_seconds, bool)
        or not 1 <= ttl_seconds <= MAX_LEASE_TTL_SECONDS
    ):
        raise ControlStateError(
            f"lease TTL must be between 1 and {MAX_LEASE_TTL_SECONDS} seconds"
        )


def _validate_owner(owner_id: str) -> None:
    if not isinstance(owner_id, str) or not 3 <= len(owner_id) <= 128:
        raise ControlStateError("lease owner ID must contain 3 to 128 characters")


def _decode_json(raw: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StoredDataError(f"stored {label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise StoredDataError(f"stored {label} must be a JSON object")
    if _canonical_json(payload) != raw:
        raise StoredDataError(f"stored {label} is not canonical JSON")
    return payload


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


class ControlStateRepository:
    """Versioned SQLite repository for the sole mutable cloud writer."""

    def __init__(
        self,
        path: Path,
        *,
        writer: Writer | str = Writer.CLOUD_CONTROL_PLANE,
        clock: Callable[[], datetime] = _system_clock,
    ) -> None:
        assert_writer_allowed(writer, ArtifactKind.CONTROL_STATE)
        self.path = Path(path)
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=5.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        try:
            self._migrate()
            self._validate_schema()
        except Exception:
            self._connection.close()
            raise

    def __enter__(self) -> ControlStateRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self._connection.close()

    def _now(self) -> datetime:
        return _parse_timestamp(self._clock(), field="repository clock")

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield self._connection
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise ControlStateError("SQLite write transaction failed") from exc
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _user_tables(self) -> set[str]:
        rows = self._connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return {str(row[0]) for row in rows}

    def _migrate(self) -> None:
        version = self.schema_version
        if version > CONTROL_SCHEMA_VERSION:
            raise SchemaMigrationError(
                f"control schema version {version} is newer than supported "
                f"version {CONTROL_SCHEMA_VERSION}"
            )
        if version == 0 and self._user_tables():
            raise SchemaMigrationError(
                "refusing to migrate a populated unversioned database"
            )
        if version > 0:
            self._validate_schema(expected_version=version)
        for target_version in range(version + 1, CONTROL_SCHEMA_VERSION + 1):
            applied_at = _timestamp(self._now(), field="applied_at")
            try:
                with self._write_transaction() as connection:
                    for statement in _MIGRATIONS[target_version]:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, applied_at) "
                        "VALUES (?, ?)",
                        (target_version, applied_at),
                    )
                    connection.execute(
                        f"PRAGMA user_version = {target_version}"
                    )
            except ControlStateError as exc:
                raise SchemaMigrationError(
                    f"control schema migration {target_version} failed: {exc}"
                ) from exc

    def _validate_schema(self, *, expected_version: int | None = None) -> None:
        version = CONTROL_SCHEMA_VERSION if expected_version is None else expected_version
        if version not in _MIGRATIONS:
            raise SchemaMigrationError(f"unsupported control schema version {version}")
        if self.schema_version != version:
            raise SchemaMigrationError(
                f"control schema version does not match expected version {version}"
            )
        tables = self._user_tables()
        required_columns: dict[str, set[str]] = {}
        for migration_version in range(1, version + 1):
            required_columns.update(_REQUIRED_COLUMNS_BY_VERSION[migration_version])
        missing_tables = set(required_columns) - tables
        if missing_tables:
            raise SchemaMigrationError(
                f"control schema is missing tables: {sorted(missing_tables)}"
            )
        for table, required in required_columns.items():
            columns = {
                str(row[1])
                for row in self._connection.execute(f"PRAGMA table_info({table})")
            }
            missing = required - columns
            if missing:
                raise SchemaMigrationError(
                    f"control table {table} is missing columns: {sorted(missing)}"
                )
        versions = [
            int(row[0])
            for row in self._connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        if versions != list(range(1, version + 1)):
            raise SchemaMigrationError("control schema migration history is invalid")
        integrity = self._connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise SchemaMigrationError(
                f"control database integrity check failed: {integrity}"
            )
        if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SchemaMigrationError("control database foreign-key check failed")

    def acquire_lease(
        self,
        owner_id: str,
        *,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> LeaseHandle:
        """Acquire the singleton lease, replacing it only after expiration."""
        _validate_owner(owner_id)
        _validate_ttl(ttl_seconds)
        acquired = self._now()
        acquired_at = _timestamp(acquired, field="lease acquisition time")
        expires_at = _timestamp(
            acquired + timedelta(seconds=ttl_seconds),
            field="lease expiry",
        )
        token = uuid.uuid4().hex
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT owner_id, expires_at FROM control_lease WHERE lease_name = ?",
                (_CONTROL_LEASE_NAME,),
            ).fetchone()
            if row is not None and _parse_timestamp(
                row["expires_at"], field="stored lease expiry"
            ) > acquired:
                raise LeaseConflictError(
                    f"control lease is held by {row['owner_id']!r} until "
                    f"{row['expires_at']}"
                )
            connection.execute(
                """
                INSERT INTO control_lease
                    (lease_name, owner_id, lease_token, acquired_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    lease_token = excluded.lease_token,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                """,
                (_CONTROL_LEASE_NAME, owner_id, token, acquired_at, expires_at),
            )
        return LeaseHandle(owner_id, token, expires_at)

    def renew_lease(
        self,
        lease: LeaseHandle,
        *,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> LeaseHandle:
        """Extend a live lease without changing its opaque token."""
        _validate_ttl(ttl_seconds)
        renewed = self._now()
        expires_at = _timestamp(
            renewed + timedelta(seconds=ttl_seconds),
            field="lease expiry",
        )
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, renewed)
            connection.execute(
                "UPDATE control_lease SET expires_at = ? "
                "WHERE lease_name = ? AND lease_token = ?",
                (expires_at, _CONTROL_LEASE_NAME, lease.token),
            )
        return LeaseHandle(lease.owner_id, lease.token, expires_at)

    def release_lease(self, lease: LeaseHandle) -> None:
        """Release the lease only when its owner and token still match."""
        with self._write_transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM control_lease "
                "WHERE lease_name = ? AND owner_id = ? AND lease_token = ?",
                (_CONTROL_LEASE_NAME, lease.owner_id, lease.token),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError("control lease token is no longer current")

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        lease: LeaseHandle,
        now: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT owner_id, lease_token, expires_at FROM control_lease "
            "WHERE lease_name = ?",
            (_CONTROL_LEASE_NAME,),
        ).fetchone()
        if (
            row is None
            or row["owner_id"] != lease.owner_id
            or row["lease_token"] != lease.token
        ):
            raise LeaseLostError("control lease token is missing or replaced")
        if _parse_timestamp(
            row["expires_at"], field="stored lease expiry"
        ) <= now:
            raise LeaseLostError("control lease has expired")

    def accept_verification(
        self,
        discovery: Mapping[str, Any],
        request: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        lease: LeaseHandle,
        received_at: datetime | str,
    ) -> bool:
        """Persist one strict bundle; return false for a semantic replay."""
        assert_secret_free(discovery)
        assert_secret_free(request)
        assert_secret_free(result)
        validate_verification_result(result, request, discovery)
        received = _parse_timestamp(received_at, field="verification received_at")
        received_text = _timestamp(received, field="verification received_at")
        discovery_json = _canonical_json(discovery)
        request_json = _canonical_json(request)
        result_json = _canonical_json(result)
        identities = (
            result["evidence_fingerprint"],
            result["discovery_id"],
            result["request_id"],
            result["venue_id"],
            result["year"],
            result["schema_version"],
        )
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            existing = connection.execute(
                "SELECT * FROM verification_history WHERE verification_id = ?",
                (result["verification_id"],),
            ).fetchone()
            if existing is not None:
                stored = (
                    existing["evidence_fingerprint"],
                    existing["discovery_id"],
                    existing["request_id"],
                    existing["venue_id"],
                    existing["year"],
                    existing["schema_version"],
                )
                if stored != identities:
                    raise VerificationReplayConflictError(
                        "verification ID already has different semantic evidence"
                    )
                self._verification_from_row(existing)
                return False
            connection.execute(
                """
                INSERT INTO verification_history (
                    verification_id, evidence_fingerprint, discovery_id,
                    request_id, venue_id, year, schema_version, received_at,
                    discovery_fingerprint, request_fingerprint,
                    result_fingerprint, discovery_json, request_json, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["verification_id"],
                    *identities,
                    received_text,
                    artifact_fingerprint(discovery),
                    artifact_fingerprint(request),
                    artifact_fingerprint(result),
                    discovery_json,
                    request_json,
                    result_json,
                ),
            )
        return True

    def replay_verifications(
        self,
        *,
        venue_id: str | None = None,
        year: int | None = None,
    ) -> tuple[VerificationRecord, ...]:
        """Return semantically revalidated history in stable insertion order."""
        if (venue_id is None) != (year is None):
            raise ControlStateError("verification replay filters need venue and year")
        if venue_id is None:
            rows = self._connection.execute(
                "SELECT * FROM verification_history ORDER BY sequence"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM verification_history "
                "WHERE venue_id = ? AND year = ? ORDER BY sequence",
                (venue_id, year),
            ).fetchall()
        return tuple(self._verification_from_row(row) for row in rows)

    def _verification_from_row(self, row: sqlite3.Row) -> VerificationRecord:
        received_at = _timestamp(
            str(row["received_at"]), field="stored verification received_at"
        )
        if received_at != row["received_at"] or int(row["sequence"]) < 1:
            raise StoredDataError("stored verification sequence or timestamp is invalid")
        discovery = _decode_json(row["discovery_json"], label="discovery")
        request = _decode_json(row["request_json"], label="verification request")
        result = _decode_json(row["result_json"], label="verification result")
        for payload, column, label in (
            (discovery, "discovery_fingerprint", "discovery"),
            (request, "request_fingerprint", "verification request"),
            (result, "result_fingerprint", "verification result"),
        ):
            if artifact_fingerprint(payload) != row[column]:
                raise StoredDataError(f"stored {label} fingerprint does not match")
        identity = (
            result.get("verification_id"),
            result.get("evidence_fingerprint"),
            result.get("discovery_id"),
            result.get("request_id"),
            result.get("venue_id"),
            result.get("year"),
            result.get("schema_version"),
        )
        stored = (
            row["verification_id"],
            row["evidence_fingerprint"],
            row["discovery_id"],
            row["request_id"],
            row["venue_id"],
            row["year"],
            row["schema_version"],
        )
        if identity != stored:
            raise StoredDataError("stored verification identity columns do not match")
        try:
            validate_verification_result(result, request, discovery)
        except Exception as exc:
            raise StoredDataError(
                f"stored verification bundle is not replayable: {exc}"
            ) from exc
        return VerificationRecord(
            sequence=int(row["sequence"]),
            received_at=received_at,
            discovery=deepcopy(discovery),
            request=deepcopy(request),
            result=deepcopy(result),
        )

    def store_conference_state(
        self,
        state: Mapping[str, Any],
        *,
        expected_revision: int,
        lease: LeaseHandle,
        stored_at: datetime | str,
    ) -> StateWriteOutcome:
        """Atomically store an optimistic state revision without reducing it."""
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ControlStateError("expected revision must be a non-negative integer")
        assert_secret_free(state)
        validate_contract(ContractName.CONFERENCE_STATE, state)
        stored = _parse_timestamp(stored_at, field="state stored_at")
        stored_text = _timestamp(stored, field="state stored_at")
        state_json = _canonical_json(state)
        fingerprint = artifact_fingerprint(state)
        venue_id = state["venue_id"]
        year = state["year"]
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            row = connection.execute(
                "SELECT * FROM conference_state_current "
                "WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
            current = self._state_from_row(row) if row is not None else None
            if current is not None and current.state_fingerprint == fingerprint:
                return StateWriteOutcome(current, applied=False)
            current_revision = current.revision if current is not None else 0
            if expected_revision != current_revision:
                raise StateRevisionConflictError(
                    f"expected state revision {expected_revision}, "
                    f"found {current_revision}"
                )
            revision = current_revision + 1
            values = (
                venue_id,
                year,
                revision,
                fingerprint,
                stored_text,
                state_json,
            )
            connection.execute(
                """
                INSERT INTO conference_state_history (
                    venue_id, year, revision, state_fingerprint,
                    stored_at, state_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.execute(
                """
                INSERT INTO conference_state_current (
                    venue_id, year, revision, state_fingerprint,
                    stored_at, state_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(venue_id, year) DO UPDATE SET
                    revision = excluded.revision,
                    state_fingerprint = excluded.state_fingerprint,
                    stored_at = excluded.stored_at,
                    state_json = excluded.state_json
                """,
                values,
            )
        record = StateRevision(
            venue_id=venue_id,
            year=year,
            revision=revision,
            stored_at=stored_text,
            state_fingerprint=fingerprint,
            state=deepcopy(dict(state)),
        )
        return StateWriteOutcome(record, applied=True)

    def get_conference_state(
        self,
        venue_id: str,
        year: int,
    ) -> StateRevision | None:
        """Return and validate the current stored state for one venue/year."""
        row = self._connection.execute(
            "SELECT * FROM conference_state_current "
            "WHERE venue_id = ? AND year = ?",
            (venue_id, year),
        ).fetchone()
        if row is None:
            return None
        current = self._state_from_row(row)
        history_row = self._connection.execute(
            "SELECT * FROM conference_state_history "
            "WHERE venue_id = ? AND year = ? AND revision = ?",
            (venue_id, year, current.revision),
        ).fetchone()
        if history_row is None or self._state_from_row(history_row) != current:
            raise StoredDataError("current conference state is absent from history")
        return current

    def conference_state_history(
        self,
        venue_id: str,
        year: int,
    ) -> tuple[StateRevision, ...]:
        """Return validated immutable revisions in ascending order."""
        rows = self._connection.execute(
            "SELECT * FROM conference_state_history "
            "WHERE venue_id = ? AND year = ? ORDER BY revision",
            (venue_id, year),
        ).fetchall()
        history = tuple(self._state_from_row(row) for row in rows)
        if [item.revision for item in history] != list(range(1, len(history) + 1)):
            raise StoredDataError("conference-state revision history is not contiguous")
        return history

    def _state_from_row(self, row: sqlite3.Row) -> StateRevision:
        stored_at = _timestamp(str(row["stored_at"]), field="stored state timestamp")
        revision = int(row["revision"])
        if stored_at != row["stored_at"] or revision < 1:
            raise StoredDataError("stored state revision or timestamp is invalid")
        state = _decode_json(row["state_json"], label="conference state")
        fingerprint = artifact_fingerprint(state)
        if fingerprint != row["state_fingerprint"]:
            raise StoredDataError("stored conference-state fingerprint does not match")
        if state.get("venue_id") != row["venue_id"] or state.get("year") != row["year"]:
            raise StoredDataError("stored conference-state identity does not match")
        try:
            assert_secret_free(state)
            validate_contract(ContractName.CONFERENCE_STATE, state)
        except Exception as exc:
            raise StoredDataError(f"stored conference state is invalid: {exc}") from exc
        return StateRevision(
            venue_id=str(row["venue_id"]),
            year=int(row["year"]),
            revision=revision,
            stored_at=stored_at,
            state_fingerprint=str(row["state_fingerprint"]),
            state=deepcopy(state),
        )

    def observe_case(
        self,
        observation: CaseObservation,
        *,
        lease: LeaseHandle,
    ) -> CaseWriteOutcome:
        """Create or update one deduplicated case under the control lease."""
        event = case_event_payload(observation)
        return self._accept_case_event(event, lease=lease, observation=observation)

    def control_case(
        self,
        case_id: str,
        request: CaseControlRequest,
        *,
        lease: LeaseHandle,
    ) -> CaseWriteOutcome:
        """Persist one resolve, snooze, ignore, or reactivate control."""
        event = case_event_payload(request, case_id=case_id)
        return self._accept_case_event(event, lease=lease, control=request)

    def _accept_case_event(
        self,
        event: Mapping[str, Any],
        *,
        lease: LeaseHandle,
        observation: CaseObservation | None = None,
        control: CaseControlRequest | None = None,
    ) -> CaseWriteOutcome:
        if (observation is None) == (control is None):
            raise ControlStateError("case event requires exactly one typed input")
        assert_secret_free(event)
        validate_case_event_payload(event)
        event_json = _canonical_json(event)
        event_fingerprint = artifact_fingerprint(event)
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            existing_event_row = connection.execute(
                "SELECT * FROM case_event_history WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if existing_event_row is not None:
                existing_event = self._case_event_from_row(existing_event_row)
                if (
                    existing_event.event_fingerprint != event_fingerprint
                    or existing_event.event != dict(event)
                ):
                    raise CaseEventConflictError(
                        "case event ID already has different meaning"
                    )
                current = self._get_case_from_connection(
                    connection, existing_event.case_id
                )
                if current is None:
                    raise StoredDataError("replayed case event has no current case")
                events = self._case_events_from_connection(
                    connection, existing_event.case_id
                )
                if not events or events[-1].resulting_revision != current.revision:
                    raise StoredDataError(
                        "case event history does not reach current revision"
                    )
                return CaseWriteOutcome(
                    record=current,
                    event=existing_event,
                    applied=False,
                    replayed=True,
                )

            if observation is not None:
                current_row = connection.execute(
                    "SELECT * FROM case_state_current "
                    "WHERE venue_id = ? AND year = ? AND blocker = ?",
                    (event["venue_id"], event["year"], event["blocker"]),
                ).fetchone()
            else:
                current_row = connection.execute(
                    "SELECT * FROM case_state_current WHERE case_id = ?",
                    (event["case_id"],),
                ).fetchone()
            current = (
                self._get_case_from_connection(
                    connection, str(current_row["case_id"])
                )
                if current_row is not None
                else None
            )
            if control is not None and current is None:
                raise ControlStateError(f"case {event['case_id']!r} does not exist")
            if current is not None:
                events = self._case_events_from_connection(
                    connection, current.case_id
                )
                if not events or events[-1].resulting_revision != current.revision:
                    raise StoredDataError(
                        "case event history does not reach current revision"
                    )
            if observation is not None:
                mutation = observe_case(
                    current.state if current is not None else None,
                    observation,
                )
            else:
                mutation = control_case(current.state, control)

            previous_revision = current.revision if current is not None else 0
            record = current
            if mutation.changed:
                revision = previous_revision + 1
                stored_at = _timestamp(self._now(), field="case stored_at")
                state_json = _canonical_json(mutation.state)
                state_fingerprint = artifact_fingerprint(mutation.state)
                values = (
                    mutation.state["case_id"],
                    mutation.state["venue_id"],
                    mutation.state["year"],
                    mutation.state["blocker"],
                    mutation.state["status"],
                    revision,
                    state_fingerprint,
                    stored_at,
                    state_json,
                )
                connection.execute(
                    """
                    INSERT INTO case_state_history (
                        case_id, venue_id, year, blocker, status, revision,
                        state_fingerprint, stored_at, state_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                connection.execute(
                    """
                    INSERT INTO case_state_current (
                        case_id, venue_id, year, blocker, status, revision,
                        state_fingerprint, stored_at, state_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(case_id) DO UPDATE SET
                        venue_id = excluded.venue_id,
                        year = excluded.year,
                        blocker = excluded.blocker,
                        status = excluded.status,
                        revision = excluded.revision,
                        state_fingerprint = excluded.state_fingerprint,
                        stored_at = excluded.stored_at,
                        state_json = excluded.state_json
                    """,
                    values,
                )
                record = CaseRevision(
                    case_id=mutation.state["case_id"],
                    venue_id=mutation.state["venue_id"],
                    year=mutation.state["year"],
                    blocker=mutation.state["blocker"],
                    revision=revision,
                    stored_at=stored_at,
                    state_fingerprint=state_fingerprint,
                    state=deepcopy(mutation.state),
                )
            if record is None:
                raise ControlStateError("case event did not produce durable state")

            resulting_revision = record.revision
            cursor = connection.execute(
                """
                INSERT INTO case_event_history (
                    event_id, case_id, event_kind, event_at,
                    event_fingerprint, previous_revision, resulting_revision,
                    revision_applied, meaningful_change, reactivated, event_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["case_id"],
                    event["event_kind"],
                    event["at"],
                    event_fingerprint,
                    previous_revision,
                    resulting_revision,
                    int(mutation.changed),
                    int(mutation.meaningful_change),
                    int(mutation.reactivated),
                    event_json,
                ),
            )
            event_row = connection.execute(
                "SELECT * FROM case_event_history WHERE sequence = ?",
                (cursor.lastrowid,),
            ).fetchone()
            event_record = self._case_event_from_row(event_row)
        return CaseWriteOutcome(
            record=record,
            event=event_record,
            applied=mutation.changed,
            replayed=False,
        )

    def get_case(self, case_id: str) -> CaseRevision | None:
        """Return and validate the current revision of one case."""
        return self._get_case_from_connection(self._connection, case_id)

    def _get_case_from_connection(
        self,
        connection: sqlite3.Connection,
        case_id: str,
    ) -> CaseRevision | None:
        row = connection.execute(
            "SELECT * FROM case_state_current WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            return None
        current = self._case_revision_from_row(row)
        history = self._case_history_from_connection(connection, case_id)
        if not history or history[-1] != current:
            raise StoredDataError("current case state is absent from history")
        return current

    def list_cases(
        self,
        *,
        include_closed: bool = False,
        venue_id: str | None = None,
        year: int | None = None,
    ) -> tuple[CaseRevision, ...]:
        """List stable current cases, unresolved-only unless explicitly widened."""
        if not isinstance(include_closed, bool):
            raise ControlStateError("include_closed must be a boolean")
        if (venue_id is None) != (year is None):
            raise ControlStateError("case filters need venue and year")
        clauses: list[str] = []
        parameters: list[Any] = []
        if not include_closed:
            clauses.append("status IN (?, ?, ?, ?)")
            parameters.extend(("open", "stalled", "dormant", "snoozed"))
        if venue_id is not None:
            clauses.append("venue_id = ? AND year = ?")
            parameters.extend((venue_id, year))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM case_state_current{where} ORDER BY case_id",
            parameters,
        ).fetchall()
        records = []
        for row in rows:
            record = self._get_case_from_connection(
                self._connection, str(row["case_id"])
            )
            if record is None:
                raise StoredDataError("listed case disappeared during read")
            records.append(record)
        return tuple(records)

    def case_history(self, case_id: str) -> tuple[CaseRevision, ...]:
        """Return validated immutable case revisions in ascending order."""
        return self._case_history_from_connection(self._connection, case_id)

    def _case_history_from_connection(
        self,
        connection: sqlite3.Connection,
        case_id: str,
    ) -> tuple[CaseRevision, ...]:
        rows = connection.execute(
            "SELECT * FROM case_state_history "
            "WHERE case_id = ? ORDER BY revision",
            (case_id,),
        ).fetchall()
        history = tuple(self._case_revision_from_row(row) for row in rows)
        if [item.revision for item in history] != list(range(1, len(history) + 1)):
            raise StoredDataError("case-state revision history is not contiguous")
        return history

    def case_event_history(self, case_id: str) -> tuple[CaseEventRecord, ...]:
        """Return validated immutable events for one case in insertion order."""
        return self._case_events_from_connection(self._connection, case_id)

    def _case_events_from_connection(
        self,
        connection: sqlite3.Connection,
        case_id: str,
    ) -> tuple[CaseEventRecord, ...]:
        rows = connection.execute(
            "SELECT * FROM case_event_history "
            "WHERE case_id = ? ORDER BY sequence",
            (case_id,),
        ).fetchall()
        events = tuple(self._case_event_from_row(row) for row in rows)
        previous_revision = 0
        for event in events:
            if event.previous_revision != previous_revision:
                raise StoredDataError("case event revision history is not contiguous")
            previous_revision = event.resulting_revision
        return events

    def _case_revision_from_row(self, row: sqlite3.Row) -> CaseRevision:
        stored_at = _timestamp(str(row["stored_at"]), field="stored case timestamp")
        revision = int(row["revision"])
        if stored_at != row["stored_at"] or revision < 1:
            raise StoredDataError("stored case revision or timestamp is invalid")
        state = _decode_json(row["state_json"], label="case state")
        fingerprint = artifact_fingerprint(state)
        if fingerprint != row["state_fingerprint"]:
            raise StoredDataError("stored case-state fingerprint does not match")
        identity = (
            state.get("case_id"),
            state.get("venue_id"),
            state.get("year"),
            state.get("blocker"),
            state.get("status"),
        )
        stored = (
            row["case_id"],
            row["venue_id"],
            row["year"],
            row["blocker"],
            row["status"],
        )
        if identity != stored:
            raise StoredDataError("stored case-state identity does not match")
        try:
            validate_case_state(state)
        except Exception as exc:
            raise StoredDataError(f"stored case state is invalid: {exc}") from exc
        return CaseRevision(
            case_id=str(row["case_id"]),
            venue_id=str(row["venue_id"]),
            year=int(row["year"]),
            blocker=str(row["blocker"]),
            revision=revision,
            stored_at=stored_at,
            state_fingerprint=str(row["state_fingerprint"]),
            state=deepcopy(state),
        )

    def _case_event_from_row(self, row: sqlite3.Row) -> CaseEventRecord:
        sequence = int(row["sequence"])
        previous_revision = int(row["previous_revision"])
        resulting_revision = int(row["resulting_revision"])
        if sequence < 1 or previous_revision < 0 or resulting_revision < 1:
            raise StoredDataError("stored case event revision or sequence is invalid")
        flags = (
            row["revision_applied"],
            row["meaningful_change"],
            row["reactivated"],
        )
        if any(flag not in (0, 1) for flag in flags):
            raise StoredDataError("stored case event flags are invalid")
        revision_applied, meaningful_change, reactivated = map(bool, flags)
        expected_revision = previous_revision + int(revision_applied)
        if resulting_revision != expected_revision:
            raise StoredDataError("stored case event revisions are inconsistent")
        if reactivated and not meaningful_change:
            raise StoredDataError("reactivated case event must be meaningful")
        event_at = _timestamp(str(row["event_at"]), field="stored case event_at")
        if event_at != row["event_at"]:
            raise StoredDataError("stored case event timestamp is not canonical")
        event = _decode_json(row["event_json"], label="case event")
        fingerprint = artifact_fingerprint(event)
        if fingerprint != row["event_fingerprint"]:
            raise StoredDataError("stored case-event fingerprint does not match")
        identity = (
            event.get("event_id"),
            event.get("case_id"),
            event.get("event_kind"),
            event.get("at"),
        )
        stored = (
            row["event_id"],
            row["case_id"],
            row["event_kind"],
            row["event_at"],
        )
        if identity != stored:
            raise StoredDataError("stored case-event identity does not match")
        try:
            validate_case_event_payload(event)
        except Exception as exc:
            raise StoredDataError(f"stored case event is invalid: {exc}") from exc
        return CaseEventRecord(
            sequence=sequence,
            event_id=str(row["event_id"]),
            case_id=str(row["case_id"]),
            event_kind=str(row["event_kind"]),
            event_at=event_at,
            event_fingerprint=fingerprint,
            previous_revision=previous_revision,
            resulting_revision=resulting_revision,
            revision_applied=revision_applied,
            meaningful_change=meaningful_change,
            reactivated=reactivated,
            event=deepcopy(event),
        )
