"""P5.4 guarded existing-scraper execution and readiness routing.

This local-only coordinator composes accepted safety, staging, validation, and
immutable-result boundaries through injected effects.  It is not imported by
the installed service and provides no CLI, canonical write, promotion, or
concrete cloud client.
"""

from __future__ import annotations

import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from automation.domain import assert_secret_free
from automation.job_queue import JobType, build_job, validate_job_identity
from automation.job_results import (
    PublishedResultBundle,
    build_job_manifest,
    build_job_result,
    validate_result_bundle,
)
from automation.mac_worker.safety import (
    JournalState,
    LocalJobJournal,
    WorkerSafetyConfig,
    disk_is_sufficient,
)
from automation.staging_executor import (
    CancellationSignal,
    ProcessLauncher,
    StagingCheckpointStatus,
    StagingExecutionStatus,
    StagingExecutorConfig,
    StagingCheckpointStore,
    run_staged_scrape,
)
from automation.staging_validation import (
    CandidateBundle,
    StagingValidationConfig,
    StagingValidationError,
    ValidationBundle,
    capture_staging_candidate,
    validate_staging_candidate,
)


class P5ExecutionError(ValueError):
    """Raised when P5.4 inputs or configuration fail before execution."""


class P5ExecutionStatus(str, Enum):
    """Closed readiness and recovery routes; none promotes canonical data."""

    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"
    RETRY = "retry"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"
    SKIPPED = "skipped"


class P5FailureClass(str, Enum):
    """Failure categories consumed by later policy, never inferred from text."""

    TRANSIENT = "transient"
    OPERATIONAL = "operational"
    STRUCTURAL = "structural"


class P5Reason(str, Enum):
    """Bounded secret-free reason vocabulary for the P5.4 observation."""

    VALIDATED_READY = "validated_ready"
    CANDIDATE_INVALID = "candidate_invalid"
    VALIDATION_FAILED_CLOSED = "validation_failed_closed"
    PROCESS_FAILED = "process_failed"
    PROCESS_TIMED_OUT = "process_timed_out"
    PROCESS_CANCELLED = "process_cancelled"
    PROCESS_AMBIGUOUS = "process_ambiguous"
    VENUE_YEAR_BUSY = "venue_year_busy"
    ACTIVE_CLAIM = "active_claim"
    INSUFFICIENT_DISK = "insufficient_disk"
    RESULT_PUBLISH_FAILED = "result_publish_failed"
    COMPLETION_UNCERTAIN = "completion_uncertain"
    DUPLICATE_COMPLETED = "duplicate_completed"


@dataclass(frozen=True)
class P5ExecutionConfig:
    """Coherent existing safety, staging, validation, and worker bindings."""

    worker_safety: WorkerSafetyConfig
    staging_executor: StagingExecutorConfig
    staging_validation: StagingValidationConfig
    worker_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.worker_safety, WorkerSafetyConfig):
            raise TypeError("worker_safety must be WorkerSafetyConfig")
        if not isinstance(self.staging_executor, StagingExecutorConfig):
            raise TypeError("staging_executor must be StagingExecutorConfig")
        if not isinstance(self.staging_validation, StagingValidationConfig):
            raise TypeError("staging_validation must be StagingValidationConfig")
        if (
            not isinstance(self.worker_id, str)
            or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{2,127}", self.worker_id)
            is None
        ):
            raise ValueError("worker_id is invalid")
        assert_secret_free({"worker_id": self.worker_id})


@dataclass(frozen=True)
class P5ExecutionObservation:
    """Bounded route with no paths, exception text, or promotion authority."""

    status: P5ExecutionStatus
    failure_class: P5FailureClass | None
    reason_code: P5Reason
    scrape_job_id: str
    result_job_id: str | None
    published: bool
    retry_permitted: bool
    paper_count: int | None = None
    valid_pdf_count: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, P5ExecutionStatus):
            raise TypeError("P5.4 status must be P5ExecutionStatus")
        if self.failure_class is not None and not isinstance(
            self.failure_class, P5FailureClass
        ):
            raise TypeError("P5.4 failure_class must be P5FailureClass or None")
        if not isinstance(self.reason_code, P5Reason):
            raise TypeError("P5.4 reason_code must be P5Reason")
        if (self.status in {P5ExecutionStatus.READY, P5ExecutionStatus.SKIPPED}) != (
            self.failure_class is None
        ):
            raise ValueError("P5.4 status and failure class conflict")
        if self.retry_permitted != (
            self.status in {P5ExecutionStatus.RETRY, P5ExecutionStatus.CANCELLED}
        ):
            raise ValueError("P5.4 status and retry policy conflict")
        if self.published and self.result_job_id is None:
            raise ValueError("published P5.4 output requires a result job")
        for value in (self.paper_count, self.valid_pdf_count):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError("P5.4 counts must be non-negative integers or None")
        assert_secret_free(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "failure_class": (
                None if self.failure_class is None else self.failure_class.value
            ),
            "reason_code": self.reason_code.value,
            "scrape_job_id": self.scrape_job_id,
            "result_job_id": self.result_job_id,
            "published": self.published,
            "retry_permitted": self.retry_permitted,
            "paper_count": self.paper_count,
            "valid_pdf_count": self.valid_pdf_count,
        }


class ImmutableResultPublisher(Protocol):
    """Minimal injected P4.4 publication surface."""

    def publish(
        self,
        job: Mapping[str, Any],
        manifest: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> PublishedResultBundle:
        """Create or exactly replay one strict immutable result bundle."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        raise P5ExecutionError("P5.4 root could not be normalized") from None


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _validate_config(config: P5ExecutionConfig) -> None:
    if not isinstance(config, P5ExecutionConfig):
        raise TypeError("config must be P5ExecutionConfig")
    executor = config.staging_executor
    validation = config.staging_validation
    safety = config.worker_safety
    if (
        executor.staging_root != validation.staging_root
        or executor.staging_root != safety.data_root
        or executor.canonical_data_root != validation.canonical_data_root
        or executor.timeout_seconds != safety.timeout_seconds
        or executor.cancellation_grace_seconds
        != safety.cancellation_grace_seconds
    ):
        raise P5ExecutionError("P5.4 component configuration is incoherent")
    roots = (
        safety.state_root,
        executor.repository_root,
        executor.staging_root,
        validation.artifact_root,
        executor.canonical_data_root,
    )
    if any(_normalized(path) != path for path in roots):
        raise P5ExecutionError("P5.4 roots must be normalized")
    for index, left in enumerate(roots):
        for right_index, right in enumerate(roots[index + 1 :], start=index + 1):
            if {index, right_index} == {1, 4}:
                # The repository's ordinary canonical root is repository/data.
                # P5.2 requires staging to be disjoint from both roots but does
                # not require the repository and its canonical child to be
                # disjoint from one another.
                continue
            if _within(left, right) or _within(right, left):
                raise P5ExecutionError("P5.4 state and data roots must be disjoint")


def _parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise P5ExecutionError("retained P5.4 timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise P5ExecutionError("retained P5.4 timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise P5ExecutionError("retained P5.4 timestamp is invalid")
    return parsed.astimezone(timezone.utc)


def build_candidate_validation_job(
    scrape_job: Mapping[str, Any], candidate_manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Derive one strict validation job from the exact captured candidate."""
    validate_job_identity(scrape_job)
    if JobType(scrape_job["job_type"]) is not JobType.SCRAPE_EXISTING:
        raise P5ExecutionError("P5.4 accepts only an existing-scraper job")
    if not isinstance(candidate_manifest, Mapping):
        raise TypeError("candidate_manifest must be a mapping")
    manifest_id = candidate_manifest.get("manifest_id")
    if not isinstance(manifest_id, str):
        raise P5ExecutionError("candidate manifest identity is invalid")
    payload = scrape_job["payload"]
    return build_job(
        request_id=f"validation:{scrape_job['job_fingerprint']}",
        job_type=JobType.VALIDATE_CANDIDATE,
        venue_id=scrape_job["venue_id"],
        year=scrape_job["year"],
        requested_by=scrape_job["requested_by"],
        input_artifact_ids=(manifest_id,),
        payload={
            "candidate_manifest_id": manifest_id,
            "completeness_level": payload["completeness_level"],
            "require_pdfs": (
                payload["download_pdfs"]
                or payload["completeness_level"] == "archival"
            ),
            "expected_count": payload["expected_count"],
        },
    )


def _observe(
    scrape_job: Mapping[str, Any],
    status: P5ExecutionStatus,
    reason: P5Reason,
    *,
    failure_class: P5FailureClass | None,
    result_job_id: str | None = None,
    published: bool = False,
    retry_permitted: bool = False,
    paper_count: int | None = None,
    valid_pdf_count: int | None = None,
) -> P5ExecutionObservation:
    observation = P5ExecutionObservation(
        status=status,
        failure_class=failure_class,
        reason_code=reason,
        scrape_job_id=scrape_job["job_id"],
        result_job_id=result_job_id,
        published=published,
        retry_permitted=retry_permitted,
        paper_count=paper_count,
        valid_pdf_count=valid_pdf_count,
    )
    assert_secret_free(observation.as_dict())
    return observation


def _duration_seconds(manifest: Mapping[str, Any], completed_at: str) -> float:
    delta = _parse_time(completed_at) - _parse_time(manifest["created_at"])
    return max(0.0, delta.total_seconds())


def _validation_result_bundle(
    scrape_job: Mapping[str, Any],
    config: P5ExecutionConfig,
    candidate: CandidateBundle | None,
    validation: ValidationBundle | None,
    *,
    process_succeeded_at: str,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    P5ExecutionStatus,
    P5Reason,
    int | None,
    int | None,
]:
    if candidate is None:
        result_job = deepcopy(dict(scrape_job))
        manifest = build_job_manifest(
            result_job, created_at=process_succeeded_at, artifacts=[]
        )
        completed_at = process_succeeded_at
        result = build_job_result(
            result_job,
            manifest,
            worker_id=config.worker_id,
            completed_at=completed_at,
            status="failed",
            error_code="structural_validation_error",
            error_summary="staged candidate failed closed validation checks",
            duration_seconds=0.0,
            paper_count=None,
            valid_pdf_count=None,
        )
        return (
            result_job,
            manifest,
            result,
            P5ExecutionStatus.FAILED,
            P5Reason.VALIDATION_FAILED_CLOSED,
            None,
            None,
        )

    result_job = build_candidate_validation_job(scrape_job, candidate.manifest)
    if validation is None:
        manifest = build_job_manifest(
            result_job, created_at=process_succeeded_at, artifacts=[]
        )
        completed_at = process_succeeded_at
        result = build_job_result(
            result_job,
            manifest,
            worker_id=config.worker_id,
            completed_at=completed_at,
            status="failed",
            error_code="structural_validation_error",
            error_summary="staged candidate failed closed validation checks",
            duration_seconds=0.0,
            paper_count=None,
            valid_pdf_count=None,
        )
        return (
            result_job,
            manifest,
            result,
            P5ExecutionStatus.FAILED,
            P5Reason.VALIDATION_FAILED_CLOSED,
            None,
            None,
        )

    manifest = deepcopy(validation.manifest)
    report = validation.report
    completed_at = report["validated_at"]
    paper_count = report["metrics"]["paper_count"]
    valid_pdf_count = report["metrics"]["valid_pdf_count"]
    valid = report["status"] == "valid"
    result = build_job_result(
        result_job,
        manifest,
        worker_id=config.worker_id,
        completed_at=completed_at,
        status="succeeded" if valid else "failed",
        error_code=None if valid else "structural_candidate_invalid",
        error_summary=None if valid else "candidate failed independent validation",
        duration_seconds=_duration_seconds(manifest, completed_at),
        paper_count=paper_count if valid else None,
        valid_pdf_count=valid_pdf_count if valid else None,
    )
    return (
        result_job,
        manifest,
        result,
        P5ExecutionStatus.READY if valid else P5ExecutionStatus.PARTIAL,
        P5Reason.VALIDATED_READY if valid else P5Reason.CANDIDATE_INVALID,
        paper_count,
        valid_pdf_count,
    )


def run_existing_scraper_pipeline(
    scrape_job: Mapping[str, Any],
    config: P5ExecutionConfig,
    launcher: ProcessLauncher,
    publisher: ImmutableResultPublisher,
    *,
    cancellation: CancellationSignal | None = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    clock: Callable[[], datetime] = _utc_now,
) -> P5ExecutionObservation:
    """Run one fake/injected P5.4 composition without promotion authority."""
    if not isinstance(scrape_job, Mapping):
        raise TypeError("scrape_job must be a mapping")
    validate_job_identity(scrape_job)
    if JobType(scrape_job["job_type"]) is not JobType.SCRAPE_EXISTING:
        raise P5ExecutionError("P5.4 accepts only an existing-scraper job")
    _validate_config(config)
    if not callable(getattr(launcher, "start", None)):
        raise TypeError("launcher must provide start")
    if not callable(getattr(publisher, "publish", None)):
        raise TypeError("publisher must provide publish")
    if not callable(disk_usage) or not callable(clock):
        raise TypeError("disk_usage and clock must be callable")

    journal = LocalJobJournal(config.worker_safety.state_root)
    with journal.try_venue_year_lock(scrape_job) as acquired:
        if not acquired:
            return _observe(
                scrape_job,
                P5ExecutionStatus.RETRY,
                P5Reason.VENUE_YEAR_BUSY,
                failure_class=P5FailureClass.OPERATIONAL,
                retry_permitted=True,
            )
        state = journal.inspect(scrape_job)
        if state is JournalState.COMPLETED:
            return _observe(
                scrape_job,
                P5ExecutionStatus.SKIPPED,
                P5Reason.DUPLICATE_COMPLETED,
                failure_class=None,
            )
        if state is JournalState.ACTIVE or journal.has_active_venue_year_claim(
            scrape_job
        ):
            return _observe(
                scrape_job,
                P5ExecutionStatus.RECOVERY_REQUIRED,
                P5Reason.ACTIVE_CLAIM,
                failure_class=P5FailureClass.OPERATIONAL,
            )
        if not disk_is_sufficient(
            config.worker_safety.data_root,
            config.worker_safety.disk_policy,
            disk_usage,
        ):
            return _observe(
                scrape_job,
                P5ExecutionStatus.RETRY,
                P5Reason.INSUFFICIENT_DISK,
                failure_class=P5FailureClass.OPERATIONAL,
                retry_permitted=True,
            )

        journal.create_claim(scrape_job)
        try:
            process = run_staged_scrape(
                scrape_job,
                config.staging_executor,
                launcher,
                cancellation=cancellation,
                clock=clock,
            )
        except Exception:
            return _observe(
                scrape_job,
                P5ExecutionStatus.RECOVERY_REQUIRED,
                P5Reason.PROCESS_AMBIGUOUS,
                failure_class=P5FailureClass.OPERATIONAL,
            )

        if process.status is StagingExecutionStatus.RECOVERY_REQUIRED:
            return _observe(
                scrape_job,
                P5ExecutionStatus.RECOVERY_REQUIRED,
                P5Reason.PROCESS_AMBIGUOUS,
                failure_class=P5FailureClass.OPERATIONAL,
            )
        if process.status in {
            StagingExecutionStatus.FAILED,
            StagingExecutionStatus.TIMED_OUT,
            StagingExecutionStatus.CANCELLED,
        }:
            journal.clear_claim(scrape_job)
            route = {
                StagingExecutionStatus.FAILED: (
                    P5ExecutionStatus.RETRY,
                    P5Reason.PROCESS_FAILED,
                ),
                StagingExecutionStatus.TIMED_OUT: (
                    P5ExecutionStatus.RETRY,
                    P5Reason.PROCESS_TIMED_OUT,
                ),
                StagingExecutionStatus.CANCELLED: (
                    P5ExecutionStatus.CANCELLED,
                    P5Reason.PROCESS_CANCELLED,
                ),
            }[process.status]
            return _observe(
                scrape_job,
                route[0],
                route[1],
                failure_class=P5FailureClass.TRANSIENT,
                retry_permitted=True,
            )
        if process.status not in {
            StagingExecutionStatus.PROCESS_SUCCEEDED,
            StagingExecutionStatus.SKIPPED,
        }:
            return _observe(
                scrape_job,
                P5ExecutionStatus.RECOVERY_REQUIRED,
                P5Reason.PROCESS_AMBIGUOUS,
                failure_class=P5FailureClass.OPERATIONAL,
            )

        job_root = (
            config.staging_executor.staging_root / scrape_job["job_fingerprint"]
        )
        checkpoint = StagingCheckpointStore(job_root, scrape_job).read()
        if checkpoint.status is not StagingCheckpointStatus.PROCESS_SUCCEEDED:
            return _observe(
                scrape_job,
                P5ExecutionStatus.RECOVERY_REQUIRED,
                P5Reason.PROCESS_AMBIGUOUS,
                failure_class=P5FailureClass.OPERATIONAL,
            )
        candidate: CandidateBundle | None = None
        validation: ValidationBundle | None = None
        try:
            candidate = capture_staging_candidate(
                scrape_job, config.staging_validation, clock=clock
            )
            validation_job = build_candidate_validation_job(
                scrape_job, candidate.manifest
            )
            validation = validate_staging_candidate(
                validation_job,
                scrape_job,
                candidate,
                config.staging_validation,
                clock=clock,
            )
        except StagingValidationError:
            pass
        except Exception:
            return _observe(
                scrape_job,
                P5ExecutionStatus.RECOVERY_REQUIRED,
                P5Reason.COMPLETION_UNCERTAIN,
                failure_class=P5FailureClass.OPERATIONAL,
            )

        (
            result_job,
            manifest,
            result,
            routed_status,
            routed_reason,
            paper_count,
            valid_pdf_count,
        ) = _validation_result_bundle(
            scrape_job,
            config,
            candidate,
            validation,
            process_succeeded_at=checkpoint.updated_at,
        )
        validate_result_bundle(result_job, manifest, result)
        try:
            receipt = publisher.publish(result_job, manifest, result)
            if (
                not isinstance(receipt, PublishedResultBundle)
                or receipt.job_id != result_job["job_id"]
            ):
                raise P5ExecutionError("publisher returned an invalid receipt")
        except Exception:
            journal.clear_claim(scrape_job)
            return _observe(
                scrape_job,
                P5ExecutionStatus.RETRY,
                P5Reason.RESULT_PUBLISH_FAILED,
                failure_class=P5FailureClass.OPERATIONAL,
                result_job_id=result_job["job_id"],
                retry_permitted=True,
                paper_count=paper_count,
                valid_pdf_count=valid_pdf_count,
            )
        try:
            journal.mark_completed(scrape_job)
        except Exception:
            return _observe(
                scrape_job,
                P5ExecutionStatus.RECOVERY_REQUIRED,
                P5Reason.COMPLETION_UNCERTAIN,
                failure_class=P5FailureClass.OPERATIONAL,
                result_job_id=result_job["job_id"],
                published=True,
                paper_count=paper_count,
                valid_pdf_count=valid_pdf_count,
            )
        return _observe(
            scrape_job,
            routed_status,
            routed_reason,
            failure_class=(
                None
                if routed_status is P5ExecutionStatus.READY
                else P5FailureClass.STRUCTURAL
            ),
            result_job_id=result_job["job_id"],
            published=True,
            paper_count=paper_count,
            valid_pdf_count=valid_pdf_count,
        )
