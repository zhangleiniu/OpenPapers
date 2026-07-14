"""P5.5 bounded local claim, effect call, and reconciliation.

This module is the only place that claims a durably retained P5.5 execution
job and calls an injected P5.4-compatible effect. It never holds the global
control-state lease across that effect call: a short lease claims at most one
pending job, the lease is released, the effect runs, and a freshly acquired
lease reconciles the typed observation. Nothing here constructs a real
launcher, network client, or installed-service caller. Retention of a
verified action as a durable job is a separate, lighter-weight concern
handled by `automation/execution_retention.py` so that
`automation/local_control_plane.py` never has to import this module's
process/launcher-adjacent dependency chain.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from automation.control_state import (
    DEFAULT_LEASE_TTL_SECONDS,
    ControlStateRepository,
    ExecutionAttemptClaim,
)
from automation.domain import Writer
from automation.execution_pipeline import P5ExecutionObservation, P5ExecutionStatus


EXECUTION_DISPATCH_OWNER_ID = "local-execution-dispatch"


class ExecutionDispatchError(ValueError):
    """Raised when P5.5 dispatch input is unsafe or untyped."""


class ExistingScraperExecutionEffect(Protocol):
    """Injected P5.4-compatible effect boundary; no real launcher in P5.5."""

    def run(self, job: Mapping[str, Any]) -> P5ExecutionObservation:
        """Run one strict scrape job and return its bounded observation."""


@dataclass(frozen=True)
class ExecutionDispatchOutcome:
    """Bounded, typed result of at most one dispatch attempt."""

    dispatched: bool
    job_id: str | None
    attempt_number: int | None
    disposition: str | None
    observation: dict[str, Any] | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ExecutionDispatchError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def dispatch_one_existing_scraper(
    state_path: Path,
    *,
    clock: Callable[[], datetime] = _utc_now,
    effect: ExistingScraperExecutionEffect,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> ExecutionDispatchOutcome:
    """Claim, run, and reconcile at most one durable existing-scraper job.

    The global control lease is held only while claiming and only while
    reconciling; it is never held across ``effect.run``. An effect exception,
    a returned observation that does not match the claimed job, a
    ``recovery_required`` observation, or any failure while reconciling
    leaves the durable attempt ``in_flight`` and is reported as an
    undispatched-outcome-free dispatch: the caller must not treat this as
    proof that the process never started.
    """
    if not callable(getattr(effect, "run", None)):
        raise ExecutionDispatchError("effect must provide run()")
    if not callable(clock):
        raise ExecutionDispatchError("clock must be callable")

    claimed_at = _utc(clock(), field="dispatch clock")
    with ControlStateRepository(
        Path(state_path),
        writer=Writer.LOCAL_CONTROL_PLANE,
        clock=lambda: claimed_at,
    ) as repository:
        lease = repository.acquire_lease(
            EXECUTION_DISPATCH_OWNER_ID, ttl_seconds=lease_ttl_seconds
        )
        try:
            claim = repository.claim_next_execution_job(
                lease=lease, claimed_at=claimed_at
            )
        finally:
            repository.release_lease(lease)

    if claim is None:
        return ExecutionDispatchOutcome(
            dispatched=False,
            job_id=None,
            attempt_number=None,
            disposition=None,
            observation=None,
        )

    try:
        observation = effect.run(deepcopy(claim.job))
    except Exception:
        return ExecutionDispatchOutcome(
            dispatched=True,
            job_id=claim.job_id,
            attempt_number=claim.attempt_number,
            disposition=None,
            observation=None,
        )
    if (
        not isinstance(observation, P5ExecutionObservation)
        or observation.scrape_job_id != claim.job_id
        or observation.status is P5ExecutionStatus.RECOVERY_REQUIRED
    ):
        return ExecutionDispatchOutcome(
            dispatched=True,
            job_id=claim.job_id,
            attempt_number=claim.attempt_number,
            disposition=None,
            observation=(
                observation.as_dict()
                if isinstance(observation, P5ExecutionObservation)
                else None
            ),
        )

    disposition = "retry" if observation.retry_permitted else "completed"
    try:
        _reconcile(
            state_path,
            claim,
            observation,
            disposition=disposition,
            clock=clock,
            lease_ttl_seconds=lease_ttl_seconds,
        )
    except Exception:
        return ExecutionDispatchOutcome(
            dispatched=True,
            job_id=claim.job_id,
            attempt_number=claim.attempt_number,
            disposition=None,
            observation=observation.as_dict(),
        )
    return ExecutionDispatchOutcome(
        dispatched=True,
        job_id=claim.job_id,
        attempt_number=claim.attempt_number,
        disposition=disposition,
        observation=observation.as_dict(),
    )


def _reconcile(
    state_path: Path,
    claim: ExecutionAttemptClaim,
    observation: P5ExecutionObservation,
    *,
    disposition: str,
    clock: Callable[[], datetime],
    lease_ttl_seconds: int,
) -> None:
    completed_at = _utc(clock(), field="dispatch clock")
    with ControlStateRepository(
        Path(state_path),
        writer=Writer.LOCAL_CONTROL_PLANE,
        clock=lambda: completed_at,
    ) as repository:
        lease = repository.acquire_lease(
            EXECUTION_DISPATCH_OWNER_ID, ttl_seconds=lease_ttl_seconds
        )
        try:
            repository.complete_execution_attempt(
                claim,
                disposition=disposition,
                status=observation.status.value,
                failure_class=(
                    None
                    if observation.failure_class is None
                    else observation.failure_class.value
                ),
                reason_code=observation.reason_code.value,
                result_job_id=observation.result_job_id,
                published=observation.published,
                retry_permitted=observation.retry_permitted,
                paper_count=observation.paper_count,
                valid_pdf_count=observation.valid_pdf_count,
                lease=lease,
                completed_at=completed_at,
            )
        finally:
            repository.release_lease(lease)
