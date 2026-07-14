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
    OwnershipError,
    Writer,
    assert_secret_free,
    assert_writer_allowed,
)
from automation.job_queue import (
    JobQueueError,
    build_scrape_job_from_action,
    validate_job_identity,
)
from automation.job_results import (
    manifest_object_name,
    result_object_name,
    validate_result_bundle,
)
from automation.lifecycle import ActionIntent, action_intent_from_payload
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


CONTROL_SCHEMA_VERSION = 7
DEFAULT_LEASE_TTL_SECONDS = 300
MAX_LEASE_TTL_SECONDS = 86_400
DEFAULT_SCHEDULER_SELECTION_LIMIT = 100
MAX_SCHEDULER_SELECTION_LIMIT = 1000
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


class SchedulerWakeupConflictError(ControlStateError):
    """Raised when a wakeup identity changes meaning or remains ambiguous."""


class ExecutionQueueError(ControlStateError):
    """Raised when durable execution enqueue, claim, or completion is unsafe."""


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
class DueWorkSelection:
    """One stable selection of a persisted due conference-year schedule."""

    selection_id: str
    venue_id: str
    year: int
    next_check_at: str
    selected_at: str
    first_wakeup_id: str
    state_revision: int
    state_fingerprint: str


@dataclass(frozen=True)
class SchedulerWakeupRecord:
    """One bounded scheduler invocation retained for replay and recovery."""

    wakeup_id: str
    scheduled_for: str
    started_at: str
    completed_at: str | None
    status: str
    due_cutoff_at: str
    selection_limit: int
    eligible_count: int | None
    new_selection_count: int | None
    duplicate_selection_count: int | None
    truncated_count: int | None


@dataclass(frozen=True)
class SchedulerWakeupStartOutcome:
    """Result of durably beginning a wakeup or replaying a completed one."""

    record: SchedulerWakeupRecord
    applied: bool


@dataclass(frozen=True)
class SchedulerDuePlan:
    """Bounded due selections retained while their wakeup remains active."""

    record: SchedulerWakeupRecord
    selections: tuple[DueWorkSelection, ...]
    eligible_count: int
    new_selection_count: int
    duplicate_selection_count: int
    truncated_count: int
    applied: bool


@dataclass(frozen=True)
class SchedulerWakeupOutcome:
    """Result of a first wakeup completion or an exact completed replay."""

    record: SchedulerWakeupRecord
    selections: tuple[DueWorkSelection, ...]
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


@dataclass(frozen=True)
class ExecutionJobRecord:
    """Current durable state for one retained verified scraper action/job."""

    job_id: str
    action_id: str
    source_verification_id: str
    venue_id: str
    year: int
    enqueued_at: str
    state: str
    current_attempt_number: int
    action_fingerprint: str
    job_fingerprint: str
    action: dict[str, Any]
    job: dict[str, Any]


@dataclass(frozen=True)
class ExecutionRetentionOutcome:
    """Result of retaining one verified action; false for exact replay."""

    record: ExecutionJobRecord
    applied: bool


@dataclass(frozen=True)
class ExecutionAttemptClaim:
    """Opaque authority for one in-flight dispatch attempt of one job."""

    job_id: str
    attempt_number: int
    claim_token: str
    started_at: str
    job: dict[str, Any]


@dataclass(frozen=True)
class ExecutionAttemptRecord:
    """One immutable-numbered dispatch attempt for a durable execution job."""

    job_id: str
    attempt_number: int
    claim_token: str
    started_at: str
    completed_at: str | None
    disposition: str | None
    status: str | None
    failure_class: str | None
    reason_code: str | None
    result_job_id: str | None
    published: bool | None
    retry_permitted: bool | None
    paper_count: int | None
    valid_pdf_count: int | None


@dataclass(frozen=True)
class ExecutionCompletionOutcome:
    """Result of durably closing or retrying the current claimed attempt."""

    record: ExecutionJobRecord
    attempt: ExecutionAttemptRecord


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

_MIGRATION_5 = (
    """
    CREATE TABLE control_ownership (
        ownership_id INTEGER PRIMARY KEY CHECK (ownership_id = 1),
        owner_kind TEXT NOT NULL CHECK (
            owner_kind IN ('cloud_control_plane', 'local_control_plane')
        ),
        established_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE scheduler_wakeup (
        wakeup_id TEXT PRIMARY KEY,
        scheduled_for TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('active', 'completed')),
        due_cutoff_at TEXT NOT NULL,
        selection_limit INTEGER NOT NULL CHECK (selection_limit >= 1),
        eligible_count INTEGER CHECK (eligible_count >= 0),
        new_selection_count INTEGER CHECK (new_selection_count >= 0),
        duplicate_selection_count INTEGER CHECK (duplicate_selection_count >= 0),
        truncated_count INTEGER CHECK (truncated_count >= 0),
        CHECK (
            (status = 'active' AND completed_at IS NULL
                AND eligible_count IS NULL AND new_selection_count IS NULL
                AND duplicate_selection_count IS NULL AND truncated_count IS NULL)
            OR
            (status = 'completed' AND completed_at IS NOT NULL
                AND eligible_count IS NOT NULL AND new_selection_count IS NOT NULL
                AND duplicate_selection_count IS NOT NULL
                AND truncated_count IS NOT NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX scheduler_one_active_wakeup
    ON scheduler_wakeup (status) WHERE status = 'active'
    """,
    """
    CREATE TABLE scheduler_due_selection (
        selection_id TEXT PRIMARY KEY,
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        next_check_at TEXT NOT NULL,
        selected_at TEXT NOT NULL,
        first_wakeup_id TEXT NOT NULL,
        state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
        state_fingerprint TEXT NOT NULL,
        UNIQUE (venue_id, year, next_check_at),
        FOREIGN KEY (first_wakeup_id) REFERENCES scheduler_wakeup (wakeup_id),
        FOREIGN KEY (venue_id, year, state_revision)
            REFERENCES conference_state_history (venue_id, year, revision)
    )
    """,
    """
    CREATE INDEX scheduler_due_selection_wakeup
    ON scheduler_due_selection (first_wakeup_id, venue_id, year)
    """,
)

_MIGRATION_6 = (
    """
    CREATE TABLE scheduler_wakeup_plan (
        wakeup_id TEXT PRIMARY KEY,
        planned_at TEXT NOT NULL,
        eligible_count INTEGER NOT NULL CHECK (eligible_count >= 0),
        new_selection_count INTEGER NOT NULL CHECK (new_selection_count >= 0),
        duplicate_selection_count INTEGER NOT NULL
            CHECK (duplicate_selection_count >= 0),
        truncated_count INTEGER NOT NULL CHECK (truncated_count >= 0),
        CHECK (
            new_selection_count + duplicate_selection_count + truncated_count
                = eligible_count
        ),
        FOREIGN KEY (wakeup_id) REFERENCES scheduler_wakeup (wakeup_id)
    )
    """,
)

_MIGRATION_7 = (
    """
    CREATE TABLE execution_job (
        job_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL UNIQUE,
        source_verification_id TEXT NOT NULL,
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        enqueued_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'in_flight', 'completed')),
        current_attempt_number INTEGER NOT NULL CHECK (current_attempt_number >= 0),
        action_fingerprint TEXT NOT NULL,
        job_fingerprint TEXT NOT NULL,
        action_json TEXT NOT NULL,
        job_json TEXT NOT NULL,
        FOREIGN KEY (source_verification_id)
            REFERENCES verification_history (verification_id)
    )
    """,
    """
    CREATE INDEX execution_job_state_enqueued
    ON execution_job (state, enqueued_at, job_id)
    """,
    """
    CREATE TABLE execution_attempt_history (
        job_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
        claim_token TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        disposition TEXT CHECK (disposition IN ('retry', 'completed')),
        status TEXT,
        failure_class TEXT,
        reason_code TEXT,
        result_job_id TEXT,
        published INTEGER,
        retry_permitted INTEGER,
        paper_count INTEGER CHECK (paper_count IS NULL OR paper_count >= 0),
        valid_pdf_count INTEGER
            CHECK (valid_pdf_count IS NULL OR valid_pdf_count >= 0),
        PRIMARY KEY (job_id, attempt_number),
        FOREIGN KEY (job_id) REFERENCES execution_job (job_id),
        CHECK (
            (completed_at IS NULL AND disposition IS NULL AND status IS NULL
                AND reason_code IS NULL AND published IS NULL
                AND retry_permitted IS NULL)
            OR
            (completed_at IS NOT NULL AND disposition IS NOT NULL
                AND status IS NOT NULL AND reason_code IS NOT NULL
                AND published IS NOT NULL AND retry_permitted IS NOT NULL)
        )
    )
    """,
)

_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
    6: _MIGRATION_6,
    7: _MIGRATION_7,
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

_REQUIRED_COLUMNS_V5 = {
    "control_ownership": {"ownership_id", "owner_kind", "established_at"},
    "scheduler_wakeup": {
        "wakeup_id", "scheduled_for", "started_at", "completed_at", "status",
        "due_cutoff_at", "selection_limit", "eligible_count",
        "new_selection_count", "duplicate_selection_count", "truncated_count",
    },
    "scheduler_due_selection": {
        "selection_id", "venue_id", "year", "next_check_at", "selected_at",
        "first_wakeup_id", "state_revision", "state_fingerprint",
    },
}

_REQUIRED_COLUMNS_V6 = {
    "scheduler_wakeup_plan": {
        "wakeup_id", "planned_at", "eligible_count", "new_selection_count",
        "duplicate_selection_count", "truncated_count",
    },
}

_REQUIRED_COLUMNS_V7 = {
    "execution_job": {
        "job_id", "action_id", "source_verification_id", "venue_id", "year",
        "enqueued_at", "state", "current_attempt_number", "action_fingerprint",
        "job_fingerprint", "action_json", "job_json",
    },
    "execution_attempt_history": {
        "job_id", "attempt_number", "claim_token", "started_at", "completed_at",
        "disposition", "status", "failure_class", "reason_code",
        "result_job_id", "published", "retry_permitted", "paper_count",
        "valid_pdf_count",
    },
}

_REQUIRED_COLUMNS_BY_VERSION = {
    1: _REQUIRED_COLUMNS_V1,
    2: _REQUIRED_COLUMNS_V2,
    3: _REQUIRED_COLUMNS_V3,
    4: _REQUIRED_COLUMNS_V4,
    5: _REQUIRED_COLUMNS_V5,
    6: _REQUIRED_COLUMNS_V6,
    7: _REQUIRED_COLUMNS_V7,
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


def _validate_scheduler_identity(value: str, *, field: str, prefix: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or not 10 <= len(value) <= 160
        or any(character.isspace() for character in value)
    ):
        raise ControlStateError(f"{field} is invalid")


def _validate_selection_limit(value: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_SCHEDULER_SELECTION_LIMIT
    ):
        raise ControlStateError(
            "scheduler selection limit must be between 1 and "
            f"{MAX_SCHEDULER_SELECTION_LIMIT}"
        )


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
    """Versioned SQLite repository bound to one durable mutable owner."""

    def __init__(
        self,
        path: Path,
        *,
        writer: Writer | str = Writer.CLOUD_CONTROL_PLANE,
        clock: Callable[[], datetime] = _system_clock,
    ) -> None:
        assert_writer_allowed(writer, ArtifactKind.CONTROL_STATE)
        self.writer = Writer(writer)
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
            self._validate_ownership()
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
        if 0 < version < 5 and self.writer is Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError(
                "legacy control databases are cloud-owned and cannot be opened "
                "by the local control plane"
            )
        original_version = version
        if version > 0:
            self._validate_schema(expected_version=version)
        for target_version in range(version + 1, CONTROL_SCHEMA_VERSION + 1):
            applied_at = _timestamp(self._now(), field="applied_at")
            try:
                with self._write_transaction() as connection:
                    for statement in _MIGRATIONS[target_version]:
                        connection.execute(statement)
                    if target_version == 5:
                        owner = (
                            self.writer
                            if original_version == 0
                            else Writer.CLOUD_CONTROL_PLANE
                        )
                        connection.execute(
                            "INSERT INTO control_ownership "
                            "(ownership_id, owner_kind, established_at) "
                            "VALUES (1, ?, ?)",
                            (owner.value, applied_at),
                        )
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

    def _validate_ownership(self) -> None:
        rows = self._connection.execute(
            "SELECT ownership_id, owner_kind, established_at "
            "FROM control_ownership"
        ).fetchall()
        if len(rows) != 1 or int(rows[0]["ownership_id"]) != 1:
            raise SchemaMigrationError(
                "control database ownership is missing or ambiguous"
            )
        try:
            stored_owner = Writer(str(rows[0]["owner_kind"]))
        except ValueError as exc:
            raise SchemaMigrationError(
                "control database ownership value is invalid"
            ) from exc
        if stored_owner not in {
            Writer.CLOUD_CONTROL_PLANE,
            Writer.LOCAL_CONTROL_PLANE,
        }:
            raise SchemaMigrationError(
                "control database owner is not a control-plane role"
            )
        established_at = _timestamp(
            str(rows[0]["established_at"]), field="ownership established_at"
        )
        if established_at != rows[0]["established_at"]:
            raise SchemaMigrationError(
                "control database ownership timestamp is not canonical"
            )
        if stored_owner is not self.writer:
            raise OwnershipError(
                f"control database is owned by {stored_owner.value}, not "
                f"{self.writer.value}"
            )

    @property
    def control_owner(self) -> Writer:
        """Return the already-validated immutable database owner."""
        return self.writer

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

    def begin_scheduler_wakeup(
        self,
        wakeup_id: str,
        *,
        scheduled_for: datetime | str,
        due_cutoff_at: datetime | str,
        selection_limit: int = DEFAULT_SCHEDULER_SELECTION_LIMIT,
        lease: LeaseHandle,
    ) -> SchedulerWakeupStartOutcome:
        """Durably claim one local wakeup before inspecting due state."""
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError("scheduler wakeups require local control ownership")
        _validate_scheduler_identity(
            wakeup_id, field="scheduler wakeup ID", prefix="scheduler-wakeup:"
        )
        _validate_selection_limit(selection_limit)
        scheduled = _parse_timestamp(scheduled_for, field="wakeup scheduled_for")
        cutoff = _parse_timestamp(due_cutoff_at, field="wakeup due_cutoff_at")
        if scheduled > cutoff:
            raise ControlStateError("wakeup cannot be scheduled after its due cutoff")
        scheduled_text = _timestamp(scheduled, field="wakeup scheduled_for")
        cutoff_text = _timestamp(cutoff, field="wakeup due_cutoff_at")
        started_text = _timestamp(self._now(), field="wakeup started_at")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            active = connection.execute(
                "SELECT wakeup_id FROM scheduler_wakeup WHERE status = 'active'"
            ).fetchone()
            if active is not None:
                raise SchedulerWakeupConflictError(
                    "a scheduler wakeup is active or ambiguously interrupted"
                )
            existing = connection.execute(
                "SELECT * FROM scheduler_wakeup WHERE wakeup_id = ?",
                (wakeup_id,),
            ).fetchone()
            if existing is not None:
                record = self._scheduler_wakeup_from_row(existing)
                if (
                    record.scheduled_for != scheduled_text
                    or record.selection_limit != selection_limit
                ):
                    raise SchedulerWakeupConflictError(
                        "scheduler wakeup ID changed meaning"
                    )
                return SchedulerWakeupStartOutcome(record, applied=False)
            connection.execute(
                """
                INSERT INTO scheduler_wakeup (
                    wakeup_id, scheduled_for, started_at, completed_at, status,
                    due_cutoff_at, selection_limit, eligible_count,
                    new_selection_count, duplicate_selection_count,
                    truncated_count
                ) VALUES (?, ?, ?, NULL, 'active', ?, ?, NULL, NULL, NULL, NULL)
                """,
                (
                    wakeup_id,
                    scheduled_text,
                    started_text,
                    cutoff_text,
                    selection_limit,
                ),
            )
        return SchedulerWakeupStartOutcome(
            SchedulerWakeupRecord(
                wakeup_id=wakeup_id,
                scheduled_for=scheduled_text,
                started_at=started_text,
                completed_at=None,
                status="active",
                due_cutoff_at=cutoff_text,
                selection_limit=selection_limit,
                eligible_count=None,
                new_selection_count=None,
                duplicate_selection_count=None,
                truncated_count=None,
            ),
            applied=True,
        )

    def plan_scheduler_wakeup(
        self,
        wakeup_id: str,
        *,
        lease: LeaseHandle,
        selected_at: datetime | str,
    ) -> SchedulerDuePlan:
        """Select bounded due state while leaving the wakeup durably active."""
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError("scheduler wakeups require local control ownership")
        _validate_scheduler_identity(
            wakeup_id, field="scheduler wakeup ID", prefix="scheduler-wakeup:"
        )
        selected = _parse_timestamp(selected_at, field="wakeup selected_at")
        selected_text = _timestamp(selected, field="wakeup selected_at")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            row = connection.execute(
                "SELECT * FROM scheduler_wakeup WHERE wakeup_id = ?",
                (wakeup_id,),
            ).fetchone()
            if row is None:
                raise SchedulerWakeupConflictError("scheduler wakeup was not started")
            record = self._scheduler_wakeup_from_row(row)
            if record.status == "completed":
                selections = self._scheduler_selections_for_wakeup(
                    connection, wakeup_id
                )
                return SchedulerDuePlan(
                    record,
                    selections,
                    record.eligible_count,
                    record.new_selection_count,
                    record.duplicate_selection_count,
                    record.truncated_count,
                    applied=False,
                )
            if selected < _parse_timestamp(
                record.started_at, field="stored wakeup started_at"
            ):
                raise ControlStateError(
                    "wakeup selection cannot precede its start time"
                )
            existing_plan = connection.execute(
                "SELECT * FROM scheduler_wakeup_plan WHERE wakeup_id = ?",
                (wakeup_id,),
            ).fetchone()
            if existing_plan is not None:
                selections = self._scheduler_selections_for_wakeup(
                    connection, wakeup_id
                )
                counts = tuple(int(existing_plan[name]) for name in (
                    "eligible_count",
                    "new_selection_count",
                    "duplicate_selection_count",
                    "truncated_count",
                ))
                planned_at = _timestamp(
                    str(existing_plan["planned_at"]),
                    field="stored wakeup planned_at",
                )
                if (
                    planned_at != existing_plan["planned_at"]
                    or _parse_timestamp(
                        planned_at, field="stored wakeup planned_at"
                    ) < _parse_timestamp(
                        record.started_at, field="stored wakeup started_at"
                    )
                    or sum(counts[1:]) != counts[0]
                    or len(selections) != counts[1]
                ):
                    raise StoredDataError("stored scheduler plan is inconsistent")
                return SchedulerDuePlan(
                    record, selections, *counts, applied=False
                )
            state_rows = connection.execute(
                "SELECT * FROM conference_state_current ORDER BY venue_id, year"
            ).fetchall()
            due_records: list[StateRevision] = []
            cutoff = _parse_timestamp(
                record.due_cutoff_at, field="stored wakeup due_cutoff_at"
            )
            for state_row in state_rows:
                state_record = self._state_from_row(state_row)
                next_check_at = state_record.state["next_check_at"]
                if next_check_at is None:
                    continue
                next_check = _parse_timestamp(
                    next_check_at, field="conference state next_check_at"
                )
                if next_check <= cutoff:
                    due_records.append(state_record)
            due_records.sort(key=lambda item: (
                item.state["next_check_at"], item.venue_id, item.year
            ))
            eligible_count = len(due_records)
            bounded = due_records[:record.selection_limit]
            truncated_count = eligible_count - len(bounded)
            new_selections: list[DueWorkSelection] = []
            duplicate_count = 0
            for state_record in bounded:
                next_check_at = str(state_record.state["next_check_at"])
                identity = {
                    "venue_id": state_record.venue_id,
                    "year": state_record.year,
                    "next_check_at": next_check_at,
                }
                selection_id = "due-selection:" + artifact_fingerprint(identity)
                existing = connection.execute(
                    "SELECT * FROM scheduler_due_selection "
                    "WHERE venue_id = ? AND year = ? AND next_check_at = ?",
                    (state_record.venue_id, state_record.year, next_check_at),
                ).fetchone()
                if existing is not None:
                    stored = self._scheduler_selection_from_row(existing)
                    if stored.selection_id != selection_id:
                        raise StoredDataError(
                            "stored due-selection identity does not match"
                        )
                    duplicate_count += 1
                    continue
                selection_timestamp = selected_text
                connection.execute(
                    """
                    INSERT INTO scheduler_due_selection (
                        selection_id, venue_id, year, next_check_at, selected_at,
                        first_wakeup_id, state_revision, state_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        selection_id,
                        state_record.venue_id,
                        state_record.year,
                        next_check_at,
                        selection_timestamp,
                        wakeup_id,
                        state_record.revision,
                        state_record.state_fingerprint,
                    ),
                )
                new_selections.append(DueWorkSelection(
                    selection_id=selection_id,
                    venue_id=state_record.venue_id,
                    year=state_record.year,
                    next_check_at=next_check_at,
                    selected_at=selection_timestamp,
                    first_wakeup_id=wakeup_id,
                    state_revision=state_record.revision,
                    state_fingerprint=state_record.state_fingerprint,
                ))
            connection.execute(
                """
                INSERT INTO scheduler_wakeup_plan (
                    wakeup_id, planned_at, eligible_count,
                    new_selection_count, duplicate_selection_count,
                    truncated_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    wakeup_id,
                    selected_text,
                    eligible_count,
                    len(new_selections),
                    duplicate_count,
                    truncated_count,
                ),
            )
        return SchedulerDuePlan(
            record,
            tuple(new_selections),
            eligible_count,
            len(new_selections),
            duplicate_count,
            truncated_count,
            applied=True,
        )

    def finish_scheduler_wakeup(
        self,
        wakeup_id: str,
        *,
        lease: LeaseHandle,
        completed_at: datetime | str,
    ) -> SchedulerWakeupOutcome:
        """Mark a successfully planned local wakeup complete under its lease."""
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError("scheduler wakeups require local control ownership")
        _validate_scheduler_identity(
            wakeup_id, field="scheduler wakeup ID", prefix="scheduler-wakeup:"
        )
        completed = _parse_timestamp(completed_at, field="wakeup completed_at")
        completed_text = _timestamp(completed, field="wakeup completed_at")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            row = connection.execute(
                "SELECT * FROM scheduler_wakeup WHERE wakeup_id = ?",
                (wakeup_id,),
            ).fetchone()
            if row is None:
                raise SchedulerWakeupConflictError("scheduler wakeup was not started")
            record = self._scheduler_wakeup_from_row(row)
            selections = self._scheduler_selections_for_wakeup(
                connection, wakeup_id
            )
            if record.status == "completed":
                return SchedulerWakeupOutcome(record, selections, applied=False)
            plan = connection.execute(
                "SELECT * FROM scheduler_wakeup_plan WHERE wakeup_id = ?",
                (wakeup_id,),
            ).fetchone()
            if plan is None:
                raise SchedulerWakeupConflictError(
                    "scheduler wakeup has not planned due work"
                )
            counts = tuple(int(plan[name]) for name in (
                "eligible_count",
                "new_selection_count",
                "duplicate_selection_count",
                "truncated_count",
            ))
            planned_at = _parse_timestamp(
                str(plan["planned_at"]), field="stored wakeup planned_at"
            )
            if sum(counts[1:]) != counts[0] or len(selections) != counts[1]:
                raise StoredDataError("stored scheduler plan is inconsistent")
            if completed < _parse_timestamp(
                record.started_at, field="stored wakeup started_at"
            ) or completed < planned_at:
                raise ControlStateError(
                    "wakeup completion cannot precede its start or plan time"
                )
            connection.execute(
                "UPDATE scheduler_wakeup SET completed_at = ?, status = 'completed', "
                "eligible_count = ?, new_selection_count = ?, "
                "duplicate_selection_count = ?, truncated_count = ? "
                "WHERE wakeup_id = ? AND status = 'active'",
                (completed_text, *counts, wakeup_id),
            )
            completed_row = connection.execute(
                "SELECT * FROM scheduler_wakeup WHERE wakeup_id = ?",
                (wakeup_id,),
            ).fetchone()
            completed_record = self._scheduler_wakeup_from_row(completed_row)
        return SchedulerWakeupOutcome(
            completed_record, selections, applied=True
        )

    def complete_scheduler_wakeup(
        self,
        wakeup_id: str,
        *,
        lease: LeaseHandle,
        completed_at: datetime | str,
    ) -> SchedulerWakeupOutcome:
        """Select and complete an effect-free wakeup for P4.L1 compatibility."""
        self.plan_scheduler_wakeup(
            wakeup_id,
            lease=lease,
            selected_at=completed_at,
        )
        return self.finish_scheduler_wakeup(
            wakeup_id,
            lease=lease,
            completed_at=completed_at,
        )

    def get_scheduler_wakeup(
        self, wakeup_id: str
    ) -> SchedulerWakeupRecord | None:
        """Return one validated wakeup record without mutating it."""
        row = self._connection.execute(
            "SELECT * FROM scheduler_wakeup WHERE wakeup_id = ?", (wakeup_id,)
        ).fetchone()
        return self._scheduler_wakeup_from_row(row) if row is not None else None

    def list_scheduler_wakeups(self) -> tuple[SchedulerWakeupRecord, ...]:
        """Return validated wakeup history in deterministic order."""
        rows = self._connection.execute(
            "SELECT * FROM scheduler_wakeup ORDER BY started_at, wakeup_id"
        ).fetchall()
        return tuple(self._scheduler_wakeup_from_row(row) for row in rows)

    def list_due_work_selections(self) -> tuple[DueWorkSelection, ...]:
        """Return every validated stable due selection in deterministic order."""
        rows = self._connection.execute(
            "SELECT * FROM scheduler_due_selection "
            "ORDER BY selected_at, venue_id, year, selection_id"
        ).fetchall()
        return tuple(self._scheduler_selection_from_row(row) for row in rows)

    def _scheduler_selections_for_wakeup(
        self,
        connection: sqlite3.Connection,
        wakeup_id: str,
    ) -> tuple[DueWorkSelection, ...]:
        rows = connection.execute(
            "SELECT * FROM scheduler_due_selection WHERE first_wakeup_id = ? "
            "ORDER BY next_check_at, venue_id, year",
            (wakeup_id,),
        ).fetchall()
        return tuple(self._scheduler_selection_from_row(row) for row in rows)

    def _scheduler_wakeup_from_row(
        self, row: sqlite3.Row
    ) -> SchedulerWakeupRecord:
        wakeup_id = str(row["wakeup_id"])
        _validate_scheduler_identity(
            wakeup_id, field="stored scheduler wakeup ID", prefix="scheduler-wakeup:"
        )
        status = str(row["status"])
        if status not in {"active", "completed"}:
            raise StoredDataError("stored scheduler wakeup status is invalid")
        scheduled_for = _timestamp(
            str(row["scheduled_for"]), field="stored wakeup scheduled_for"
        )
        started_at = _timestamp(
            str(row["started_at"]), field="stored wakeup started_at"
        )
        due_cutoff_at = _timestamp(
            str(row["due_cutoff_at"]), field="stored wakeup due_cutoff_at"
        )
        completed_at = (
            None
            if row["completed_at"] is None
            else _timestamp(
                str(row["completed_at"]), field="stored wakeup completed_at"
            )
        )
        selection_limit = int(row["selection_limit"])
        _validate_selection_limit(selection_limit)
        counts = tuple(
            None if row[name] is None else int(row[name])
            for name in (
                "eligible_count",
                "new_selection_count",
                "duplicate_selection_count",
                "truncated_count",
            )
        )
        if status == "active" and (completed_at is not None or any(
            count is not None for count in counts
        )):
            raise StoredDataError("stored active wakeup has completion data")
        if status == "completed" and (completed_at is None or any(
            count is None or count < 0 for count in counts
        )):
            raise StoredDataError("stored completed wakeup is incomplete")
        if status == "completed" and (
            counts[1] + counts[2] + counts[3] != counts[0]
        ):
            raise StoredDataError("stored completed wakeup counts are inconsistent")
        return SchedulerWakeupRecord(
            wakeup_id=wakeup_id,
            scheduled_for=scheduled_for,
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            due_cutoff_at=due_cutoff_at,
            selection_limit=selection_limit,
            eligible_count=counts[0],
            new_selection_count=counts[1],
            duplicate_selection_count=counts[2],
            truncated_count=counts[3],
        )

    def _scheduler_selection_from_row(
        self, row: sqlite3.Row
    ) -> DueWorkSelection:
        selection_id = str(row["selection_id"])
        _validate_scheduler_identity(
            selection_id,
            field="stored due-selection ID",
            prefix="due-selection:",
        )
        venue_id = str(row["venue_id"])
        year = int(row["year"])
        next_check_at = _timestamp(
            str(row["next_check_at"]), field="stored selection next_check_at"
        )
        selected_at = _timestamp(
            str(row["selected_at"]), field="stored selection selected_at"
        )
        first_wakeup_id = str(row["first_wakeup_id"])
        _validate_scheduler_identity(
            first_wakeup_id,
            field="stored selection wakeup ID",
            prefix="scheduler-wakeup:",
        )
        state_revision = int(row["state_revision"])
        state_fingerprint = str(row["state_fingerprint"])
        expected_id = "due-selection:" + artifact_fingerprint({
            "venue_id": venue_id,
            "year": year,
            "next_check_at": next_check_at,
        })
        if selection_id != expected_id:
            raise StoredDataError("stored due-selection identity is invalid")
        state_row = self._connection.execute(
            "SELECT * FROM conference_state_history "
            "WHERE venue_id = ? AND year = ? AND revision = ?",
            (venue_id, year, state_revision),
        ).fetchone()
        if state_row is None:
            raise StoredDataError("stored due selection has no state revision")
        state_record = self._state_from_row(state_row)
        if (
            state_record.state_fingerprint != state_fingerprint
            or state_record.state["next_check_at"] != next_check_at
        ):
            raise StoredDataError("stored due selection does not match state")
        return DueWorkSelection(
            selection_id=selection_id,
            venue_id=venue_id,
            year=year,
            next_check_at=next_check_at,
            selected_at=selected_at,
            first_wakeup_id=first_wakeup_id,
            state_revision=state_revision,
            state_fingerprint=state_fingerprint,
        )

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

    def retain_existing_scraper_action(
        self,
        action: ActionIntent,
        *,
        source_verification_id: str,
        lease: LeaseHandle,
        enqueued_at: datetime | str,
    ) -> ExecutionRetentionOutcome:
        """Persist one verified queue_existing_scraper action and its job.

        The strict version-2 job is always recomputed from the action; a
        caller can never supply job bytes directly. Exact replay of the same
        action ID is a no-op; identity, evidence, or stored-content drift
        fails closed.
        """
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError(
                "execution dispatch requires local control ownership"
            )
        if not isinstance(action, ActionIntent):
            raise ExecutionQueueError("retained action must be an ActionIntent")
        if not isinstance(source_verification_id, str) or not source_verification_id:
            raise ExecutionQueueError("source_verification_id is required")
        if source_verification_id not in action.evidence_ids:
            raise ExecutionQueueError(
                "action does not cite the supplied source verification among "
                "its evidence"
            )
        try:
            job = build_scrape_job_from_action(action)
        except JobQueueError as exc:
            raise ExecutionQueueError(
                f"action cannot become a scrape job: {exc}"
            ) from exc
        action_payload = action.as_dict()
        assert_secret_free(action_payload)
        action_json = _canonical_json(action_payload)
        job_json = _canonical_json(job)
        action_fp = artifact_fingerprint(action_payload)
        job_fp = job["job_fingerprint"]
        enqueued_text = _timestamp(enqueued_at, field="execution enqueued_at")

        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            verification_row = connection.execute(
                "SELECT venue_id, year FROM verification_history "
                "WHERE verification_id = ?",
                (source_verification_id,),
            ).fetchone()
            if verification_row is None:
                raise ExecutionQueueError("source verification is not retained")
            if (
                str(verification_row["venue_id"]) != action.venue_id
                or int(verification_row["year"]) != action.year
            ):
                raise ExecutionQueueError(
                    "source verification venue/year does not match the action"
                )
            existing = connection.execute(
                "SELECT * FROM execution_job WHERE action_id = ?",
                (action.action_id,),
            ).fetchone()
            if existing is not None:
                record = self._execution_job_from_row(existing, connection)
                if (
                    record.job_id != job["job_id"]
                    or record.source_verification_id != source_verification_id
                    or record.action_fingerprint != action_fp
                    or record.job_fingerprint != job_fp
                ):
                    raise ExecutionQueueError(
                        "action ID already retained a different job"
                    )
                return ExecutionRetentionOutcome(record=record, applied=False)
            conflicting = connection.execute(
                "SELECT job_id FROM execution_job WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
            if conflicting is not None:
                raise ExecutionQueueError(
                    "recomputed job ID already belongs to a different action"
                )
            connection.execute(
                """
                INSERT INTO execution_job (
                    job_id, action_id, source_verification_id, venue_id, year,
                    enqueued_at, state, current_attempt_number,
                    action_fingerprint, job_fingerprint, action_json, job_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """,
                (
                    job["job_id"], action.action_id, source_verification_id,
                    action.venue_id, action.year, enqueued_text,
                    action_fp, job_fp, action_json, job_json,
                ),
            )
            row = connection.execute(
                "SELECT * FROM execution_job WHERE job_id = ?", (job["job_id"],)
            ).fetchone()
            record = self._execution_job_from_row(row, connection)
        return ExecutionRetentionOutcome(record=record, applied=True)

    def claim_next_execution_job(
        self,
        *,
        lease: LeaseHandle,
        claimed_at: datetime | str,
    ) -> ExecutionAttemptClaim | None:
        """Claim at most one pending job in stable enqueue/job-ID order."""
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError(
                "execution dispatch requires local control ownership"
            )
        claimed_text = _timestamp(claimed_at, field="execution claimed_at")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            row = connection.execute(
                "SELECT * FROM execution_job WHERE state = 'pending' "
                "ORDER BY enqueued_at, job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            record = self._execution_job_from_row(row, connection)
            attempt_number = record.current_attempt_number + 1
            claim_token = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO execution_attempt_history (
                    job_id, attempt_number, claim_token, started_at,
                    completed_at, disposition, status, failure_class,
                    reason_code, result_job_id, published, retry_permitted,
                    paper_count, valid_pdf_count
                ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL, NULL)
                """,
                (record.job_id, attempt_number, claim_token, claimed_text),
            )
            connection.execute(
                "UPDATE execution_job "
                "SET state = 'in_flight', current_attempt_number = ? "
                "WHERE job_id = ?",
                (attempt_number, record.job_id),
            )
        return ExecutionAttemptClaim(
            job_id=record.job_id,
            attempt_number=attempt_number,
            claim_token=claim_token,
            started_at=claimed_text,
            job=deepcopy(record.job),
        )

    def complete_execution_attempt(
        self,
        claim: ExecutionAttemptClaim,
        *,
        disposition: str,
        status: str,
        failure_class: str | None,
        reason_code: str,
        result_job_id: str | None,
        published: bool,
        retry_permitted: bool,
        paper_count: int | None,
        valid_pdf_count: int | None,
        lease: LeaseHandle,
        completed_at: datetime | str,
    ) -> ExecutionCompletionOutcome:
        """Durably close the current in-flight attempt exactly once.

        A ``retry`` disposition returns the job to ``pending`` for a new
        attempt with an incremented attempt number. A ``completed``
        disposition closes the job permanently. Any inability to prove the
        effect outcome (an exception, a stale claim, or a lost lease) must
        never reach this method; the attempt then stays durably ``in_flight``
        and is never reclaimed by elapsed time.
        """
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError(
                "execution dispatch requires local control ownership"
            )
        if not isinstance(claim, ExecutionAttemptClaim):
            raise ExecutionQueueError("claim must be an ExecutionAttemptClaim")
        if disposition not in {"retry", "completed"}:
            raise ExecutionQueueError(
                "execution disposition must be retry or completed"
            )
        if retry_permitted != (disposition == "retry"):
            raise ExecutionQueueError(
                "retry_permitted does not match the supplied disposition"
            )
        if not isinstance(status, str) or not status:
            raise ExecutionQueueError("execution status is required")
        if failure_class is not None and not isinstance(failure_class, str):
            raise ExecutionQueueError(
                "execution failure_class must be a string or None"
            )
        if not isinstance(reason_code, str) or not reason_code:
            raise ExecutionQueueError("execution reason_code is required")
        if result_job_id is not None and not isinstance(result_job_id, str):
            raise ExecutionQueueError("execution result_job_id must be a string or None")
        if published and result_job_id is None:
            raise ExecutionQueueError(
                "published execution output requires a result job ID"
            )
        for value in (paper_count, valid_pdf_count):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ExecutionQueueError(
                    "execution counts must be non-negative integers or None"
                )
        completed_text = _timestamp(completed_at, field="execution completed_at")

        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            job_row = connection.execute(
                "SELECT * FROM execution_job WHERE job_id = ?", (claim.job_id,)
            ).fetchone()
            if job_row is None:
                raise ExecutionQueueError("claimed execution job does not exist")
            record = self._execution_job_from_row(job_row, connection)
            if (
                record.state != "in_flight"
                or record.current_attempt_number != claim.attempt_number
            ):
                raise ExecutionQueueError(
                    "claim does not match the current in-flight attempt"
                )
            attempt_row = connection.execute(
                "SELECT * FROM execution_attempt_history "
                "WHERE job_id = ? AND attempt_number = ?",
                (claim.job_id, claim.attempt_number),
            ).fetchone()
            if attempt_row is None:
                raise StoredDataError("claimed execution attempt is missing")
            if (
                str(attempt_row["claim_token"]) != claim.claim_token
                or attempt_row["completed_at"] is not None
            ):
                raise ExecutionQueueError(
                    "execution claim token is stale or already completed"
                )
            started_at = _timestamp(
                str(attempt_row["started_at"]), field="stored execution started_at"
            )
            if _parse_timestamp(
                completed_text, field="execution completed_at"
            ) < _parse_timestamp(started_at, field="execution started_at"):
                raise ExecutionQueueError(
                    "execution completion time cannot precede its start"
                )
            connection.execute(
                """
                UPDATE execution_attempt_history
                SET completed_at = ?, disposition = ?, status = ?,
                    failure_class = ?, reason_code = ?, result_job_id = ?,
                    published = ?, retry_permitted = ?, paper_count = ?,
                    valid_pdf_count = ?
                WHERE job_id = ? AND attempt_number = ?
                """,
                (
                    completed_text, disposition, status, failure_class,
                    reason_code, result_job_id, int(published),
                    int(retry_permitted), paper_count, valid_pdf_count,
                    claim.job_id, claim.attempt_number,
                ),
            )
            new_state = "pending" if disposition == "retry" else "completed"
            connection.execute(
                "UPDATE execution_job SET state = ? WHERE job_id = ?",
                (new_state, claim.job_id),
            )
            completed_row = connection.execute(
                "SELECT * FROM execution_job WHERE job_id = ?", (claim.job_id,)
            ).fetchone()
            completed_record = self._execution_job_from_row(completed_row, connection)
            attempt_row = connection.execute(
                "SELECT * FROM execution_attempt_history "
                "WHERE job_id = ? AND attempt_number = ?",
                (claim.job_id, claim.attempt_number),
            ).fetchone()
            attempt = self._execution_attempt_from_row(attempt_row)
        return ExecutionCompletionOutcome(record=completed_record, attempt=attempt)

    def get_execution_job(self, job_id: str) -> ExecutionJobRecord | None:
        """Return one fully revalidated execution job for recovery inspection."""
        row = self._connection.execute(
            "SELECT * FROM execution_job WHERE job_id = ?", (job_id,)
        ).fetchone()
        return (
            self._execution_job_from_row(row, self._connection)
            if row is not None
            else None
        )

    def list_execution_jobs(
        self, *, state: str | None = None
    ) -> tuple[ExecutionJobRecord, ...]:
        """Return validated execution jobs in stable enqueue order."""
        if state is not None and state not in {"pending", "in_flight", "completed"}:
            raise ExecutionQueueError("execution job state filter is invalid")
        if state is None:
            rows = self._connection.execute(
                "SELECT * FROM execution_job ORDER BY enqueued_at, job_id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM execution_job WHERE state = ? "
                "ORDER BY enqueued_at, job_id",
                (state,),
            ).fetchall()
        return tuple(
            self._execution_job_from_row(row, self._connection) for row in rows
        )

    def execution_attempt_history(
        self, job_id: str
    ) -> tuple[ExecutionAttemptRecord, ...]:
        """Return validated dispatch attempts for one job in attempt order."""
        rows = self._connection.execute(
            "SELECT * FROM execution_attempt_history WHERE job_id = ? "
            "ORDER BY attempt_number",
            (job_id,),
        ).fetchall()
        return tuple(self._execution_attempt_from_row(row) for row in rows)

    def _execution_job_from_row(
        self,
        row: sqlite3.Row,
        connection: sqlite3.Connection,
    ) -> ExecutionJobRecord:
        job_id = str(row["job_id"])
        action_id = str(row["action_id"])
        source_verification_id = str(row["source_verification_id"])
        venue_id = str(row["venue_id"])
        year = int(row["year"])
        enqueued_at = _timestamp(
            str(row["enqueued_at"]), field="stored execution enqueued_at"
        )
        state = str(row["state"])
        if state not in {"pending", "in_flight", "completed"}:
            raise StoredDataError("stored execution job state is invalid")
        current_attempt_number = int(row["current_attempt_number"])
        if current_attempt_number < 0:
            raise StoredDataError("stored execution attempt number is invalid")
        action_payload = _decode_json(row["action_json"], label="execution action")
        job = _decode_json(row["job_json"], label="execution job")
        if artifact_fingerprint(action_payload) != row["action_fingerprint"]:
            raise StoredDataError(
                "stored execution action fingerprint does not match"
            )
        try:
            validate_job_identity(job)
        except JobQueueError as exc:
            raise StoredDataError(f"stored execution job is invalid: {exc}") from exc
        if (
            job.get("job_fingerprint") != row["job_fingerprint"]
            or job.get("job_id") != job_id
        ):
            raise StoredDataError("stored execution job fingerprint does not match")
        try:
            action = action_intent_from_payload(action_payload)
        except Exception as exc:
            raise StoredDataError(
                f"stored execution action is invalid: {exc}"
            ) from exc
        if (
            action.action_id != action_id
            or action.venue_id != venue_id
            or action.year != year
            or source_verification_id not in action.evidence_ids
        ):
            raise StoredDataError("stored execution action identity does not match")
        try:
            recomputed_job = build_scrape_job_from_action(action)
        except JobQueueError as exc:
            raise StoredDataError(
                f"stored execution action cannot rebuild its job: {exc}"
            ) from exc
        if recomputed_job != job or recomputed_job["job_id"] != job_id:
            raise StoredDataError("stored execution job does not match its action")

        counts = connection.execute(
            "SELECT COUNT(*) AS attempts, MAX(attempt_number) AS max_attempt "
            "FROM execution_attempt_history WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        stored_attempts = int(counts["attempts"])
        max_attempt = 0 if counts["max_attempt"] is None else int(counts["max_attempt"])
        if stored_attempts != max_attempt or max_attempt != current_attempt_number:
            raise StoredDataError(
                "stored execution attempt count does not match its history"
            )
        if state in {"in_flight", "completed"} or (
            state == "pending" and current_attempt_number > 0
        ):
            latest = connection.execute(
                "SELECT completed_at, disposition FROM execution_attempt_history "
                "WHERE job_id = ? AND attempt_number = ?",
                (job_id, current_attempt_number),
            ).fetchone()
            if latest is None:
                raise StoredDataError("stored execution job has no matching attempt")
            if state == "in_flight" and latest["completed_at"] is not None:
                raise StoredDataError(
                    "stored in-flight execution job attempt is already closed"
                )
            if state == "completed" and (
                latest["completed_at"] is None
                or str(latest["disposition"]) != "completed"
            ):
                raise StoredDataError(
                    "stored completed execution job attempt does not match"
                )
            if state == "pending" and current_attempt_number > 0 and (
                latest["completed_at"] is None
                or str(latest["disposition"]) != "retry"
            ):
                raise StoredDataError(
                    "stored pending execution job attempt does not match"
                )
        return ExecutionJobRecord(
            job_id=job_id,
            action_id=action_id,
            source_verification_id=source_verification_id,
            venue_id=venue_id,
            year=year,
            enqueued_at=enqueued_at,
            state=state,
            current_attempt_number=current_attempt_number,
            action_fingerprint=str(row["action_fingerprint"]),
            job_fingerprint=str(row["job_fingerprint"]),
            action=deepcopy(action_payload),
            job=deepcopy(job),
        )

    def _execution_attempt_from_row(
        self, row: sqlite3.Row
    ) -> ExecutionAttemptRecord:
        job_id = str(row["job_id"])
        attempt_number = int(row["attempt_number"])
        if attempt_number < 1:
            raise StoredDataError("stored execution attempt number is invalid")
        started_at = _timestamp(
            str(row["started_at"]), field="stored execution started_at"
        )
        completed_at = (
            None
            if row["completed_at"] is None
            else _timestamp(
                str(row["completed_at"]), field="stored execution completed_at"
            )
        )
        closed_fields = (
            row["disposition"], row["status"], row["reason_code"],
        )
        if completed_at is None:
            if (
                any(value is not None for value in closed_fields)
                or row["published"] is not None
                or row["retry_permitted"] is not None
            ):
                raise StoredDataError(
                    "stored in-flight execution attempt has completion data"
                )
        else:
            if (
                any(value is None for value in closed_fields)
                or row["published"] is None
                or row["retry_permitted"] is None
            ):
                raise StoredDataError(
                    "stored closed execution attempt is incomplete"
                )
            if str(row["disposition"]) not in {"retry", "completed"}:
                raise StoredDataError("stored execution disposition is invalid")
            if _parse_timestamp(
                completed_at, field="stored execution completed_at"
            ) < _parse_timestamp(started_at, field="stored execution started_at"):
                raise StoredDataError("stored execution attempt regresses")
        return ExecutionAttemptRecord(
            job_id=job_id,
            attempt_number=attempt_number,
            claim_token=str(row["claim_token"]),
            started_at=started_at,
            completed_at=completed_at,
            disposition=(
                None if row["disposition"] is None else str(row["disposition"])
            ),
            status=None if row["status"] is None else str(row["status"]),
            failure_class=(
                None if row["failure_class"] is None else str(row["failure_class"])
            ),
            reason_code=(
                None if row["reason_code"] is None else str(row["reason_code"])
            ),
            result_job_id=(
                None if row["result_job_id"] is None else str(row["result_job_id"])
            ),
            published=(
                None if row["published"] is None else bool(row["published"])
            ),
            retry_permitted=(
                None
                if row["retry_permitted"] is None
                else bool(row["retry_permitted"])
            ),
            paper_count=(
                None if row["paper_count"] is None else int(row["paper_count"])
            ),
            valid_pdf_count=(
                None
                if row["valid_pdf_count"] is None
                else int(row["valid_pdf_count"])
            ),
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
