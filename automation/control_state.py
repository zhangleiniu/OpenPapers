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
from automation.job_results import (
    manifest_object_name,
    result_object_name,
    validate_result_bundle,
)
from automation.notifications import (
    FailureCategory,
    NotificationIntent,
    TransportFailure,
    classify_transport_failure,
    notification_intent_from_payload,
    validate_notification_intent,
    validate_receipt_id,
)
from automation.verification import validate_verification_result


CONTROL_SCHEMA_VERSION = 4
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


class NotificationIntentConflictError(ControlStateError):
    """Raised when a notification or source identity changes meaning."""


class NotificationDeliveryStateError(ControlStateError):
    """Raised when a notification attempt violates its state machine."""


class JobResultConsumptionConflictError(ControlStateError):
    """Raised when one job ID is reused with different result semantics."""


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


@dataclass(frozen=True)
class NotificationRecord:
    """Current durable delivery state for one immutable intent."""

    notification_id: str
    kind: str
    status: str
    registered_at: str
    updated_at: str
    attempt_count: int
    delivered_at: str | None
    last_failure_category: str | None
    receipt_id: str | None
    intent_fingerprint: str
    intent: NotificationIntent


@dataclass(frozen=True)
class NotificationWriteOutcome:
    """Result of registering one immutable notification intent as output."""

    record: NotificationRecord
    applied: bool


@dataclass(frozen=True)
class NotificationAttemptRecord:
    """One immutable-numbered notification delivery attempt."""

    notification_id: str
    attempt_number: int
    started_at: str
    completed_at: str | None
    outcome: str
    failure_category: str | None
    receipt_id: str | None


@dataclass(frozen=True)
class JobResultConsumptionRecord:
    """One immutable, fully revalidated cloud consumption record."""

    sequence: int
    job_id: str
    job_fingerprint: str
    job_type: str
    venue_id: str
    year: int
    consumed_at: str
    manifest_object_name: str
    manifest_generation: int
    result_object_name: str
    result_generation: int
    job: dict[str, Any]
    manifest: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class JobResultConsumptionOutcome:
    """Outcome of accepting a result pair under the cloud writer lease."""

    record: JobResultConsumptionRecord
    applied: bool


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

_MIGRATION_3 = (
    """
    CREATE TABLE notification_intent (
        notification_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        registered_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        attempt_count INTEGER NOT NULL,
        delivered_at TEXT,
        last_failure_category TEXT,
        receipt_id TEXT,
        intent_fingerprint TEXT NOT NULL,
        intent_json TEXT NOT NULL,
        CHECK (kind IN ('immediate', 'digest')),
        CHECK (status IN (
            'pending', 'in_flight', 'retryable', 'delivered',
            'permanent_failure'
        )),
        CHECK (attempt_count >= 0)
    )
    """,
    """
    CREATE TABLE notification_source (
        source_id TEXT PRIMARY KEY,
        notification_id TEXT NOT NULL,
        FOREIGN KEY (notification_id)
            REFERENCES notification_intent (notification_id)
    )
    """,
    """
    CREATE INDEX notification_source_intent
    ON notification_source (notification_id, source_id)
    """,
    """
    CREATE TABLE notification_attempt_history (
        notification_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        outcome TEXT NOT NULL,
        failure_category TEXT,
        receipt_id TEXT,
        PRIMARY KEY (notification_id, attempt_number),
        FOREIGN KEY (notification_id)
            REFERENCES notification_intent (notification_id),
        CHECK (attempt_number >= 1),
        CHECK (outcome IN (
            'in_flight', 'retryable', 'delivered', 'permanent_failure'
        ))
    )
    """,
)

_MIGRATION_4 = (
    """
    CREATE TABLE job_result_consumption (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL UNIQUE,
        job_fingerprint TEXT NOT NULL,
        job_type TEXT NOT NULL,
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        consumed_at TEXT NOT NULL,
        manifest_object_name TEXT NOT NULL,
        manifest_generation INTEGER NOT NULL,
        result_object_name TEXT NOT NULL,
        result_generation INTEGER NOT NULL,
        job_payload_fingerprint TEXT NOT NULL,
        manifest_payload_fingerprint TEXT NOT NULL,
        result_payload_fingerprint TEXT NOT NULL,
        job_json TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        CHECK (manifest_generation >= 1),
        CHECK (result_generation >= 1)
    )
    """,
    """
    CREATE INDEX job_result_consumption_venue_year_sequence
    ON job_result_consumption (venue_id, year, sequence)
    """,
)

_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
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

_REQUIRED_COLUMNS_V3 = {
    "notification_intent": {
        "notification_id", "kind", "status", "registered_at", "updated_at",
        "attempt_count", "delivered_at", "last_failure_category", "receipt_id",
        "intent_fingerprint", "intent_json",
    },
    "notification_source": {"source_id", "notification_id"},
    "notification_attempt_history": {
        "notification_id", "attempt_number", "started_at", "completed_at",
        "outcome", "failure_category", "receipt_id",
    },
}

_REQUIRED_COLUMNS_V4 = {
    "job_result_consumption": {
        "sequence", "job_id", "job_fingerprint", "job_type", "venue_id",
        "year", "consumed_at", "manifest_object_name",
        "manifest_generation", "result_object_name", "result_generation",
        "job_payload_fingerprint", "manifest_payload_fingerprint",
        "result_payload_fingerprint", "job_json", "manifest_json",
        "result_json",
    },
}

_REQUIRED_COLUMNS_BY_VERSION = {
    1: _REQUIRED_COLUMNS_V1,
    2: _REQUIRED_COLUMNS_V2,
    3: _REQUIRED_COLUMNS_V3,
    4: _REQUIRED_COLUMNS_V4,
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


def _positive_generation(value: Any, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ControlStateError(f"{field} must be a positive integer")
    return value


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

    def consume_job_result(
        self,
        job: Mapping[str, Any],
        manifest: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        manifest_name: str,
        manifest_generation: int,
        result_name: str,
        result_generation: int,
        lease: LeaseHandle,
        consumed_at: datetime | str,
    ) -> JobResultConsumptionOutcome:
        """Record one validated immutable pair; exact replay is a no-op."""
        assert_secret_free(job)
        assert_secret_free(manifest)
        assert_secret_free(result)
        validate_result_bundle(job, manifest, result)
        if manifest_name != manifest_object_name(job["job_id"]):
            raise ControlStateError("manifest object name does not match the job ID")
        if result_name != result_object_name(job["job_id"]):
            raise ControlStateError("result object name does not match the job ID")
        manifest_generation = _positive_generation(
            manifest_generation, field="manifest generation"
        )
        result_generation = _positive_generation(
            result_generation, field="result generation"
        )
        consumed_text = _timestamp(consumed_at, field="result consumed_at")
        job_json = _canonical_json(job)
        manifest_json = _canonical_json(manifest)
        result_json = _canonical_json(result)
        payload_fingerprints = (
            artifact_fingerprint(job),
            artifact_fingerprint(manifest),
            artifact_fingerprint(result),
        )
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            existing = connection.execute(
                "SELECT * FROM job_result_consumption WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
            if existing is not None:
                record = self._job_result_consumption_from_row(existing)
                replay_identity = (
                    record.manifest_object_name,
                    record.manifest_generation,
                    record.result_object_name,
                    record.result_generation,
                    record.job,
                    record.manifest,
                    record.result,
                )
                supplied_identity = (
                    manifest_name,
                    manifest_generation,
                    result_name,
                    result_generation,
                    dict(job),
                    dict(manifest),
                    dict(result),
                )
                if replay_identity != supplied_identity:
                    raise JobResultConsumptionConflictError(
                        "job result was already consumed with different content "
                        "or object generation"
                    )
                return JobResultConsumptionOutcome(record=record, applied=False)
            connection.execute(
                """
                INSERT INTO job_result_consumption (
                    job_id, job_fingerprint, job_type, venue_id, year,
                    consumed_at, manifest_object_name, manifest_generation,
                    result_object_name, result_generation,
                    job_payload_fingerprint, manifest_payload_fingerprint,
                    result_payload_fingerprint, job_json, manifest_json,
                    result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["job_id"], job["job_fingerprint"], job["job_type"],
                    job["venue_id"], job["year"], consumed_text,
                    manifest_name, manifest_generation, result_name,
                    result_generation, *payload_fingerprints, job_json,
                    manifest_json, result_json,
                ),
            )
            row = connection.execute(
                "SELECT * FROM job_result_consumption WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
            if row is None:
                raise StoredDataError("stored job-result consumption is missing")
            record = self._job_result_consumption_from_row(row)
        return JobResultConsumptionOutcome(record=record, applied=True)

    def get_job_result_consumption(
        self,
        job_id: str,
    ) -> JobResultConsumptionRecord | None:
        """Return one fully revalidated immutable consumption record."""
        row = self._connection.execute(
            "SELECT * FROM job_result_consumption WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._job_result_consumption_from_row(row) if row is not None else None

    def replay_job_result_consumptions(
        self,
        *,
        venue_id: str | None = None,
        year: int | None = None,
    ) -> tuple[JobResultConsumptionRecord, ...]:
        """Return validated consumption history in stable insertion order."""
        if (venue_id is None) != (year is None):
            raise ControlStateError("result replay filters need venue and year")
        if venue_id is None:
            rows = self._connection.execute(
                "SELECT * FROM job_result_consumption ORDER BY sequence"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM job_result_consumption "
                "WHERE venue_id = ? AND year = ? ORDER BY sequence",
                (venue_id, year),
            ).fetchall()
        return tuple(self._job_result_consumption_from_row(row) for row in rows)

    def _job_result_consumption_from_row(
        self,
        row: sqlite3.Row,
    ) -> JobResultConsumptionRecord:
        consumed_at = _timestamp(
            str(row["consumed_at"]), field="stored result consumed_at"
        )
        sequence = int(row["sequence"])
        manifest_generation = int(row["manifest_generation"])
        result_generation = int(row["result_generation"])
        if consumed_at != row["consumed_at"] or sequence < 1:
            raise StoredDataError("stored result sequence or timestamp is invalid")
        if manifest_generation < 1 or result_generation < 1:
            raise StoredDataError("stored result object generation is invalid")
        job = _decode_json(row["job_json"], label="consumed job")
        manifest = _decode_json(row["manifest_json"], label="consumed manifest")
        result = _decode_json(row["result_json"], label="consumed result")
        for payload, column, label in (
            (job, "job_payload_fingerprint", "consumed job"),
            (manifest, "manifest_payload_fingerprint", "consumed manifest"),
            (result, "result_payload_fingerprint", "consumed result"),
        ):
            if artifact_fingerprint(payload) != row[column]:
                raise StoredDataError(f"stored {label} fingerprint does not match")
        try:
            expected_names = (
                manifest_object_name(job.get("job_id")),
                result_object_name(job.get("job_id")),
            )
        except Exception as exc:
            raise StoredDataError("stored job ID cannot derive object names") from exc
        stored_identity = (
            row["job_id"], row["job_fingerprint"], row["job_type"],
            row["venue_id"], row["year"], row["manifest_object_name"],
            row["result_object_name"],
        )
        payload_identity = (
            job.get("job_id"), job.get("job_fingerprint"), job.get("job_type"),
            job.get("venue_id"), job.get("year"), *expected_names,
        )
        if stored_identity != payload_identity:
            raise StoredDataError("stored job-result identity columns do not match")
        try:
            validate_result_bundle(job, manifest, result)
        except Exception as exc:
            raise StoredDataError(
                f"stored job-result bundle is not replayable: {exc}"
            ) from exc
        return JobResultConsumptionRecord(
            sequence=sequence,
            job_id=str(row["job_id"]),
            job_fingerprint=str(row["job_fingerprint"]),
            job_type=str(row["job_type"]),
            venue_id=str(row["venue_id"]),
            year=int(row["year"]),
            consumed_at=consumed_at,
            manifest_object_name=str(row["manifest_object_name"]),
            manifest_generation=manifest_generation,
            result_object_name=str(row["result_object_name"]),
            result_generation=result_generation,
            job=deepcopy(job),
            manifest=deepcopy(manifest),
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

    def register_notification_intent(
        self,
        intent: NotificationIntent,
        *,
        lease: LeaseHandle,
        registered_at: datetime | str,
    ) -> NotificationWriteOutcome:
        """Persist an immutable pending intent without claiming delivery.

        This registration-only boundary is suitable for shadow output. It
        creates no attempt row and grants no authority to call a transport.
        """
        validate_notification_intent(intent)
        payload = intent.to_payload()
        assert_secret_free(payload)
        intent_json = _canonical_json(payload)
        fingerprint = artifact_fingerprint(payload)
        registered = _timestamp(
            registered_at, field="notification registered_at"
        )
        if _parse_timestamp(
            registered, field="notification registered_at"
        ) < _parse_timestamp(intent.created_at, field="notification created_at"):
            raise NotificationDeliveryStateError(
                "notification registration cannot precede intent creation"
            )

        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            record, applied = self._register_notification_in_connection(
                connection,
                intent=intent,
                payload=payload,
                intent_json=intent_json,
                fingerprint=fingerprint,
                registered_at=registered,
            )
        return NotificationWriteOutcome(record=record, applied=applied)

    def _register_notification_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        intent: NotificationIntent,
        payload: Mapping[str, Any],
        intent_json: str,
        fingerprint: str,
        registered_at: str,
    ) -> tuple[NotificationRecord, bool]:
        row = connection.execute(
            "SELECT * FROM notification_intent WHERE notification_id = ?",
            (intent.notification_id,),
        ).fetchone()
        applied = row is None
        if applied:
            for source_id in intent.source_ids:
                source_row = connection.execute(
                    "SELECT notification_id FROM notification_source "
                    "WHERE source_id = ?",
                    (source_id,),
                ).fetchone()
                if source_row is not None:
                    raise NotificationIntentConflictError(
                        "notification source already belongs to another intent"
                    )
            connection.execute(
                """
                INSERT INTO notification_intent (
                    notification_id, kind, status, registered_at,
                    updated_at, attempt_count, delivered_at,
                    last_failure_category, receipt_id,
                    intent_fingerprint, intent_json
                ) VALUES (?, ?, 'pending', ?, ?, 0, NULL, NULL, NULL, ?, ?)
                """,
                (
                    intent.notification_id,
                    intent.kind.value,
                    registered_at,
                    registered_at,
                    fingerprint,
                    intent_json,
                ),
            )
            connection.executemany(
                "INSERT INTO notification_source (source_id, notification_id) "
                "VALUES (?, ?)",
                (
                    (source_id, intent.notification_id)
                    for source_id in intent.source_ids
                ),
            )
        record = self._get_notification_from_connection(
            connection, intent.notification_id
        )
        if record is None:
            raise ControlStateError("notification registration disappeared")
        if (
            record.intent_fingerprint != fingerprint
            or record.intent.to_payload() != dict(payload)
        ):
            raise NotificationIntentConflictError(
                "notification ID already has different meaning"
            )
        return record, applied

    def prepare_notification_delivery(
        self,
        intent: NotificationIntent,
        *,
        lease: LeaseHandle,
        started_at: datetime | str,
    ) -> NotificationAttemptRecord | None:
        """Register an immutable intent and claim its next explicit attempt.

        Delivered, permanent-failure, and unresolved in-flight records return
        no claim, so a caller cannot perform a stateless duplicate effect.
        Retryable records may be claimed again by an explicit caller.
        """
        validate_notification_intent(intent)
        payload = intent.to_payload()
        assert_secret_free(payload)
        intent_json = _canonical_json(payload)
        fingerprint = artifact_fingerprint(payload)
        started = _timestamp(started_at, field="notification attempt started_at")
        started_time = _parse_timestamp(
            started, field="notification attempt started_at"
        )
        if started_time < _parse_timestamp(
            intent.created_at,
            field="notification created_at",
        ):
            raise NotificationDeliveryStateError(
                "notification attempt cannot precede intent creation"
            )

        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            record, _ = self._register_notification_in_connection(
                connection,
                intent=intent,
                payload=payload,
                intent_json=intent_json,
                fingerprint=fingerprint,
                registered_at=started,
            )
            if record.status not in {"pending", "retryable"}:
                return None
            if started_time < _parse_timestamp(
                record.updated_at,
                field="notification updated_at",
            ):
                raise NotificationDeliveryStateError(
                    "notification attempt time cannot regress"
                )

            attempt_number = record.attempt_count + 1
            connection.execute(
                """
                INSERT INTO notification_attempt_history (
                    notification_id, attempt_number, started_at, completed_at,
                    outcome, failure_category, receipt_id
                ) VALUES (?, ?, ?, NULL, 'in_flight', NULL, NULL)
                """,
                (intent.notification_id, attempt_number, started),
            )
            connection.execute(
                """
                UPDATE notification_intent
                SET status = 'in_flight', updated_at = ?, attempt_count = ?,
                    delivered_at = NULL, last_failure_category = NULL,
                    receipt_id = NULL
                WHERE notification_id = ?
                """,
                (started, attempt_number, intent.notification_id),
            )
            attempt_row = connection.execute(
                "SELECT * FROM notification_attempt_history "
                "WHERE notification_id = ? AND attempt_number = ?",
                (intent.notification_id, attempt_number),
            ).fetchone()
            return self._notification_attempt_from_row(attempt_row)

    def complete_notification_delivery(
        self,
        notification_id: str,
        attempt_number: int,
        *,
        status: str,
        lease: LeaseHandle,
        completed_at: datetime | str,
        failure_category: str | None = None,
        receipt_id: str | None = None,
    ) -> NotificationRecord:
        """Finalize the current in-flight attempt with only safe metadata."""
        if status not in {"retryable", "delivered", "permanent_failure"}:
            raise NotificationDeliveryStateError(
                "notification completion status is invalid"
            )
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number < 1
        ):
            raise NotificationDeliveryStateError(
                "notification attempt number must be a positive integer"
            )
        if status == "delivered":
            if failure_category is not None or receipt_id is None:
                raise NotificationDeliveryStateError(
                    "delivered notification requires only a receipt ID"
                )
            validate_receipt_id(receipt_id)
            resolved_category = None
        else:
            if failure_category is None or receipt_id is not None:
                raise NotificationDeliveryStateError(
                    "failed notification requires only a failure category"
                )
            try:
                resolved_category = FailureCategory(failure_category)
            except (TypeError, ValueError) as exc:
                raise NotificationDeliveryStateError(
                    "notification failure category is invalid"
                ) from exc
            decision = classify_transport_failure(TransportFailure(resolved_category))
            expected_status = "retryable" if decision.retryable else "permanent_failure"
            if status != expected_status:
                raise NotificationDeliveryStateError(
                    "notification failure status does not match its category"
                )
        completed = _timestamp(
            completed_at, field="notification attempt completed_at"
        )

        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            record = self._get_notification_from_connection(
                connection, notification_id
            )
            if record is None:
                raise NotificationDeliveryStateError("notification does not exist")
            if record.status != "in_flight" or record.attempt_count != attempt_number:
                raise NotificationDeliveryStateError(
                    "notification attempt is not the current in-flight claim"
                )
            attempt_row = connection.execute(
                "SELECT * FROM notification_attempt_history "
                "WHERE notification_id = ? AND attempt_number = ?",
                (notification_id, attempt_number),
            ).fetchone()
            if attempt_row is None:
                raise StoredDataError("current notification attempt is missing")
            attempt = self._notification_attempt_from_row(attempt_row)
            if attempt.outcome != "in_flight" or attempt.completed_at is not None:
                raise NotificationDeliveryStateError(
                    "notification attempt was already completed"
                )
            if _parse_timestamp(
                completed,
                field="notification completed_at",
            ) < _parse_timestamp(
                attempt.started_at,
                field="notification started_at",
            ):
                raise NotificationDeliveryStateError(
                    "notification completion time cannot regress"
                )
            connection.execute(
                """
                UPDATE notification_attempt_history
                SET completed_at = ?, outcome = ?, failure_category = ?,
                    receipt_id = ?
                WHERE notification_id = ? AND attempt_number = ?
                """,
                (
                    completed,
                    status,
                    resolved_category.value if resolved_category is not None else None,
                    receipt_id,
                    notification_id,
                    attempt_number,
                ),
            )
            connection.execute(
                """
                UPDATE notification_intent
                SET status = ?, updated_at = ?, delivered_at = ?,
                    last_failure_category = ?, receipt_id = ?
                WHERE notification_id = ?
                """,
                (
                    status,
                    completed,
                    completed if status == "delivered" else None,
                    resolved_category.value if resolved_category is not None else None,
                    receipt_id,
                    notification_id,
                ),
            )
            completed_record = self._get_notification_from_connection(
                connection, notification_id
            )
            if completed_record is None:
                raise ControlStateError("completed notification disappeared")
            return completed_record

    def get_notification(self, notification_id: str) -> NotificationRecord | None:
        """Return one fully revalidated notification delivery record."""
        return self._get_notification_from_connection(
            self._connection, notification_id
        )

    def get_notification_by_source(
        self, source_id: str
    ) -> NotificationRecord | None:
        """Return the one validated intent that immutably claims a source."""
        row = self._connection.execute(
            "SELECT notification_id FROM notification_source WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        record = self._get_notification_from_connection(
            self._connection, str(row["notification_id"])
        )
        if record is None or source_id not in record.intent.source_ids:
            raise StoredDataError(
                "notification source does not reference a valid intent"
            )
        return record

    def _get_notification_from_connection(
        self,
        connection: sqlite3.Connection,
        notification_id: str,
    ) -> NotificationRecord | None:
        row = connection.execute(
            "SELECT * FROM notification_intent WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if row is None:
            return None
        record = self._notification_from_row(row)
        sources = tuple(
            str(source_row["source_id"])
            for source_row in connection.execute(
                "SELECT source_id FROM notification_source "
                "WHERE notification_id = ? ORDER BY source_id",
                (notification_id,),
            ).fetchall()
        )
        if sources != record.intent.source_ids:
            raise StoredDataError("stored notification sources do not match intent")
        attempts = self._notification_attempts_from_connection(
            connection, notification_id
        )
        if len(attempts) != record.attempt_count:
            raise StoredDataError("notification attempt count does not match history")
        if attempts:
            if attempts[-1].outcome != record.status:
                raise StoredDataError(
                    "notification status does not match latest attempt"
                )
        elif record.status != "pending":
            raise StoredDataError("notification without attempts must be pending")
        return record

    def notification_attempt_history(
        self,
        notification_id: str,
    ) -> tuple[NotificationAttemptRecord, ...]:
        """Return validated attempts in ascending attempt order."""
        record = self.get_notification(notification_id)
        if record is None:
            return ()
        return self._notification_attempts_from_connection(
            self._connection, notification_id
        )

    def _notification_attempts_from_connection(
        self,
        connection: sqlite3.Connection,
        notification_id: str,
    ) -> tuple[NotificationAttemptRecord, ...]:
        rows = connection.execute(
            "SELECT * FROM notification_attempt_history "
            "WHERE notification_id = ? ORDER BY attempt_number",
            (notification_id,),
        ).fetchall()
        attempts = tuple(self._notification_attempt_from_row(row) for row in rows)
        if [item.attempt_number for item in attempts] != list(
            range(1, len(attempts) + 1)
        ):
            raise StoredDataError("notification attempt history is not contiguous")
        previous_completed: str | None = None
        for attempt in attempts:
            if previous_completed is not None and _parse_timestamp(
                attempt.started_at, field="notification attempt started_at"
            ) < _parse_timestamp(
                previous_completed, field="notification prior completed_at"
            ):
                raise StoredDataError("notification attempt history regresses")
            previous_completed = attempt.completed_at
        return attempts

    def _notification_from_row(self, row: sqlite3.Row) -> NotificationRecord:
        registered_at = _timestamp(
            str(row["registered_at"]), field="stored notification registered_at"
        )
        updated_at = _timestamp(
            str(row["updated_at"]), field="stored notification updated_at"
        )
        if registered_at != row["registered_at"] or updated_at != row["updated_at"]:
            raise StoredDataError("stored notification timestamps are not canonical")
        if _parse_timestamp(
            updated_at,
            field="notification updated_at",
        ) < _parse_timestamp(
            registered_at,
            field="notification registered_at",
        ):
            raise StoredDataError("stored notification timestamp regresses")
        attempt_count = int(row["attempt_count"])
        if attempt_count < 0:
            raise StoredDataError("stored notification attempt count is invalid")
        payload = _decode_json(row["intent_json"], label="notification intent")
        fingerprint = artifact_fingerprint(payload)
        if fingerprint != row["intent_fingerprint"]:
            raise StoredDataError("stored notification fingerprint does not match")
        try:
            intent = notification_intent_from_payload(payload)
        except Exception as exc:
            raise StoredDataError(
                f"stored notification intent is invalid: {exc}"
            ) from exc
        if (
            intent.notification_id != row["notification_id"]
            or intent.kind.value != row["kind"]
        ):
            raise StoredDataError("stored notification identity does not match")
        if _parse_timestamp(
            registered_at,
            field="notification registered_at",
        ) < _parse_timestamp(
            intent.created_at,
            field="notification created_at",
        ):
            raise StoredDataError("notification was registered before creation")

        status = str(row["status"])
        delivered_at = row["delivered_at"]
        failure_category = row["last_failure_category"]
        receipt_id = row["receipt_id"]
        if delivered_at is not None:
            canonical_delivered = _timestamp(
                str(delivered_at), field="stored notification delivered_at"
            )
            if canonical_delivered != delivered_at or canonical_delivered != updated_at:
                raise StoredDataError("stored delivery timestamp is inconsistent")
            delivered_at = canonical_delivered
        if status == "delivered":
            if (
                delivered_at is None
                or failure_category is not None
                or receipt_id is None
            ):
                raise StoredDataError("stored delivered notification is inconsistent")
            try:
                validate_receipt_id(str(receipt_id))
            except Exception as exc:
                raise StoredDataError("stored notification receipt is invalid") from exc
        elif status in {"retryable", "permanent_failure"}:
            if (
                delivered_at is not None
                or failure_category is None
                or receipt_id is not None
            ):
                raise StoredDataError("stored failed notification is inconsistent")
            try:
                decision = classify_transport_failure(
                    TransportFailure(str(failure_category))
                )
            except Exception as exc:
                raise StoredDataError(
                    "stored notification failure category is invalid"
                ) from exc
            expected = "retryable" if decision.retryable else "permanent_failure"
            if status != expected:
                raise StoredDataError(
                    "stored notification failure category contradicts status"
                )
        elif status in {"pending", "in_flight"}:
            if (
                delivered_at is not None
                or failure_category is not None
                or receipt_id is not None
            ):
                raise StoredDataError("stored open notification is inconsistent")
        else:
            raise StoredDataError("stored notification status is invalid")
        return NotificationRecord(
            notification_id=intent.notification_id,
            kind=intent.kind.value,
            status=status,
            registered_at=registered_at,
            updated_at=updated_at,
            attempt_count=attempt_count,
            delivered_at=delivered_at,
            last_failure_category=(
                str(failure_category) if failure_category is not None else None
            ),
            receipt_id=str(receipt_id) if receipt_id is not None else None,
            intent_fingerprint=fingerprint,
            intent=intent,
        )

    def _notification_attempt_from_row(
        self,
        row: sqlite3.Row,
    ) -> NotificationAttemptRecord:
        attempt_number = int(row["attempt_number"])
        if attempt_number < 1:
            raise StoredDataError("stored notification attempt number is invalid")
        started_at = _timestamp(
            str(row["started_at"]), field="stored notification attempt started_at"
        )
        if started_at != row["started_at"]:
            raise StoredDataError("stored attempt start is not canonical")
        completed_at = row["completed_at"]
        if completed_at is not None:
            canonical_completed = _timestamp(
                str(completed_at), field="stored notification attempt completed_at"
            )
            if canonical_completed != completed_at or _parse_timestamp(
                canonical_completed, field="notification attempt completed_at"
            ) < _parse_timestamp(started_at, field="notification attempt started_at"):
                raise StoredDataError("stored attempt completion is invalid")
            completed_at = canonical_completed
        outcome = str(row["outcome"])
        failure_category = row["failure_category"]
        receipt_id = row["receipt_id"]
        if outcome == "in_flight":
            if (
                completed_at is not None
                or failure_category is not None
                or receipt_id is not None
            ):
                raise StoredDataError("stored in-flight attempt is inconsistent")
        elif outcome == "delivered":
            if (
                completed_at is None
                or failure_category is not None
                or receipt_id is None
            ):
                raise StoredDataError("stored delivered attempt is inconsistent")
            try:
                validate_receipt_id(str(receipt_id))
            except Exception as exc:
                raise StoredDataError("stored attempt receipt is invalid") from exc
        elif outcome in {"retryable", "permanent_failure"}:
            if (
                completed_at is None
                or failure_category is None
                or receipt_id is not None
            ):
                raise StoredDataError("stored failed attempt is inconsistent")
            try:
                decision = classify_transport_failure(
                    TransportFailure(str(failure_category))
                )
            except Exception as exc:
                raise StoredDataError("stored attempt category is invalid") from exc
            expected = "retryable" if decision.retryable else "permanent_failure"
            if outcome != expected:
                raise StoredDataError("stored attempt category contradicts outcome")
        else:
            raise StoredDataError("stored notification attempt outcome is invalid")
        return NotificationAttemptRecord(
            notification_id=str(row["notification_id"]),
            attempt_number=attempt_number,
            started_at=started_at,
            completed_at=(str(completed_at) if completed_at is not None else None),
            outcome=outcome,
            failure_category=(
                str(failure_category) if failure_category is not None else None
            ),
            receipt_id=str(receipt_id) if receipt_id is not None else None,
        )
