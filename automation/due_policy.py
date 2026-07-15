"""Effect-free durable policy for due coding-agent checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from automation.control_state import (
    DEFAULT_LEASE_TTL_SECONDS,
    AgentRunClaim,
    AgentRunClaimOutcome,
    AgentScheduleRecord,
    AgentScheduleError,
    ControlStateRepository,
)
from automation.domain import Writer


AGENT_POLICY_OWNER_ID = "agent-due-policy"


@dataclass(frozen=True)
class DuePolicy:
    """Conservative local limits applied before and after an agent run."""

    default_not_ready_delay: timedelta = timedelta(days=3)
    minimum_retry_delay: timedelta = timedelta(hours=6)
    max_suggested_retry_delay: timedelta = timedelta(days=30)
    failure_backoff: tuple[timedelta, ...] = (
        timedelta(days=1),
        timedelta(days=3),
        timedelta(days=7),
    )
    max_consecutive_failures: int = 3
    monthly_run_limit: int = 10
    systemic_failure_threshold: int = 3
    systemic_failure_window: timedelta = timedelta(hours=24)
    systemic_circuit_delay: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        for value, field in (
            (self.default_not_ready_delay, "default_not_ready_delay"),
            (self.minimum_retry_delay, "minimum_retry_delay"),
            (self.max_suggested_retry_delay, "max_suggested_retry_delay"),
            (self.systemic_failure_window, "systemic_failure_window"),
            (self.systemic_circuit_delay, "systemic_circuit_delay"),
        ):
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{field} must be positive")
        if self.default_not_ready_delay < self.minimum_retry_delay:
            raise ValueError("default_not_ready_delay is below the cooldown")
        if self.max_suggested_retry_delay < self.minimum_retry_delay:
            raise ValueError("max_suggested_retry_delay is below the cooldown")
        if (
            not isinstance(self.failure_backoff, tuple)
            or not self.failure_backoff
            or any(
                not isinstance(delay, timedelta) or delay <= timedelta(0)
                for delay in self.failure_backoff
            )
            or tuple(sorted(self.failure_backoff)) != self.failure_backoff
            or self.failure_backoff[0] < self.minimum_retry_delay
        ):
            raise ValueError("failure_backoff must be a nonempty sorted tuple")
        for value, field in (
            (self.max_consecutive_failures, "max_consecutive_failures"),
            (self.monthly_run_limit, "monthly_run_limit"),
            (self.systemic_failure_threshold, "systemic_failure_threshold"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field} must be a positive integer")


@dataclass(frozen=True)
class AgentRunResult:
    """Small machine-readable result returned by a future coding-agent run."""

    disposition: str
    explanation: str
    suggested_retry_at: datetime | None = None
    failure_category: str | None = None


def _utc(value: datetime, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def claim_due_agent_run(
    state_path: Path,
    *,
    clock: Callable[[], datetime],
    policy: DuePolicy = DuePolicy(),
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> AgentRunClaimOutcome:
    """Claim at most one due run without invoking an external agent."""
    if not isinstance(policy, DuePolicy):
        raise ValueError("policy must be a DuePolicy")
    now = _utc(clock(), field="agent policy clock")
    frozen_clock = lambda: now
    with ControlStateRepository(
        Path(state_path),
        writer=Writer.LOCAL_CONTROL_PLANE,
        clock=frozen_clock,
    ) as repository:
        lease = repository.acquire_lease(
            AGENT_POLICY_OWNER_ID, ttl_seconds=lease_ttl_seconds
        )
        try:
            return repository.claim_due_agent_run(
                claimed_at=now,
                monthly_run_limit=policy.monthly_run_limit,
                systemic_failure_threshold=policy.systemic_failure_threshold,
                systemic_failure_window=policy.systemic_failure_window,
                systemic_circuit_delay=policy.systemic_circuit_delay,
                lease=lease,
            )
        finally:
            repository.release_lease(lease)


def complete_agent_run(
    state_path: Path,
    claim: AgentRunClaim,
    result: AgentRunResult,
    *,
    clock: Callable[[], datetime],
    policy: DuePolicy = DuePolicy(),
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    changed_files: tuple[str, ...] | None = None,
    returncode: int | None = None,
    timed_out: bool = False,
) -> AgentScheduleRecord:
    """Apply one future coding-agent result to its durable schedule."""
    if not isinstance(policy, DuePolicy):
        raise ValueError("policy must be a DuePolicy")
    if not isinstance(result, AgentRunResult):
        raise ValueError("result must be an AgentRunResult")
    if result.disposition not in {
        "success", "not_ready", "needs_human", "failed"
    }:
        raise ValueError("agent result disposition is invalid")
    now = _utc(clock(), field="agent policy clock")
    suggested = result.suggested_retry_at
    if suggested is not None:
        suggested = _utc(suggested, field="suggested_retry_at")
    frozen_clock = lambda: now
    with ControlStateRepository(
        Path(state_path),
        writer=Writer.LOCAL_CONTROL_PLANE,
        clock=frozen_clock,
    ) as repository:
        lease = repository.acquire_lease(
            AGENT_POLICY_OWNER_ID, ttl_seconds=lease_ttl_seconds
        )
        try:
            schedule = repository.get_agent_schedule(claim.venue_id, claim.year)
            if schedule is None:
                raise AgentScheduleError("agent schedule is not retained")
            next_check_at = None
            retained_suggestion = None
            pause_after_failure = False
            if result.disposition == "not_ready":
                if (
                    suggested is not None
                    and now + policy.minimum_retry_delay <= suggested
                    <= now + policy.max_suggested_retry_delay
                ):
                    next_check_at = suggested
                    retained_suggestion = suggested
                else:
                    next_check_at = now + policy.default_not_ready_delay
            elif result.disposition == "failed":
                failure_number = schedule.consecutive_failures + 1
                pause_after_failure = (
                    failure_number >= policy.max_consecutive_failures
                )
                if not pause_after_failure:
                    index = min(failure_number, len(policy.failure_backoff)) - 1
                    next_check_at = now + policy.failure_backoff[index]
            return repository.complete_agent_run_attempt(
                claim,
                disposition=result.disposition,
                explanation=result.explanation,
                completed_at=now,
                next_check_at=next_check_at,
                suggested_retry_at=retained_suggestion,
                failure_category=result.failure_category,
                pause_after_failure=pause_after_failure,
                lease=lease,
                changed_files=changed_files,
                returncode=returncode,
                timed_out=timed_out,
            )
        finally:
            repository.release_lease(lease)


def resume_agent_schedule(
    state_path: Path,
    venue_id: str,
    year: int,
    *,
    clock: Callable[[], datetime],
    next_check_at: datetime | None = None,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> AgentScheduleRecord:
    """Resume a failure-paused target after explicit operator review."""
    now = _utc(clock(), field="agent policy clock")
    due = now if next_check_at is None else _utc(
        next_check_at, field="next_check_at"
    )
    frozen_clock = lambda: now
    with ControlStateRepository(
        Path(state_path),
        writer=Writer.LOCAL_CONTROL_PLANE,
        clock=frozen_clock,
    ) as repository:
        lease = repository.acquire_lease(
            AGENT_POLICY_OWNER_ID, ttl_seconds=lease_ttl_seconds
        )
        try:
            return repository.resume_agent_schedule(
                venue_id,
                year,
                next_check_at=due,
                resumed_at=now,
                lease=lease,
            )
        finally:
            repository.release_lease(lease)
