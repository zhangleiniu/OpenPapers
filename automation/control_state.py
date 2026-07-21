"""Durable single-writer storage for the local automation control plane."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from automation.contracts import artifact_fingerprint
from automation.domain import (
    ArtifactKind,
    OwnershipError,
    Writer,
    assert_secret_free,
    assert_writer_allowed,
)
from automation.notifications import (
    FailureCategory,
    validate_receipt_id,
)


CONTROL_SCHEMA_VERSION = 11
DEFAULT_LEASE_TTL_SECONDS = 300
MAX_LEASE_TTL_SECONDS = 86_400
MAX_SELECTION_LIMIT = 1000
_CONTROL_LEASE_NAME = "control-state"


class ControlStateError(RuntimeError):
    """Base class for durable control-state failures."""


class SchemaMigrationError(ControlStateError):
    """Raised when the database cannot be migrated or validated safely."""


class LeaseConflictError(ControlStateError):
    """Raised when another unexpired control-plane lease exists."""


class LeaseLostError(ControlStateError):
    """Raised when a lease token is missing, expired, or replaced."""


class EventDateScheduleError(ControlStateError):
    """Raised when approximate-date scheduling cannot proceed safely."""


class AgentScheduleError(ControlStateError):
    """Raised when an agent schedule or run transition is unsafe."""


class AgentArtifactError(ControlStateError):
    """Raised when an agent execution artifact transition is unsafe."""


class AgentRunReportError(ControlStateError):
    """Raised when an agent-run report delivery transition is unsafe."""


class StoredDataError(ControlStateError):
    """Raised when persisted JSON, identity, or fingerprints are corrupt."""


@dataclass(frozen=True)
class LeaseHandle:
    """Opaque authority for one unexpired control-plane writer."""

    owner_id: str
    token: str
    expires_at: str


@dataclass(frozen=True)
class EventDateScheduleRecord:
    """Current durable approximate-date state for one venue/year."""

    venue_id: str
    year: int
    status: str
    next_check_at: str
    estimated_event_date: str | None
    estimated_at: str | None
    provider_name: str | None
    provider_model: str | None
    prompt_version: str | None
    attempt_count: int
    active_attempt_id: str | None
    last_failure_category: str | None
    updated_at: str


@dataclass(frozen=True)
class EventDateScheduleWriteOutcome:
    """Result of idempotently registering one event-date target."""

    record: EventDateScheduleRecord
    applied: bool


@dataclass(frozen=True)
class EventDateAttemptClaim:
    """Opaque authority for one in-flight approximate-date provider call."""

    attempt_id: str
    venue_id: str
    year: int
    attempt_number: int
    started_at: str
    provider_name: str
    provider_model: str
    prompt_version: str


@dataclass(frozen=True)
class EventDateAttemptRecord:
    """One immutable-numbered approximate-date lookup attempt."""

    attempt_id: str
    venue_id: str
    year: int
    attempt_number: int
    started_at: str
    completed_at: str | None
    outcome: str
    provider_name: str
    provider_model: str
    prompt_version: str
    estimated_event_date: str | None
    failure_category: str | None


@dataclass(frozen=True)
class AgentScheduleRecord:
    """Current durable coding-agent schedule for one venue/year."""

    venue_id: str
    year: int
    status: str
    next_check_at: str | None
    attempt_count: int
    active_run_id: str | None
    consecutive_failures: int
    last_disposition: str | None
    last_run_at: str | None
    suggested_retry_at: str | None
    last_gate_reason: str | None
    updated_at: str


@dataclass(frozen=True)
class AgentRunClaim:
    """Opaque authority for one retained in-flight coding-agent run."""

    run_id: str
    venue_id: str
    year: int
    attempt_number: int
    started_at: str


@dataclass(frozen=True)
class AgentRunAttemptRecord:
    """One immutable-numbered coding-agent run attempt."""

    run_id: str
    venue_id: str
    year: int
    attempt_number: int
    started_at: str
    completed_at: str | None
    disposition: str
    explanation: str | None
    suggested_retry_at: str | None
    failure_category: str | None


@dataclass(frozen=True)
class AgentRunClaimOutcome:
    """One claim, idle result, or durable policy-gate deferral."""

    claim: AgentRunClaim | None
    schedule: AgentScheduleRecord | None
    reason: str


@dataclass(frozen=True)
class AgentScheduleHintOutcome:
    """Result of applying one non-authoritative scheduling hint."""

    schedule: AgentScheduleRecord | None
    applied: bool
    reason: str


@dataclass(frozen=True)
class AgentExecutionArtifactRecord:
    """Durable review and retention state for one managed agent worktree."""

    run_id: str
    lifecycle: str
    runs_root: str
    worktree_path: str
    branch_name: str
    base_commit: str
    started_at: str
    completed_at: str | None
    changed_files: tuple[str, ...]
    returncode: int | None
    timed_out: bool
    retention_status: str
    removed_at: str | None
    removal_failure: str | None


@dataclass(frozen=True)
class AgentRunReportRecord:
    """One replay-safe email report derived from a terminal agent run."""

    report_id: str
    run_id: str
    status: str
    schedule_status: str
    next_check_at: str | None
    attempt_count: int
    created_at: str
    updated_at: str
    delivered_at: str | None
    last_failure_category: str | None
    receipt_id: str | None


@dataclass(frozen=True)
class AgentRunReportAttemptRecord:
    """One numbered delivery attempt for an agent-run report."""

    report_id: str
    attempt_number: int
    started_at: str
    completed_at: str | None
    outcome: str
    failure_category: str | None
    receipt_id: str | None


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

_MIGRATION_8 = (
    """
    CREATE TABLE event_date_schedule (
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'scheduled')),
        next_check_at TEXT NOT NULL,
        estimated_event_date TEXT,
        estimated_at TEXT,
        provider_name TEXT,
        provider_model TEXT,
        prompt_version TEXT,
        attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
        active_attempt_id TEXT,
        last_failure_category TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (venue_id, year),
        CHECK (
            (status = 'pending' AND estimated_event_date IS NULL
                AND estimated_at IS NULL AND provider_name IS NULL
                AND provider_model IS NULL AND prompt_version IS NULL
                AND active_attempt_id IS NULL)
            OR
            (status = 'active' AND estimated_event_date IS NULL
                AND estimated_at IS NULL AND provider_name IS NULL
                AND provider_model IS NULL AND prompt_version IS NULL
                AND active_attempt_id IS NOT NULL)
            OR
            (status = 'scheduled' AND estimated_event_date IS NOT NULL
                AND estimated_at IS NOT NULL AND provider_name IS NOT NULL
                AND provider_model IS NOT NULL AND prompt_version IS NOT NULL
                AND active_attempt_id IS NULL
                AND last_failure_category IS NULL)
        )
    )
    """,
    """
    CREATE INDEX event_date_schedule_due
    ON event_date_schedule (status, next_check_at, venue_id, year)
    """,
    """
    CREATE TABLE event_date_attempt (
        attempt_id TEXT PRIMARY KEY,
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
        started_at TEXT NOT NULL,
        completed_at TEXT,
        outcome TEXT NOT NULL CHECK (outcome IN ('active', 'scheduled', 'retry')),
        provider_name TEXT NOT NULL,
        provider_model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        estimated_event_date TEXT,
        failure_category TEXT,
        UNIQUE (venue_id, year, attempt_number),
        FOREIGN KEY (venue_id, year)
            REFERENCES event_date_schedule (venue_id, year),
        CHECK (
            (outcome = 'active' AND completed_at IS NULL
                AND estimated_event_date IS NULL AND failure_category IS NULL)
            OR
            (outcome = 'scheduled' AND completed_at IS NOT NULL
                AND estimated_event_date IS NOT NULL AND failure_category IS NULL)
            OR
            (outcome = 'retry' AND completed_at IS NOT NULL
                AND estimated_event_date IS NULL AND failure_category IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX event_date_attempt_target
    ON event_date_attempt (venue_id, year, attempt_number)
    """,
)

_MIGRATION_9 = (
    """
    CREATE TABLE agent_schedule (
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'scheduled', 'active', 'completed', 'needs_human', 'paused'
        )),
        next_check_at TEXT,
        attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
        active_run_id TEXT,
        consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
        last_disposition TEXT CHECK (last_disposition IN (
            'success', 'not_ready', 'needs_human', 'failed'
        )),
        last_run_at TEXT,
        suggested_retry_at TEXT,
        last_gate_reason TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (venue_id, year),
        FOREIGN KEY (venue_id, year)
            REFERENCES event_date_schedule (venue_id, year),
        CHECK (
            (status = 'scheduled' AND next_check_at IS NOT NULL
                AND active_run_id IS NULL)
            OR
            (status = 'active' AND next_check_at IS NULL
                AND active_run_id IS NOT NULL)
            OR
            (status IN ('completed', 'needs_human', 'paused')
                AND next_check_at IS NULL AND active_run_id IS NULL)
        )
    )
    """,
    """
    CREATE INDEX agent_schedule_due
    ON agent_schedule (status, next_check_at, venue_id, year)
    """,
    """
    CREATE TABLE agent_run_attempt (
        run_id TEXT PRIMARY KEY,
        venue_id TEXT NOT NULL,
        year INTEGER NOT NULL,
        attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
        started_at TEXT NOT NULL,
        completed_at TEXT,
        disposition TEXT NOT NULL CHECK (disposition IN (
            'active', 'success', 'not_ready', 'needs_human', 'failed'
        )),
        explanation TEXT,
        suggested_retry_at TEXT,
        failure_category TEXT,
        UNIQUE (venue_id, year, attempt_number),
        FOREIGN KEY (venue_id, year)
            REFERENCES agent_schedule (venue_id, year),
        CHECK (
            (disposition = 'active' AND completed_at IS NULL
                AND explanation IS NULL AND suggested_retry_at IS NULL
                AND failure_category IS NULL)
            OR
            (disposition IN ('success', 'not_ready', 'needs_human')
                AND completed_at IS NOT NULL AND explanation IS NOT NULL
                AND failure_category IS NULL)
            OR
            (disposition = 'failed' AND completed_at IS NOT NULL
                AND explanation IS NOT NULL AND failure_category IS NOT NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX agent_one_active_run
    ON agent_run_attempt (disposition) WHERE disposition = 'active'
    """,
    """
    CREATE INDEX agent_run_attempt_started
    ON agent_run_attempt (started_at, venue_id, year)
    """,
    """
    INSERT INTO agent_schedule (
        venue_id, year, status, next_check_at, attempt_count,
        active_run_id, consecutive_failures, last_disposition,
        last_run_at, suggested_retry_at, last_gate_reason, updated_at
    )
    SELECT venue_id, year, 'scheduled', next_check_at, 0,
        NULL, 0, NULL, NULL, NULL, NULL, updated_at
    FROM event_date_schedule WHERE status = 'scheduled'
    """,
)

_MIGRATION_10 = (
    """
    CREATE TABLE agent_execution_artifact (
        run_id TEXT PRIMARY KEY,
        lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'terminal')),
        runs_root TEXT NOT NULL,
        worktree_path TEXT NOT NULL UNIQUE,
        branch_name TEXT NOT NULL UNIQUE,
        base_commit TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        changed_files_json TEXT,
        returncode INTEGER,
        timed_out INTEGER NOT NULL CHECK (timed_out IN (0, 1)),
        retention_status TEXT NOT NULL CHECK (retention_status IN (
            'retained', 'removed', 'removal_failed'
        )),
        removed_at TEXT,
        removal_failure TEXT,
        FOREIGN KEY (run_id) REFERENCES agent_run_attempt (run_id),
        CHECK (
            (lifecycle = 'active' AND completed_at IS NULL
                AND changed_files_json IS NULL AND returncode IS NULL
                AND timed_out = 0 AND retention_status = 'retained'
                AND removed_at IS NULL AND removal_failure IS NULL)
            OR
            (lifecycle = 'terminal' AND completed_at IS NOT NULL
                AND changed_files_json IS NOT NULL)
        ),
        CHECK (
            (retention_status = 'retained' AND removed_at IS NULL
                AND removal_failure IS NULL)
            OR
            (retention_status = 'removed' AND removed_at IS NOT NULL
                AND removal_failure IS NULL)
            OR
            (retention_status = 'removal_failed' AND removed_at IS NULL
                AND removal_failure IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX agent_execution_retention
    ON agent_execution_artifact (
        lifecycle, retention_status, completed_at, run_id
    )
    """,
    """
    CREATE TABLE agent_run_report (
        report_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (status IN (
            'pending', 'in_flight', 'retryable', 'delivered',
            'permanent_failure'
        )),
        schedule_status TEXT NOT NULL CHECK (schedule_status IN (
            'scheduled', 'completed', 'needs_human', 'paused'
        )),
        next_check_at TEXT,
        attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        delivered_at TEXT,
        last_failure_category TEXT,
        receipt_id TEXT,
        FOREIGN KEY (run_id) REFERENCES agent_run_attempt (run_id),
        CHECK (
            (schedule_status = 'scheduled' AND next_check_at IS NOT NULL)
            OR
            (schedule_status IN ('completed', 'needs_human', 'paused')
                AND next_check_at IS NULL)
        ),
        CHECK (
            (status IN ('pending', 'in_flight') AND delivered_at IS NULL
                AND last_failure_category IS NULL AND receipt_id IS NULL)
            OR
            (status = 'retryable' AND delivered_at IS NULL
                AND last_failure_category IS NOT NULL AND receipt_id IS NULL)
            OR
            (status = 'permanent_failure' AND delivered_at IS NULL
                AND last_failure_category IS NOT NULL AND receipt_id IS NULL)
            OR
            (status = 'delivered' AND delivered_at IS NOT NULL
                AND last_failure_category IS NULL AND receipt_id IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE agent_run_report_attempt (
        report_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
        started_at TEXT NOT NULL,
        completed_at TEXT,
        outcome TEXT NOT NULL CHECK (outcome IN (
            'active', 'retryable', 'delivered', 'permanent_failure'
        )),
        failure_category TEXT,
        receipt_id TEXT,
        PRIMARY KEY (report_id, attempt_number),
        FOREIGN KEY (report_id) REFERENCES agent_run_report (report_id),
        CHECK (
            (outcome = 'active' AND completed_at IS NULL
                AND failure_category IS NULL AND receipt_id IS NULL)
            OR
            (outcome IN ('retryable', 'permanent_failure')
                AND completed_at IS NOT NULL AND failure_category IS NOT NULL
                AND receipt_id IS NULL)
            OR
            (outcome = 'delivered' AND completed_at IS NOT NULL
                AND failure_category IS NULL AND receipt_id IS NOT NULL)
        )
    )
    """,
)

_MIGRATION_11 = tuple(
    f"DROP TABLE {table}"
    for table in (
        "execution_attempt_history",
        "execution_job",
        "scheduler_wakeup_plan",
        "scheduler_due_selection",
        "scheduler_wakeup",
        "notification_attempt_history",
        "notification_source",
        "notification_intent",
        "case_event_history",
        "case_state_current",
        "case_state_history",
        "job_result_consumption",
        "conference_state_current",
        "conference_state_history",
        "verification_history",
    )
)

_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
    6: _MIGRATION_6,
    7: _MIGRATION_7,
    8: _MIGRATION_8,
    9: _MIGRATION_9,
    10: _MIGRATION_10,
    11: _MIGRATION_11,
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

_REQUIRED_COLUMNS_V8 = {
    "event_date_schedule": {
        "venue_id", "year", "status", "next_check_at",
        "estimated_event_date", "estimated_at", "provider_name",
        "provider_model", "prompt_version", "attempt_count",
        "active_attempt_id", "last_failure_category", "updated_at",
    },
    "event_date_attempt": {
        "attempt_id", "venue_id", "year", "attempt_number", "started_at",
        "completed_at", "outcome", "provider_name", "provider_model",
        "prompt_version", "estimated_event_date", "failure_category",
    },
}

_REQUIRED_COLUMNS_V9 = {
    "agent_schedule": {
        "venue_id", "year", "status", "next_check_at", "attempt_count",
        "active_run_id", "consecutive_failures", "last_disposition",
        "last_run_at", "suggested_retry_at", "last_gate_reason", "updated_at",
    },
    "agent_run_attempt": {
        "run_id", "venue_id", "year", "attempt_number", "started_at",
        "completed_at", "disposition", "explanation", "suggested_retry_at",
        "failure_category",
    },
}

_REQUIRED_COLUMNS_V10 = {
    "agent_execution_artifact": {
        "run_id", "lifecycle", "runs_root", "worktree_path", "branch_name",
        "base_commit", "started_at", "completed_at", "changed_files_json",
        "returncode", "timed_out", "retention_status", "removed_at",
        "removal_failure",
    },
    "agent_run_report": {
        "report_id", "run_id", "status", "schedule_status", "next_check_at",
        "attempt_count", "created_at", "updated_at", "delivered_at",
        "last_failure_category", "receipt_id",
    },
    "agent_run_report_attempt": {
        "report_id", "attempt_number", "started_at", "completed_at",
        "outcome", "failure_category", "receipt_id",
    },
}

_REQUIRED_COLUMNS_V11 = {
    "schema_migrations": _REQUIRED_COLUMNS_V1["schema_migrations"],
    "control_lease": _REQUIRED_COLUMNS_V1["control_lease"],
    "control_ownership": _REQUIRED_COLUMNS_V5["control_ownership"],
    **_REQUIRED_COLUMNS_V8,
    **_REQUIRED_COLUMNS_V9,
    **_REQUIRED_COLUMNS_V10,
}

_REQUIRED_COLUMNS_BY_VERSION = {
    1: _REQUIRED_COLUMNS_V1,
    2: _REQUIRED_COLUMNS_V2,
    3: _REQUIRED_COLUMNS_V3,
    4: _REQUIRED_COLUMNS_V4,
    5: _REQUIRED_COLUMNS_V5,
    6: _REQUIRED_COLUMNS_V6,
    7: _REQUIRED_COLUMNS_V7,
    8: _REQUIRED_COLUMNS_V8,
    9: _REQUIRED_COLUMNS_V9,
    10: _REQUIRED_COLUMNS_V10,
    11: _REQUIRED_COLUMNS_V11,
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


def _validate_selection_limit(value: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_SELECTION_LIMIT
    ):
        raise ControlStateError(
            "selection limit must be between 1 and "
            f"{MAX_SELECTION_LIMIT}"
        )


def _validate_event_date_target(venue_id: str, year: int) -> None:
    if (
        not isinstance(venue_id, str)
        or not 2 <= len(venue_id) <= 32
        or not venue_id[0].isalnum()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                for character in venue_id)
    ):
        raise EventDateScheduleError("event-date venue_id is invalid")
    if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2200:
        raise EventDateScheduleError("event-date year is invalid")


def _bounded_event_text(value: str, *, field: str, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character in value for character in ("\x00", "\n", "\r"))
    ):
        raise EventDateScheduleError(f"{field} is invalid")
    return value


def _event_date(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise EventDateScheduleError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise EventDateScheduleError(f"{field} must be an ISO date") from exc
    canonical = parsed.isoformat()
    if canonical != value:
        raise EventDateScheduleError(f"{field} must be a canonical ISO date")
    return canonical


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
        if version == 11:
            required_columns.update(_REQUIRED_COLUMNS_V11)
        else:
            for migration_version in range(1, version + 1):
                required_columns.update(
                    _REQUIRED_COLUMNS_BY_VERSION[migration_version]
                )
        missing_tables = set(required_columns) - tables
        if missing_tables:
            raise SchemaMigrationError(
                f"control schema is missing tables: {sorted(missing_tables)}"
            )
        if version == 11 and tables != set(required_columns):
            raise SchemaMigrationError(
                "control schema has unexpected tables: "
                f"{sorted(tables - set(required_columns))}"
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


    def register_event_date_target(
        self,
        venue_id: str,
        year: int,
        *,
        registered_at: datetime | str,
        lease: LeaseHandle,
    ) -> EventDateScheduleWriteOutcome:
        """Idempotently register one venue/year for approximate-date lookup."""
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError("event-date schedules require local ownership")
        _validate_event_date_target(venue_id, year)
        registered = _timestamp(registered_at, field="event-date registered_at")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            existing = connection.execute(
                "SELECT * FROM event_date_schedule WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
            if existing is not None:
                return EventDateScheduleWriteOutcome(
                    self._event_date_schedule_from_row(existing), applied=False
                )
            connection.execute(
                """
                INSERT INTO event_date_schedule (
                    venue_id, year, status, next_check_at,
                    estimated_event_date, estimated_at, provider_name,
                    provider_model, prompt_version, attempt_count,
                    active_attempt_id, last_failure_category, updated_at
                ) VALUES (?, ?, 'pending', ?, NULL, NULL, NULL, NULL, NULL,
                    0, NULL, NULL, ?)
                """,
                (venue_id, year, registered, registered),
            )
            row = connection.execute(
                "SELECT * FROM event_date_schedule WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
        return EventDateScheduleWriteOutcome(
            self._event_date_schedule_from_row(row), applied=True
        )

    def list_due_event_date_schedules(
        self,
        due_at: datetime | str,
        *,
        limit: int = 1,
    ) -> tuple[EventDateScheduleRecord, ...]:
        """Return bounded pending date lookups without claiming an effect."""
        _validate_selection_limit(limit)
        due = _timestamp(due_at, field="event-date due_at")
        rows = self._connection.execute(
            "SELECT * FROM event_date_schedule "
            "WHERE status = 'pending' AND next_check_at <= ? "
            "ORDER BY next_check_at, venue_id, year LIMIT ?",
            (due, limit),
        ).fetchall()
        return tuple(self._event_date_schedule_from_row(row) for row in rows)

    def event_date_attempt_count(
        self, *, started_at_or_after: datetime | str, started_before: datetime | str
    ) -> int:
        """Count immutable date lookups in one validated half-open window."""
        start = _timestamp(started_at_or_after, field="event-date count start")
        end = _timestamp(started_before, field="event-date count end")
        if _parse_timestamp(start, field="event-date count start") >= \
                _parse_timestamp(end, field="event-date count end"):
            raise EventDateScheduleError("event-date count window is invalid")
        return int(self._connection.execute(
            "SELECT COUNT(*) FROM event_date_attempt "
            "WHERE started_at >= ? AND started_at < ?",
            (start, end),
        ).fetchone()[0])

    def defer_event_date_schedule(
        self,
        venue_id: str,
        year: int,
        *,
        retry_at: datetime | str,
        deferred_at: datetime | str,
        failure_category: str,
        lease: LeaseHandle,
    ) -> EventDateScheduleRecord:
        """Move one pending lookup forward without creating an attempt."""
        _validate_event_date_target(venue_id, year)
        retry = _timestamp(retry_at, field="event-date deferred retry_at")
        deferred = _timestamp(deferred_at, field="event-date deferred_at")
        failure = _bounded_event_text(
            failure_category, field="event-date deferral category", maximum=200
        )
        if _parse_timestamp(retry, field="event-date deferred retry_at") <= \
                _parse_timestamp(deferred, field="event-date deferred_at"):
            raise EventDateScheduleError("event-date deferral must be in the future")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            cursor = connection.execute(
                "UPDATE event_date_schedule SET next_check_at = ?, "
                "last_failure_category = ?, updated_at = ? "
                "WHERE venue_id = ? AND year = ? AND status = 'pending'",
                (retry, failure, deferred, venue_id, year),
            )
            if cursor.rowcount != 1:
                raise EventDateScheduleError("event-date schedule is not pending")
        record = self.get_event_date_schedule(venue_id, year)
        if record is None:
            raise EventDateScheduleError("deferred event-date schedule disappeared")
        return record

    def claim_event_date_attempt(
        self,
        venue_id: str,
        year: int,
        *,
        provider_name: str,
        provider_model: str,
        prompt_version: str,
        claimed_at: datetime | str,
        lease: LeaseHandle,
    ) -> EventDateAttemptClaim:
        """Durably claim one due provider call before crossing that boundary."""
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError("event-date schedules require local ownership")
        _validate_event_date_target(venue_id, year)
        provider_name = _bounded_event_text(
            provider_name, field="event-date provider name"
        )
        provider_model = _bounded_event_text(
            provider_model, field="event-date provider model"
        )
        prompt_version = _bounded_event_text(
            prompt_version, field="event-date prompt version", maximum=50
        )
        claimed = _timestamp(claimed_at, field="event-date claimed_at")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            row = connection.execute(
                "SELECT * FROM event_date_schedule WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
            if row is None:
                raise EventDateScheduleError("event-date target is not registered")
            record = self._event_date_schedule_from_row(row)
            if record.status == "active":
                raise EventDateScheduleError(
                    "event-date attempt is active or ambiguously interrupted"
                )
            if record.status != "pending":
                raise EventDateScheduleError("event-date target is already scheduled")
            if _parse_timestamp(
                record.next_check_at, field="stored event-date next_check_at"
            ) > _parse_timestamp(claimed, field="event-date claimed_at"):
                raise EventDateScheduleError("event-date target is not due")
            attempt_number = record.attempt_count + 1
            attempt_id = "event-date-attempt:" + artifact_fingerprint({
                "venue_id": venue_id,
                "year": year,
                "attempt_number": attempt_number,
            })
            connection.execute(
                """
                INSERT INTO event_date_attempt (
                    attempt_id, venue_id, year, attempt_number, started_at,
                    completed_at, outcome, provider_name, provider_model,
                    prompt_version, estimated_event_date, failure_category
                ) VALUES (?, ?, ?, ?, ?, NULL, 'active', ?, ?, ?, NULL, NULL)
                """,
                (
                    attempt_id, venue_id, year, attempt_number, claimed,
                    provider_name, provider_model, prompt_version,
                ),
            )
            connection.execute(
                "UPDATE event_date_schedule SET status = 'active', "
                "attempt_count = ?, active_attempt_id = ?, "
                "last_failure_category = NULL, updated_at = ? "
                "WHERE venue_id = ? AND year = ? AND status = 'pending'",
                (attempt_number, attempt_id, claimed, venue_id, year),
            )
        return EventDateAttemptClaim(
            attempt_id=attempt_id,
            venue_id=venue_id,
            year=year,
            attempt_number=attempt_number,
            started_at=claimed,
            provider_name=provider_name,
            provider_model=provider_model,
            prompt_version=prompt_version,
        )

    def complete_event_date_success(
        self,
        claim: EventDateAttemptClaim,
        *,
        estimated_event_date: str,
        estimated_at: datetime | str,
        next_check_at: datetime | str,
        lease: LeaseHandle,
    ) -> EventDateScheduleRecord:
        """Close one claimed lookup with a date and future agent-check time."""
        event_date = _event_date(
            estimated_event_date, field="estimated event date"
        )
        estimated = _timestamp(estimated_at, field="event-date estimated_at")
        next_check = _timestamp(next_check_at, field="event-date next_check_at")
        if _parse_timestamp(next_check, field="event-date next_check_at") < \
                _parse_timestamp(estimated, field="event-date estimated_at"):
            raise EventDateScheduleError(
                "event-date next_check_at cannot precede estimation"
            )
        with self._write_transaction() as connection:
            self._require_event_date_claim(connection, claim, lease)
            connection.execute(
                "UPDATE event_date_attempt SET completed_at = ?, "
                "outcome = 'scheduled', estimated_event_date = ? "
                "WHERE attempt_id = ? AND outcome = 'active'",
                (estimated, event_date, claim.attempt_id),
            )
            connection.execute(
                "UPDATE event_date_schedule SET status = 'scheduled', "
                "next_check_at = ?, estimated_event_date = ?, estimated_at = ?, "
                "provider_name = ?, provider_model = ?, prompt_version = ?, "
                "active_attempt_id = NULL, last_failure_category = NULL, "
                "updated_at = ? WHERE venue_id = ? AND year = ? "
                "AND status = 'active' AND active_attempt_id = ?",
                (
                    next_check, event_date, estimated, claim.provider_name,
                    claim.provider_model, claim.prompt_version, estimated,
                    claim.venue_id, claim.year, claim.attempt_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO agent_schedule (
                    venue_id, year, status, next_check_at, attempt_count,
                    active_run_id, consecutive_failures, last_disposition,
                    last_run_at, suggested_retry_at, last_gate_reason, updated_at
                ) VALUES (?, ?, 'scheduled', ?, 0, NULL, 0, NULL, NULL,
                    NULL, NULL, ?)
                ON CONFLICT (venue_id, year) DO NOTHING
                """,
                (claim.venue_id, claim.year, next_check, estimated),
            )
            row = connection.execute(
                "SELECT * FROM event_date_schedule WHERE venue_id = ? AND year = ?",
                (claim.venue_id, claim.year),
            ).fetchone()
        return self._event_date_schedule_from_row(row)

    def register_continuous_event_date(
        self,
        venue_id: str,
        year: int,
        *,
        registered_at: datetime | str,
        lease: LeaseHandle,
    ) -> EventDateScheduleWriteOutcome:
        """Idempotently register one continuous-lifecycle venue/year with a
        placeholder 'scheduled' event_date_schedule row.

        A continuous venue (e.g. a journal with no discrete edition) has no
        event date to discover, so it never goes through the normal
        register/claim/complete Gemini flow. This exists purely to satisfy
        the ``agent_schedule`` foreign key: only ``agent_schedule.
        next_check_at`` drives real scheduling for this venue/year going
        forward. The placeholder's ``estimated_event_date``/``provider_name``
        are clearly marked (``'continuous_lifecycle'``) as a registration-time
        placeholder, never a real estimate.
        """
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError("event-date schedules require local ownership")
        _validate_event_date_target(venue_id, year)
        registered = _timestamp(registered_at, field="event-date registered_at")
        placeholder_date = registered[:10]
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            existing = connection.execute(
                "SELECT * FROM event_date_schedule WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
            if existing is not None:
                return EventDateScheduleWriteOutcome(
                    self._event_date_schedule_from_row(existing), applied=False
                )
            connection.execute(
                """
                INSERT INTO event_date_schedule (
                    venue_id, year, status, next_check_at,
                    estimated_event_date, estimated_at, provider_name,
                    provider_model, prompt_version, attempt_count,
                    active_attempt_id, last_failure_category, updated_at
                ) VALUES (?, ?, 'scheduled', ?, ?, ?, 'continuous_lifecycle',
                    'continuous_lifecycle', 'continuous_lifecycle', 0, NULL,
                    NULL, ?)
                """,
                (venue_id, year, registered, placeholder_date, registered, registered),
            )
            row = connection.execute(
                "SELECT * FROM event_date_schedule WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
        return EventDateScheduleWriteOutcome(
            self._event_date_schedule_from_row(row), applied=True
        )

    def ensure_scheduled_agent_target(
        self,
        venue_id: str,
        year: int,
        *,
        next_check_at: datetime | str,
        registered_at: datetime | str,
        lease: LeaseHandle,
    ) -> AgentScheduleRecord:
        """Idempotently ensure one 'scheduled' agent_schedule row exists.

        Used when a venue/year needs a coding-agent check without (or ahead
        of) a confirmed event-date estimate on this wake: a calendar
        fallback for a venue whose date discovery keeps failing, or a
        continuous-lifecycle venue with no discrete edition date at all.
        The matching ``event_date_schedule`` row must already exist (the
        foreign key requires it); this method never creates or mutates that
        row, so it never fabricates a confirmed event-date estimate.
        """
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError("agent schedules require local ownership")
        _validate_event_date_target(venue_id, year)
        next_check = _timestamp(next_check_at, field="agent next_check_at")
        registered = _timestamp(registered_at, field="agent registered_at")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            connection.execute(
                "INSERT INTO agent_schedule (venue_id, year, status, "
                "next_check_at, attempt_count, active_run_id, "
                "consecutive_failures, last_disposition, last_run_at, "
                "suggested_retry_at, last_gate_reason, updated_at) "
                "VALUES (?, ?, 'scheduled', ?, 0, NULL, 0, NULL, NULL, "
                "NULL, NULL, ?) ON CONFLICT (venue_id, year) DO NOTHING",
                (venue_id, year, next_check, registered),
            )
            row = connection.execute(
                "SELECT * FROM agent_schedule WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
        if row is None:
            raise AgentScheduleError("agent schedule was not retained")
        return self._agent_schedule_from_row(row)

    def complete_event_date_retry(
        self,
        claim: EventDateAttemptClaim,
        *,
        failure_category: str,
        completed_at: datetime | str,
        retry_at: datetime | str,
        lease: LeaseHandle,
    ) -> EventDateScheduleRecord:
        """Close one expected lookup failure with a bounded later retry."""
        failure = _bounded_event_text(
            failure_category, field="event-date failure category"
        )
        completed = _timestamp(completed_at, field="event-date completed_at")
        retry = _timestamp(retry_at, field="event-date retry_at")
        if _parse_timestamp(retry, field="event-date retry_at") <= \
                _parse_timestamp(completed, field="event-date completed_at"):
            raise EventDateScheduleError("event-date retry must be in the future")
        with self._write_transaction() as connection:
            self._require_event_date_claim(connection, claim, lease)
            connection.execute(
                "UPDATE event_date_attempt SET completed_at = ?, "
                "outcome = 'retry', failure_category = ? "
                "WHERE attempt_id = ? AND outcome = 'active'",
                (completed, failure, claim.attempt_id),
            )
            connection.execute(
                "UPDATE event_date_schedule SET status = 'pending', "
                "next_check_at = ?, active_attempt_id = NULL, "
                "last_failure_category = ?, updated_at = ? "
                "WHERE venue_id = ? AND year = ? AND status = 'active' "
                "AND active_attempt_id = ?",
                (
                    retry, failure, completed, claim.venue_id, claim.year,
                    claim.attempt_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM event_date_schedule WHERE venue_id = ? AND year = ?",
                (claim.venue_id, claim.year),
            ).fetchone()
        return self._event_date_schedule_from_row(row)

    def _require_event_date_claim(
        self,
        connection: sqlite3.Connection,
        claim: EventDateAttemptClaim,
        lease: LeaseHandle,
    ) -> None:
        if not isinstance(claim, EventDateAttemptClaim):
            raise EventDateScheduleError("event-date claim is invalid")
        self._require_lease(connection, lease, self._now())
        schedule = connection.execute(
            "SELECT * FROM event_date_schedule WHERE venue_id = ? AND year = ?",
            (claim.venue_id, claim.year),
        ).fetchone()
        attempt = connection.execute(
            "SELECT * FROM event_date_attempt WHERE attempt_id = ?",
            (claim.attempt_id,),
        ).fetchone()
        if schedule is None or attempt is None:
            raise EventDateScheduleError("event-date claim is not retained")
        record = self._event_date_schedule_from_row(schedule)
        attempt_record = self._event_date_attempt_from_row(attempt)
        if (
            record.status != "active"
            or record.active_attempt_id != claim.attempt_id
            or record.attempt_count != claim.attempt_number
            or attempt_record.outcome != "active"
            or attempt_record.venue_id != claim.venue_id
            or attempt_record.year != claim.year
            or attempt_record.attempt_number != claim.attempt_number
            or attempt_record.started_at != claim.started_at
            or attempt_record.provider_name != claim.provider_name
            or attempt_record.provider_model != claim.provider_model
            or attempt_record.prompt_version != claim.prompt_version
        ):
            raise EventDateScheduleError(
                "event-date claim is stale or already completed"
            )

    def get_event_date_schedule(
        self, venue_id: str, year: int
    ) -> EventDateScheduleRecord | None:
        """Return one fully validated event-date schedule."""
        _validate_event_date_target(venue_id, year)
        row = self._connection.execute(
            "SELECT * FROM event_date_schedule WHERE venue_id = ? AND year = ?",
            (venue_id, year),
        ).fetchone()
        return None if row is None else self._event_date_schedule_from_row(row)

    def list_event_date_schedules(self) -> tuple[EventDateScheduleRecord, ...]:
        """Return all validated date schedules in stable target order."""
        rows = self._connection.execute(
            "SELECT * FROM event_date_schedule ORDER BY venue_id, year"
        ).fetchall()
        return tuple(self._event_date_schedule_from_row(row) for row in rows)

    def event_date_attempt_history(
        self, venue_id: str, year: int
    ) -> tuple[EventDateAttemptRecord, ...]:
        """Return immutable lookup attempts for one target."""
        _validate_event_date_target(venue_id, year)
        rows = self._connection.execute(
            "SELECT * FROM event_date_attempt WHERE venue_id = ? AND year = ? "
            "ORDER BY attempt_number",
            (venue_id, year),
        ).fetchall()
        return tuple(self._event_date_attempt_from_row(row) for row in rows)

    def _event_date_schedule_from_row(
        self, row: sqlite3.Row
    ) -> EventDateScheduleRecord:
        venue_id = str(row["venue_id"])
        year = int(row["year"])
        _validate_event_date_target(venue_id, year)
        status = str(row["status"])
        if status not in {"pending", "active", "scheduled"}:
            raise StoredDataError("stored event-date status is invalid")
        next_check_at = _timestamp(
            str(row["next_check_at"]), field="stored event-date next_check_at"
        )
        updated_at = _timestamp(
            str(row["updated_at"]), field="stored event-date updated_at"
        )
        attempt_count = int(row["attempt_count"])
        if attempt_count < 0:
            raise StoredDataError("stored event-date attempt count is invalid")
        optional = {
            name: None if row[name] is None else str(row[name])
            for name in (
                "estimated_event_date", "estimated_at", "provider_name",
                "provider_model", "prompt_version", "active_attempt_id",
                "last_failure_category",
            )
        }
        if optional["estimated_event_date"] is not None:
            _event_date(
                optional["estimated_event_date"], field="stored estimated event date"
            )
        if optional["estimated_at"] is not None:
            optional["estimated_at"] = _timestamp(
                optional["estimated_at"], field="stored event-date estimated_at"
            )
        attempts = int(self._connection.execute(
            "SELECT COUNT(*) FROM event_date_attempt WHERE venue_id = ? AND year = ?",
            (venue_id, year),
        ).fetchone()[0])
        if attempts != attempt_count:
            raise StoredDataError("stored event-date attempt count does not match")
        if status == "pending" and any(optional[name] is not None for name in (
            "estimated_event_date", "estimated_at", "provider_name",
            "provider_model", "prompt_version", "active_attempt_id",
        )):
            raise StoredDataError("stored pending event-date schedule is invalid")
        if status == "active":
            if optional["active_attempt_id"] is None:
                raise StoredDataError("stored active event-date attempt is missing")
            active = self._connection.execute(
                "SELECT outcome FROM event_date_attempt WHERE attempt_id = ?",
                (optional["active_attempt_id"],),
            ).fetchone()
            if active is None or active["outcome"] != "active":
                raise StoredDataError("stored active event-date attempt is inconsistent")
        if status == "scheduled" and (
            optional["estimated_event_date"] is None
            or optional["estimated_at"] is None
            or optional["provider_name"] is None
            or optional["provider_model"] is None
            or optional["prompt_version"] is None
            or optional["active_attempt_id"] is not None
            or optional["last_failure_category"] is not None
        ):
            raise StoredDataError("stored scheduled event-date state is invalid")
        return EventDateScheduleRecord(
            venue_id=venue_id,
            year=year,
            status=status,
            next_check_at=next_check_at,
            estimated_event_date=optional["estimated_event_date"],
            estimated_at=optional["estimated_at"],
            provider_name=optional["provider_name"],
            provider_model=optional["provider_model"],
            prompt_version=optional["prompt_version"],
            attempt_count=attempt_count,
            active_attempt_id=optional["active_attempt_id"],
            last_failure_category=optional["last_failure_category"],
            updated_at=updated_at,
        )

    def _event_date_attempt_from_row(
        self, row: sqlite3.Row
    ) -> EventDateAttemptRecord:
        venue_id = str(row["venue_id"])
        year = int(row["year"])
        _validate_event_date_target(venue_id, year)
        attempt_number = int(row["attempt_number"])
        attempt_id = str(row["attempt_id"])
        expected_id = "event-date-attempt:" + artifact_fingerprint({
            "venue_id": venue_id,
            "year": year,
            "attempt_number": attempt_number,
        })
        if attempt_number < 1 or attempt_id != expected_id:
            raise StoredDataError("stored event-date attempt identity is invalid")
        outcome = str(row["outcome"])
        if outcome not in {"active", "scheduled", "retry"}:
            raise StoredDataError("stored event-date attempt outcome is invalid")
        started_at = _timestamp(
            str(row["started_at"]), field="stored event-date attempt start"
        )
        completed_at = (
            None if row["completed_at"] is None else _timestamp(
                str(row["completed_at"]), field="stored event-date attempt completion"
            )
        )
        estimated_event_date = (
            None if row["estimated_event_date"] is None else _event_date(
                str(row["estimated_event_date"]),
                field="stored attempt estimated event date",
            )
        )
        failure_category = (
            None if row["failure_category"] is None
            else str(row["failure_category"])
        )
        if completed_at is not None and _parse_timestamp(
            completed_at, field="stored event-date attempt completion"
        ) < _parse_timestamp(started_at, field="stored event-date attempt start"):
            raise StoredDataError("stored event-date attempt time regresses")
        if outcome == "active" and (
            completed_at is not None or estimated_event_date is not None
            or failure_category is not None
        ):
            raise StoredDataError("stored active event-date attempt is invalid")
        if outcome == "scheduled" and (
            completed_at is None or estimated_event_date is None
            or failure_category is not None
        ):
            raise StoredDataError("stored successful event-date attempt is invalid")
        if outcome == "retry" and (
            completed_at is None or estimated_event_date is not None
            or failure_category is None
        ):
            raise StoredDataError("stored retry event-date attempt is invalid")
        return EventDateAttemptRecord(
            attempt_id=attempt_id,
            venue_id=venue_id,
            year=year,
            attempt_number=attempt_number,
            started_at=started_at,
            completed_at=completed_at,
            outcome=outcome,
            provider_name=str(row["provider_name"]),
            provider_model=str(row["provider_model"]),
            prompt_version=str(row["prompt_version"]),
            estimated_event_date=estimated_event_date,
            failure_category=failure_category,
        )

    def claim_due_agent_run(
        self,
        *,
        claimed_at: datetime | str,
        monthly_run_limit: int,
        systemic_failure_threshold: int,
        systemic_failure_window: timedelta,
        systemic_circuit_delay: timedelta,
        lease: LeaseHandle,
    ) -> AgentRunClaimOutcome:
        """Claim one due target after durable concurrency and budget gates."""
        if self.writer is not Writer.LOCAL_CONTROL_PLANE:
            raise OwnershipError("agent schedules require local ownership")
        for value, field in (
            (monthly_run_limit, "monthly run limit"),
            (systemic_failure_threshold, "systemic failure threshold"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise AgentScheduleError(f"{field} must be a positive integer")
        for value, field in (
            (systemic_failure_window, "systemic failure window"),
            (systemic_circuit_delay, "systemic circuit delay"),
        ):
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise AgentScheduleError(f"{field} must be positive")
        claimed = _timestamp(claimed_at, field="agent run claimed_at")
        claimed_dt = _parse_timestamp(claimed, field="agent run claimed_at")
        month_start = claimed_dt.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        if month_start.month == 12:
            next_month = month_start.replace(
                year=month_start.year + 1, month=1
            )
        else:
            next_month = month_start.replace(month=month_start.month + 1)
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            active = connection.execute(
                "SELECT * FROM agent_schedule WHERE status = 'active'"
            ).fetchone()
            if active is not None:
                return AgentRunClaimOutcome(
                    claim=None,
                    schedule=self._agent_schedule_from_row(active),
                    reason="active_run",
                )
            row = connection.execute(
                "SELECT * FROM agent_schedule WHERE status = 'scheduled' "
                "AND next_check_at <= ? ORDER BY next_check_at, venue_id, year "
                "LIMIT 1",
                (claimed,),
            ).fetchone()
            if row is None:
                return AgentRunClaimOutcome(None, None, "nothing_due")
            schedule = self._agent_schedule_from_row(row)
            monthly_count = int(connection.execute(
                "SELECT COUNT(*) FROM agent_run_attempt "
                "WHERE started_at >= ? AND started_at < ?",
                (
                    _timestamp(month_start, field="agent budget month start"),
                    _timestamp(next_month, field="agent budget next month"),
                ),
            ).fetchone()[0])
            if monthly_count >= monthly_run_limit:
                connection.execute(
                    "UPDATE agent_schedule SET next_check_at = ?, "
                    "last_gate_reason = 'monthly_budget', updated_at = ? "
                    "WHERE venue_id = ? AND year = ? AND status = 'scheduled'",
                    (
                        _timestamp(next_month, field="agent budget retry"),
                        claimed, schedule.venue_id, schedule.year,
                    ),
                )
                deferred = connection.execute(
                    "SELECT * FROM agent_schedule WHERE venue_id = ? AND year = ?",
                    (schedule.venue_id, schedule.year),
                ).fetchone()
                return AgentRunClaimOutcome(
                    None, self._agent_schedule_from_row(deferred), "monthly_budget"
                )
            window_start = claimed_dt - systemic_failure_window
            failures = connection.execute(
                "SELECT COUNT(DISTINCT venue_id), MAX(completed_at) "
                "FROM agent_run_attempt WHERE disposition = 'failed' "
                "AND completed_at >= ? AND completed_at <= ?",
                (
                    _timestamp(window_start, field="systemic window start"),
                    claimed,
                ),
            ).fetchone()
            distinct_failures = int(failures[0])
            latest_failure = failures[1]
            if distinct_failures >= systemic_failure_threshold and \
                    latest_failure is not None:
                circuit_until_dt = _parse_timestamp(
                    str(latest_failure), field="latest systemic failure"
                ) + systemic_circuit_delay
                if circuit_until_dt > claimed_dt:
                    circuit_until = _timestamp(
                        circuit_until_dt, field="systemic circuit retry"
                    )
                    connection.execute(
                        "UPDATE agent_schedule SET next_check_at = ?, "
                        "last_gate_reason = 'systemic_failure', updated_at = ? "
                        "WHERE venue_id = ? AND year = ? AND status = 'scheduled'",
                        (
                            circuit_until, claimed, schedule.venue_id,
                            schedule.year,
                        ),
                    )
                    deferred = connection.execute(
                        "SELECT * FROM agent_schedule "
                        "WHERE venue_id = ? AND year = ?",
                        (schedule.venue_id, schedule.year),
                    ).fetchone()
                    return AgentRunClaimOutcome(
                        None,
                        self._agent_schedule_from_row(deferred),
                        "systemic_failure",
                    )
            attempt_number = schedule.attempt_count + 1
            run_id = "agent-run:" + artifact_fingerprint({
                "venue_id": schedule.venue_id,
                "year": schedule.year,
                "attempt_number": attempt_number,
            })
            connection.execute(
                "INSERT INTO agent_run_attempt (run_id, venue_id, year, "
                "attempt_number, started_at, completed_at, disposition, "
                "explanation, suggested_retry_at, failure_category) "
                "VALUES (?, ?, ?, ?, ?, NULL, 'active', NULL, NULL, NULL)",
                (
                    run_id, schedule.venue_id, schedule.year, attempt_number,
                    claimed,
                ),
            )
            connection.execute(
                "UPDATE agent_schedule SET status = 'active', "
                "next_check_at = NULL, attempt_count = ?, active_run_id = ?, "
                "last_gate_reason = NULL, updated_at = ? "
                "WHERE venue_id = ? AND year = ? AND status = 'scheduled'",
                (
                    attempt_number, run_id, claimed, schedule.venue_id,
                    schedule.year,
                ),
            )
            claimed_row = connection.execute(
                "SELECT * FROM agent_schedule WHERE venue_id = ? AND year = ?",
                (schedule.venue_id, schedule.year),
            ).fetchone()
        claim = AgentRunClaim(
            run_id=run_id,
            venue_id=schedule.venue_id,
            year=schedule.year,
            attempt_number=attempt_number,
            started_at=claimed,
        )
        return AgentRunClaimOutcome(
            claim, self._agent_schedule_from_row(claimed_row), "claimed"
        )

    def complete_agent_run_attempt(
        self,
        claim: AgentRunClaim,
        *,
        disposition: str,
        explanation: str,
        completed_at: datetime | str,
        next_check_at: datetime | str | None,
        suggested_retry_at: datetime | str | None,
        failure_category: str | None,
        pause_after_failure: bool,
        lease: LeaseHandle,
        changed_files: tuple[str, ...] | None = None,
        returncode: int | None = None,
        timed_out: bool = False,
        recurring: bool = False,
    ) -> AgentScheduleRecord:
        """Complete one claimed run with an already-reduced policy result.

        ``recurring`` is only valid with ``disposition == "success"``: it
        keeps the schedule at ``'scheduled'`` with the given future
        ``next_check_at`` instead of moving it to the terminal ``'completed'``
        state, for a continuous-lifecycle venue (e.g. a journal) that has no
        single "done" edition and must keep being rechecked for new items
        after every successful check.
        """
        if disposition not in {"success", "not_ready", "needs_human", "failed"}:
            raise AgentScheduleError("agent disposition is invalid")
        if recurring and disposition != "success":
            raise AgentScheduleError("recurring only applies to a success disposition")
        explanation = _bounded_event_text(
            explanation, field="agent explanation", maximum=4000
        )
        failure = None if failure_category is None else _bounded_event_text(
            failure_category, field="agent failure category"
        )
        assert_secret_free({
            "explanation": explanation,
            "failure_category": failure or "",
        })
        completed = _timestamp(completed_at, field="agent run completed_at")
        next_check = None if next_check_at is None else _timestamp(
            next_check_at, field="agent next_check_at"
        )
        suggested = None if suggested_retry_at is None else _timestamp(
            suggested_retry_at, field="agent suggested_retry_at"
        )
        if disposition == "not_ready":
            if next_check is None or failure is not None or pause_after_failure:
                raise AgentScheduleError("not-ready completion is inconsistent")
        elif disposition == "failed":
            if failure is None or suggested is not None:
                raise AgentScheduleError("failed completion is inconsistent")
            if pause_after_failure != (next_check is None):
                raise AgentScheduleError("failed pause state is inconsistent")
        elif recurring:
            if next_check is None or suggested is not None or failure is not None \
                    or pause_after_failure:
                raise AgentScheduleError("recurring completion is inconsistent")
        elif any((next_check, suggested, failure)) or pause_after_failure:
            raise AgentScheduleError("terminal completion is inconsistent")
        completed_dt = _parse_timestamp(completed, field="agent run completed_at")
        for value, field in (
            (next_check, "agent next_check_at"),
            (suggested, "agent suggested_retry_at"),
        ):
            if value is not None and _parse_timestamp(value, field=field) <= completed_dt:
                raise AgentScheduleError(f"{field} must be in the future")
        artifact_completion = changed_files is not None
        if not isinstance(timed_out, bool):
            raise AgentArtifactError("agent timed_out must be boolean")
        if returncode is not None and (
            not isinstance(returncode, int) or isinstance(returncode, bool)
        ):
            raise AgentArtifactError("agent returncode must be an integer")
        changed_json = None
        if artifact_completion:
            if not isinstance(changed_files, tuple) or len(changed_files) > 1000:
                raise AgentArtifactError("agent changed-file inventory is invalid")
            normalized: list[str] = []
            for item in changed_files:
                text = _bounded_event_text(
                    item, field="agent changed file", maximum=1000
                )
                if "\n" in text or "\r" in text:
                    raise AgentArtifactError("agent changed file contains a newline")
                normalized.append(text)
            assert_secret_free({"changed_files": normalized})
            changed_json = _canonical_json({"items": normalized})
        elif returncode is not None or timed_out:
            raise AgentArtifactError("agent process state lacks an artifact")
        with self._write_transaction() as connection:
            schedule = self._require_agent_run_claim(connection, claim, lease)
            artifact = connection.execute(
                "SELECT lifecycle FROM agent_execution_artifact WHERE run_id = ?",
                (claim.run_id,),
            ).fetchone()
            if artifact_completion:
                if artifact is None or artifact["lifecycle"] != "active":
                    raise AgentArtifactError("active agent artifact is not retained")
            elif artifact is not None:
                raise AgentArtifactError("active agent artifact requires completion")
            failures = schedule.consecutive_failures + int(disposition == "failed")
            if disposition != "failed":
                failures = 0
            if disposition == "success":
                status = "scheduled" if recurring else "completed"
            elif disposition == "needs_human":
                status = "needs_human"
            elif disposition == "failed" and pause_after_failure:
                status = "paused"
            else:
                status = "scheduled"
            connection.execute(
                "UPDATE agent_run_attempt SET completed_at = ?, disposition = ?, "
                "explanation = ?, suggested_retry_at = ?, failure_category = ? "
                "WHERE run_id = ? AND disposition = 'active'",
                (
                    completed, disposition, explanation, suggested, failure,
                    claim.run_id,
                ),
            )
            if artifact_completion:
                connection.execute(
                    "UPDATE agent_execution_artifact SET lifecycle = 'terminal', "
                    "completed_at = ?, changed_files_json = ?, returncode = ?, "
                    "timed_out = ? WHERE run_id = ? AND lifecycle = 'active'",
                    (completed, changed_json, returncode, int(timed_out), claim.run_id),
                )
                report_id = "agent-run-report:" + artifact_fingerprint({
                    "run_id": claim.run_id,
                })
                connection.execute(
                    "INSERT INTO agent_run_report (report_id, run_id, status, "
                    "schedule_status, next_check_at, attempt_count, "
                    "created_at, updated_at, delivered_at, "
                    "last_failure_category, receipt_id) VALUES "
                    "(?, ?, 'pending', ?, ?, 0, ?, ?, NULL, NULL, NULL)",
                    (
                        report_id, claim.run_id, status, next_check,
                        completed, completed,
                    ),
                )
            connection.execute(
                "UPDATE agent_schedule SET status = ?, next_check_at = ?, "
                "active_run_id = NULL, consecutive_failures = ?, "
                "last_disposition = ?, last_run_at = ?, suggested_retry_at = ?, "
                "last_gate_reason = NULL, updated_at = ? "
                "WHERE venue_id = ? AND year = ? AND status = 'active' "
                "AND active_run_id = ?",
                (
                    status, next_check, failures, disposition, completed,
                    suggested, completed, claim.venue_id, claim.year,
                    claim.run_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_schedule WHERE venue_id = ? AND year = ?",
                (claim.venue_id, claim.year),
            ).fetchone()
        return self._agent_schedule_from_row(row)

    def begin_agent_execution_artifact(
        self,
        claim: AgentRunClaim,
        *,
        runs_root: Path | str,
        worktree_path: Path | str,
        branch_name: str,
        base_commit: str,
        started_at: datetime | str,
        lease: LeaseHandle,
    ) -> AgentExecutionArtifactRecord:
        """Register one managed worktree before invoking an external agent."""
        root = Path(runs_root)
        worktree = Path(worktree_path)
        if not root.is_absolute() or not worktree.is_absolute():
            raise AgentArtifactError("agent worktree paths must be absolute")
        try:
            worktree.relative_to(root)
        except ValueError as exc:
            raise AgentArtifactError("agent worktree is outside its runs root") from exc
        if worktree.parent != root or worktree == root:
            raise AgentArtifactError("agent worktree must be a direct child")
        branch = _bounded_event_text(
            branch_name, field="agent branch name", maximum=200
        )
        commit = _bounded_event_text(
            base_commit, field="agent base commit", maximum=128
        )
        if not branch.startswith("automation/agent/") or len(commit) != 40 or any(
            character not in "0123456789abcdef" for character in commit.lower()
        ):
            raise AgentArtifactError("agent Git identity is invalid")
        started = _timestamp(started_at, field="agent artifact started_at")
        if _parse_timestamp(started, field="agent artifact started_at") < \
                _parse_timestamp(claim.started_at, field="agent run claimed_at"):
            raise AgentArtifactError("agent artifact predates its run claim")
        with self._write_transaction() as connection:
            self._require_agent_run_claim(connection, claim, lease)
            connection.execute(
                "INSERT INTO agent_execution_artifact (run_id, lifecycle, "
                "runs_root, worktree_path, branch_name, base_commit, started_at, "
                "completed_at, changed_files_json, returncode, timed_out, "
                "retention_status, removed_at, removal_failure) VALUES "
                "(?, 'active', ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, "
                "'retained', NULL, NULL)",
                (
                    claim.run_id, str(root), str(worktree), branch, commit, started,
                ),
            )
        record = self.get_agent_execution_artifact(claim.run_id)
        if record is None:
            raise AgentArtifactError("agent artifact registration disappeared")
        return record

    def get_agent_execution_artifact(
        self, run_id: str
    ) -> AgentExecutionArtifactRecord | None:
        row = self._connection.execute(
            "SELECT * FROM agent_execution_artifact WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else self._agent_execution_artifact_from_row(row)

    def list_agent_execution_artifacts(
        self,
    ) -> tuple[AgentExecutionArtifactRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM agent_execution_artifact "
            "ORDER BY started_at, run_id"
        ).fetchall()
        return tuple(self._agent_execution_artifact_from_row(row) for row in rows)

    def record_agent_worktree_retention(
        self,
        run_id: str,
        *,
        status: str,
        recorded_at: datetime | str,
        failure_category: str | None,
        lease: LeaseHandle,
    ) -> AgentExecutionArtifactRecord:
        """Record the result of one controller-owned worktree removal."""
        if status not in {"removed", "removal_failed"}:
            raise AgentArtifactError("agent retention status is invalid")
        recorded = _timestamp(recorded_at, field="agent retention recorded_at")
        failure = None if failure_category is None else _bounded_event_text(
            failure_category, field="agent retention failure", maximum=200
        )
        if (status == "removed") != (failure is None):
            raise AgentArtifactError("agent retention outcome is inconsistent")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            row = connection.execute(
                "SELECT * FROM agent_execution_artifact WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise AgentArtifactError("agent artifact is not retained")
            artifact = self._agent_execution_artifact_from_row(row)
            if artifact.lifecycle != "terminal" or artifact.retention_status == "removed":
                raise AgentArtifactError("agent artifact is not removable")
            connection.execute(
                "UPDATE agent_execution_artifact SET retention_status = ?, "
                "removed_at = ?, removal_failure = ? WHERE run_id = ?",
                (
                    status,
                    recorded if status == "removed" else None,
                    failure,
                    run_id,
                ),
            )
        result = self.get_agent_execution_artifact(run_id)
        if result is None:
            raise AgentArtifactError("agent retention update disappeared")
        return result

    def get_agent_run_attempt(self, run_id: str) -> AgentRunAttemptRecord | None:
        row = self._connection.execute(
            "SELECT * FROM agent_run_attempt WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else self._agent_run_attempt_from_row(row)

    def get_agent_run_report(self, run_id: str) -> AgentRunReportRecord | None:
        row = self._connection.execute(
            "SELECT * FROM agent_run_report WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else self._agent_run_report_from_row(row)

    def pending_agent_run_reports(
        self, *, limit: int = 1
    ) -> tuple[AgentRunReportRecord, ...]:
        """Return a bounded oldest-first set eligible for a delivery attempt."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise AgentRunReportError("agent report selection limit is invalid")
        rows = self._connection.execute(
            "SELECT * FROM agent_run_report WHERE status IN "
            "('pending', 'retryable') ORDER BY created_at, report_id LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(self._agent_run_report_from_row(row) for row in rows)

    def prepare_agent_run_report_delivery(
        self,
        run_id: str,
        *,
        started_at: datetime | str,
        lease: LeaseHandle,
        retry_permanent_protocol_error: bool = False,
    ) -> AgentRunReportAttemptRecord | None:
        """Claim a pending or retryable run report for one external send."""
        if not isinstance(retry_permanent_protocol_error, bool):
            raise AgentRunReportError("agent report retry authority is invalid")
        started = _timestamp(started_at, field="agent report started_at")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            row = connection.execute(
                "SELECT * FROM agent_run_report WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise AgentRunReportError("agent run report is not retained")
            report = self._agent_run_report_from_row(row)
            explicit_protocol_retry = (
                retry_permanent_protocol_error
                and report.status == "permanent_failure"
                and report.last_failure_category == "protocol_error"
            )
            if report.status in {"in_flight", "delivered"} or (
                report.status == "permanent_failure" and not explicit_protocol_retry
            ):
                return None
            attempt_number = report.attempt_count + 1
            connection.execute(
                "INSERT INTO agent_run_report_attempt (report_id, "
                "attempt_number, started_at, completed_at, outcome, "
                "failure_category, receipt_id) VALUES "
                "(?, ?, ?, NULL, 'active', NULL, NULL)",
                (report.report_id, attempt_number, started),
            )
            connection.execute(
                "UPDATE agent_run_report SET status = 'in_flight', "
                "attempt_count = ?, updated_at = ?, "
                "last_failure_category = NULL WHERE report_id = ?",
                (attempt_number, started, report.report_id),
            )
        return AgentRunReportAttemptRecord(
            report.report_id, attempt_number, started, None, "active", None, None
        )

    def complete_agent_run_report_delivery(
        self,
        report_id: str,
        attempt_number: int,
        *,
        status: str,
        completed_at: datetime | str,
        failure_category: str | None = None,
        receipt_id: str | None = None,
        lease: LeaseHandle,
    ) -> AgentRunReportRecord:
        """Complete the current report attempt without changing the run result."""
        if status not in {"retryable", "delivered", "permanent_failure"}:
            raise AgentRunReportError("agent report completion status is invalid")
        if not isinstance(attempt_number, int) or isinstance(attempt_number, bool) \
                or attempt_number < 1:
            raise AgentRunReportError("agent report attempt number is invalid")
        completed = _timestamp(completed_at, field="agent report completed_at")
        failure = None
        if failure_category is not None:
            try:
                failure = FailureCategory(failure_category).value
            except ValueError as exc:
                raise AgentRunReportError("agent report failure is invalid") from exc
        if status == "delivered":
            if failure is not None or receipt_id is None:
                raise AgentRunReportError("delivered agent report is inconsistent")
            validate_receipt_id(receipt_id)
        elif failure is None or receipt_id is not None:
            raise AgentRunReportError("failed agent report is inconsistent")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            row = connection.execute(
                "SELECT * FROM agent_run_report WHERE report_id = ?", (report_id,)
            ).fetchone()
            if row is None:
                raise AgentRunReportError("agent run report is not retained")
            report = self._agent_run_report_from_row(row)
            if report.status != "in_flight" or report.attempt_count != attempt_number:
                raise AgentRunReportError("agent report attempt is stale")
            attempt_row = connection.execute(
                "SELECT outcome FROM agent_run_report_attempt "
                "WHERE report_id = ? AND attempt_number = ?",
                (report_id, attempt_number),
            ).fetchone()
            if attempt_row is None or attempt_row["outcome"] != "active":
                raise AgentRunReportError("agent report attempt is inconsistent")
            connection.execute(
                "UPDATE agent_run_report_attempt SET completed_at = ?, "
                "outcome = ?, failure_category = ?, receipt_id = ? "
                "WHERE report_id = ? AND attempt_number = ? AND outcome = 'active'",
                (completed, status, failure, receipt_id, report_id, attempt_number),
            )
            connection.execute(
                "UPDATE agent_run_report SET status = ?, updated_at = ?, "
                "delivered_at = ?, last_failure_category = ?, receipt_id = ? "
                "WHERE report_id = ? AND status = 'in_flight'",
                (
                    status, completed,
                    completed if status == "delivered" else None,
                    failure, receipt_id, report_id,
                ),
            )
        result_row = self._connection.execute(
            "SELECT * FROM agent_run_report WHERE report_id = ?", (report_id,)
        ).fetchone()
        return self._agent_run_report_from_row(result_row)

    def resume_agent_schedule(
        self,
        venue_id: str,
        year: int,
        *,
        next_check_at: datetime | str,
        resumed_at: datetime | str,
        lease: LeaseHandle,
    ) -> AgentScheduleRecord:
        """Explicitly resume a failure-paused target after operator review."""
        _validate_event_date_target(venue_id, year)
        resumed = _timestamp(resumed_at, field="agent resumed_at")
        next_check = _timestamp(next_check_at, field="agent resume next_check_at")
        if _parse_timestamp(next_check, field="agent resume next_check_at") < \
                _parse_timestamp(resumed, field="agent resumed_at"):
            raise AgentScheduleError("agent resume time cannot be in the past")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            row = connection.execute(
                "SELECT * FROM agent_schedule WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
            if row is None or self._agent_schedule_from_row(row).status != "paused":
                raise AgentScheduleError("agent schedule is not failure-paused")
            connection.execute(
                "UPDATE agent_schedule SET status = 'scheduled', "
                "next_check_at = ?, consecutive_failures = 0, "
                "last_gate_reason = NULL, updated_at = ? "
                "WHERE venue_id = ? AND year = ? AND status = 'paused'",
                (next_check, resumed, venue_id, year),
            )
            updated = connection.execute(
                "SELECT * FROM agent_schedule WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
        return self._agent_schedule_from_row(updated)

    def advance_agent_schedule_from_hint(
        self,
        venue_id: str,
        year: int,
        *,
        hint_observed_at: datetime | str,
        next_check_at: datetime | str,
        applied_at: datetime | str,
        lease: LeaseHandle,
    ) -> AgentScheduleHintOutcome:
        """Advance one future check without claiming or reactivating work."""
        _validate_event_date_target(venue_id, year)
        observed = _timestamp(
            hint_observed_at, field="agent schedule hint observed_at"
        )
        next_check = _timestamp(
            next_check_at, field="agent schedule hint next_check_at"
        )
        applied_time = _timestamp(applied_at, field="agent schedule hint applied_at")
        if _parse_timestamp(next_check, field="agent schedule hint next_check_at") < \
                _parse_timestamp(observed, field="agent schedule hint observed_at"):
            raise AgentScheduleError("agent schedule hint cannot target the past")
        with self._write_transaction() as connection:
            self._require_lease(connection, lease, self._now())
            row = connection.execute(
                "SELECT * FROM agent_schedule WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
            if row is None:
                return AgentScheduleHintOutcome(None, False, "schedule_missing")
            schedule = self._agent_schedule_from_row(row)
            if schedule.status != "scheduled":
                return AgentScheduleHintOutcome(schedule, False, "not_scheduled")
            if schedule.last_run_at is not None and _parse_timestamp(
                schedule.last_run_at, field="agent last run"
            ) >= _parse_timestamp(observed, field="agent schedule hint observed_at"):
                return AgentScheduleHintOutcome(schedule, False, "superseded_by_run")
            if _parse_timestamp(
                schedule.next_check_at, field="agent current next_check_at"
            ) <= _parse_timestamp(next_check, field="agent schedule hint next_check_at"):
                return AgentScheduleHintOutcome(schedule, False, "already_earlier")
            connection.execute(
                "UPDATE agent_schedule SET next_check_at = ?, "
                "last_gate_reason = NULL, updated_at = ? "
                "WHERE venue_id = ? AND year = ? AND status = 'scheduled'",
                (next_check, applied_time, venue_id, year),
            )
            updated = connection.execute(
                "SELECT * FROM agent_schedule WHERE venue_id = ? AND year = ?",
                (venue_id, year),
            ).fetchone()
        return AgentScheduleHintOutcome(
            self._agent_schedule_from_row(updated), True, "advanced"
        )

    def get_agent_schedule(
        self, venue_id: str, year: int
    ) -> AgentScheduleRecord | None:
        """Return one validated coding-agent schedule."""
        _validate_event_date_target(venue_id, year)
        row = self._connection.execute(
            "SELECT * FROM agent_schedule WHERE venue_id = ? AND year = ?",
            (venue_id, year),
        ).fetchone()
        return None if row is None else self._agent_schedule_from_row(row)

    def list_agent_schedules(self) -> tuple[AgentScheduleRecord, ...]:
        """Return validated coding-agent schedules in stable target order."""
        rows = self._connection.execute(
            "SELECT * FROM agent_schedule ORDER BY venue_id, year"
        ).fetchall()
        return tuple(self._agent_schedule_from_row(row) for row in rows)

    def agent_run_history(
        self, venue_id: str, year: int
    ) -> tuple[AgentRunAttemptRecord, ...]:
        """Return immutable coding-agent attempts for one target."""
        _validate_event_date_target(venue_id, year)
        rows = self._connection.execute(
            "SELECT * FROM agent_run_attempt WHERE venue_id = ? AND year = ? "
            "ORDER BY attempt_number",
            (venue_id, year),
        ).fetchall()
        return tuple(self._agent_run_attempt_from_row(row) for row in rows)

    def _require_agent_run_claim(
        self,
        connection: sqlite3.Connection,
        claim: AgentRunClaim,
        lease: LeaseHandle,
    ) -> AgentScheduleRecord:
        if not isinstance(claim, AgentRunClaim):
            raise AgentScheduleError("agent run claim is invalid")
        self._require_lease(connection, lease, self._now())
        schedule_row = connection.execute(
            "SELECT * FROM agent_schedule WHERE venue_id = ? AND year = ?",
            (claim.venue_id, claim.year),
        ).fetchone()
        attempt_row = connection.execute(
            "SELECT * FROM agent_run_attempt WHERE run_id = ?",
            (claim.run_id,),
        ).fetchone()
        if schedule_row is None or attempt_row is None:
            raise AgentScheduleError("agent run claim is not retained")
        schedule = self._agent_schedule_from_row(schedule_row)
        attempt = self._agent_run_attempt_from_row(attempt_row)
        if (
            schedule.status != "active"
            or schedule.active_run_id != claim.run_id
            or schedule.attempt_count != claim.attempt_number
            or attempt.disposition != "active"
            or attempt.venue_id != claim.venue_id
            or attempt.year != claim.year
            or attempt.attempt_number != claim.attempt_number
            or attempt.started_at != claim.started_at
        ):
            raise AgentScheduleError("agent run claim is stale or already completed")
        return schedule

    def _agent_execution_artifact_from_row(
        self, row: sqlite3.Row
    ) -> AgentExecutionArtifactRecord:
        lifecycle = str(row["lifecycle"])
        retention = str(row["retention_status"])
        if lifecycle not in {"active", "terminal"} or retention not in {
            "retained", "removed", "removal_failed"
        }:
            raise StoredDataError("stored agent artifact state is invalid")
        root = Path(str(row["runs_root"]))
        worktree = Path(str(row["worktree_path"]))
        if not root.is_absolute() or not worktree.is_absolute():
            raise StoredDataError("stored agent artifact path is not absolute")
        try:
            worktree.relative_to(root)
        except ValueError as exc:
            raise StoredDataError("stored agent artifact escapes its root") from exc
        if worktree.parent != root:
            raise StoredDataError("stored agent artifact is not a direct child")
        started = _timestamp(str(row["started_at"]), field="stored artifact start")
        completed = None if row["completed_at"] is None else _timestamp(
            str(row["completed_at"]), field="stored artifact completion"
        )
        removed = None if row["removed_at"] is None else _timestamp(
            str(row["removed_at"]), field="stored artifact removal"
        )
        changed: tuple[str, ...] = ()
        if row["changed_files_json"] is not None:
            payload = _decode_json(row["changed_files_json"], label="changed files")
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list) or len(items) > 1000 or not all(
                isinstance(item, str) and item and len(item) <= 1000
                and "\n" not in item and "\r" not in item for item in items
            ):
                raise StoredDataError("stored changed-file inventory is invalid")
            changed = tuple(items)
        returncode = row["returncode"]
        if returncode is not None:
            returncode = int(returncode)
        timed_out = int(row["timed_out"])
        failure = None if row["removal_failure"] is None else str(
            row["removal_failure"]
        )
        if lifecycle == "active" and any((completed, changed, returncode, timed_out)):
            raise StoredDataError("stored active agent artifact is inconsistent")
        if lifecycle == "terminal" and completed is None:
            raise StoredDataError("stored terminal agent artifact is incomplete")
        if retention == "retained" and (removed is not None or failure is not None):
            raise StoredDataError("stored retained artifact is inconsistent")
        if retention == "removed" and (removed is None or failure is not None):
            raise StoredDataError("stored removed artifact is inconsistent")
        if retention == "removal_failed" and (removed is not None or failure is None):
            raise StoredDataError("stored failed retention is inconsistent")
        return AgentExecutionArtifactRecord(
            run_id=str(row["run_id"]), lifecycle=lifecycle,
            runs_root=str(root), worktree_path=str(worktree),
            branch_name=str(row["branch_name"]), base_commit=str(row["base_commit"]),
            started_at=started, completed_at=completed, changed_files=changed,
            returncode=returncode, timed_out=bool(timed_out),
            retention_status=retention, removed_at=removed,
            removal_failure=failure,
        )

    def _agent_run_report_from_row(self, row: sqlite3.Row) -> AgentRunReportRecord:
        status = str(row["status"])
        if status not in {
            "pending", "in_flight", "retryable", "delivered",
            "permanent_failure",
        }:
            raise StoredDataError("stored agent report status is invalid")
        expected_id = "agent-run-report:" + artifact_fingerprint({
            "run_id": str(row["run_id"]),
        })
        if str(row["report_id"]) != expected_id:
            raise StoredDataError("stored agent report identity is invalid")
        schedule_status = str(row["schedule_status"])
        next_check = None if row["next_check_at"] is None else _timestamp(
            str(row["next_check_at"]), field="stored report next check"
        )
        if schedule_status not in {"scheduled", "completed", "needs_human", "paused"}:
            raise StoredDataError("stored agent report schedule state is invalid")
        if (schedule_status == "scheduled") != (next_check is not None):
            raise StoredDataError("stored agent report retry state is inconsistent")
        created = _timestamp(str(row["created_at"]), field="stored report created")
        updated = _timestamp(str(row["updated_at"]), field="stored report updated")
        delivered = None if row["delivered_at"] is None else _timestamp(
            str(row["delivered_at"]), field="stored report delivered"
        )
        failure = None if row["last_failure_category"] is None else str(
            row["last_failure_category"]
        )
        receipt = None if row["receipt_id"] is None else str(row["receipt_id"])
        attempts = int(row["attempt_count"])
        actual = int(self._connection.execute(
            "SELECT COUNT(*) FROM agent_run_report_attempt WHERE report_id = ?",
            (row["report_id"],),
        ).fetchone()[0])
        if attempts != actual:
            raise StoredDataError("stored agent report attempt count differs")
        if status in {"pending", "in_flight"} and any((delivered, failure, receipt)):
            raise StoredDataError("stored open agent report is inconsistent")
        if status in {"retryable", "permanent_failure"} and (
            delivered is not None or failure is None or receipt is not None
        ):
            raise StoredDataError("stored failed agent report is inconsistent")
        if status == "delivered" and (
            delivered is None or failure is not None or receipt is None
        ):
            raise StoredDataError("stored delivered agent report is inconsistent")
        return AgentRunReportRecord(
            report_id=str(row["report_id"]), run_id=str(row["run_id"]),
            status=status, schedule_status=schedule_status,
            next_check_at=next_check,
            attempt_count=attempts, created_at=created,
            updated_at=updated, delivered_at=delivered,
            last_failure_category=failure, receipt_id=receipt,
        )

    def _agent_schedule_from_row(self, row: sqlite3.Row) -> AgentScheduleRecord:
        venue_id = str(row["venue_id"])
        year = int(row["year"])
        _validate_event_date_target(venue_id, year)
        status = str(row["status"])
        if status not in {"scheduled", "active", "completed", "needs_human", "paused"}:
            raise StoredDataError("stored agent schedule status is invalid")
        next_check = None if row["next_check_at"] is None else _timestamp(
            str(row["next_check_at"]), field="stored agent next_check_at"
        )
        updated = _timestamp(str(row["updated_at"]), field="stored agent updated_at")
        attempt_count = int(row["attempt_count"])
        consecutive_failures = int(row["consecutive_failures"])
        if attempt_count < 0 or consecutive_failures < 0:
            raise StoredDataError("stored agent counters are invalid")
        optional = {
            name: None if row[name] is None else str(row[name])
            for name in (
                "active_run_id", "last_disposition", "last_run_at",
                "suggested_retry_at", "last_gate_reason",
            )
        }
        for name in ("last_run_at", "suggested_retry_at"):
            if optional[name] is not None:
                optional[name] = _timestamp(
                    optional[name], field=f"stored agent {name}"
                )
        if optional["last_disposition"] is not None and \
                optional["last_disposition"] not in {
                    "success", "not_ready", "needs_human", "failed"
                }:
            raise StoredDataError("stored agent disposition is invalid")
        if optional["last_gate_reason"] is not None and \
                optional["last_gate_reason"] not in {
                    "monthly_budget", "systemic_failure"
                }:
            raise StoredDataError("stored agent gate reason is invalid")
        attempts = int(self._connection.execute(
            "SELECT COUNT(*) FROM agent_run_attempt WHERE venue_id = ? AND year = ?",
            (venue_id, year),
        ).fetchone()[0])
        if attempts != attempt_count:
            raise StoredDataError("stored agent attempt count does not match")
        if status == "scheduled" and (
            next_check is None or optional["active_run_id"] is not None
        ):
            raise StoredDataError("stored scheduled agent state is invalid")
        if status == "active":
            if next_check is not None or optional["active_run_id"] is None:
                raise StoredDataError("stored active agent state is invalid")
            active = self._connection.execute(
                "SELECT disposition FROM agent_run_attempt WHERE run_id = ?",
                (optional["active_run_id"],),
            ).fetchone()
            if active is None or active["disposition"] != "active":
                raise StoredDataError("stored active agent run is inconsistent")
        if status in {"completed", "needs_human", "paused"} and (
            next_check is not None or optional["active_run_id"] is not None
        ):
            raise StoredDataError("stored terminal agent state is invalid")
        return AgentScheduleRecord(
            venue_id=venue_id,
            year=year,
            status=status,
            next_check_at=next_check,
            attempt_count=attempt_count,
            active_run_id=optional["active_run_id"],
            consecutive_failures=consecutive_failures,
            last_disposition=optional["last_disposition"],
            last_run_at=optional["last_run_at"],
            suggested_retry_at=optional["suggested_retry_at"],
            last_gate_reason=optional["last_gate_reason"],
            updated_at=updated,
        )

    def _agent_run_attempt_from_row(
        self, row: sqlite3.Row
    ) -> AgentRunAttemptRecord:
        venue_id = str(row["venue_id"])
        year = int(row["year"])
        _validate_event_date_target(venue_id, year)
        attempt_number = int(row["attempt_number"])
        run_id = str(row["run_id"])
        expected = "agent-run:" + artifact_fingerprint({
            "venue_id": venue_id,
            "year": year,
            "attempt_number": attempt_number,
        })
        if attempt_number < 1 or run_id != expected:
            raise StoredDataError("stored agent run identity is invalid")
        disposition = str(row["disposition"])
        if disposition not in {"active", "success", "not_ready", "needs_human", "failed"}:
            raise StoredDataError("stored agent run disposition is invalid")
        started = _timestamp(str(row["started_at"]), field="stored agent run start")
        completed = None if row["completed_at"] is None else _timestamp(
            str(row["completed_at"]), field="stored agent run completion"
        )
        explanation = None if row["explanation"] is None else str(row["explanation"])
        suggested = None if row["suggested_retry_at"] is None else _timestamp(
            str(row["suggested_retry_at"]), field="stored agent suggested retry"
        )
        failure = None if row["failure_category"] is None else str(row["failure_category"])
        if completed is not None and _parse_timestamp(
            completed, field="stored agent run completion"
        ) < _parse_timestamp(started, field="stored agent run start"):
            raise StoredDataError("stored agent run time regresses")
        if disposition == "active" and any((completed, explanation, suggested, failure)):
            raise StoredDataError("stored active agent run is invalid")
        if disposition in {"success", "not_ready", "needs_human"} and (
            completed is None or explanation is None or failure is not None
        ):
            raise StoredDataError("stored completed agent run is invalid")
        if disposition != "not_ready" and suggested is not None:
            raise StoredDataError("stored agent retry suggestion is invalid")
        if disposition == "failed" and (
            completed is None or explanation is None or failure is None
        ):
            raise StoredDataError("stored failed agent run is invalid")
        return AgentRunAttemptRecord(
            run_id=run_id,
            venue_id=venue_id,
            year=year,
            attempt_number=attempt_number,
            started_at=started,
            completed_at=completed,
            disposition=disposition,
            explanation=explanation,
            suggested_retry_at=suggested,
            failure_category=failure,
        )
