"""Bounded, credential-free scheduler wakeups for local control state."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from automation.contracts import artifact_fingerprint
from automation.control_state import (
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_SCHEDULER_SELECTION_LIMIT,
    ControlStateRepository,
    SchedulerWakeupOutcome,
)
from automation.domain import Writer


LOCAL_SCHEDULER_OWNER_ID = "local-due-scheduler"


def _utc(value: datetime, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def scheduler_wakeup_id(scheduled_for: datetime) -> str:
    """Derive one stable wakeup identity from an aware requested timestamp."""
    scheduled = _utc(scheduled_for, field="scheduled_for")
    fingerprint = artifact_fingerprint({"scheduled_for": _timestamp(scheduled)})
    return f"scheduler-wakeup:{fingerprint}"


def run_scheduler_wakeup(
    state_path: Path,
    *,
    scheduled_for: datetime,
    clock: Callable[[], datetime],
    selection_limit: int = DEFAULT_SCHEDULER_SELECTION_LIMIT,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> SchedulerWakeupOutcome:
    """Run one local due-work selection and return its durable bounded result."""
    observed_now = _utc(clock(), field="scheduler clock")
    scheduled = _utc(scheduled_for, field="scheduled_for")
    if scheduled > observed_now:
        raise ValueError("scheduled_for cannot be later than the scheduler clock")
    frozen_clock = lambda: observed_now
    with ControlStateRepository(
        Path(state_path),
        writer=Writer.LOCAL_CONTROL_PLANE,
        clock=frozen_clock,
    ) as repository:
        lease = repository.acquire_lease(
            LOCAL_SCHEDULER_OWNER_ID,
            ttl_seconds=lease_ttl_seconds,
        )
        try:
            start = repository.begin_scheduler_wakeup(
                scheduler_wakeup_id(scheduled),
                scheduled_for=scheduled,
                due_cutoff_at=observed_now,
                selection_limit=selection_limit,
                lease=lease,
            )
            return repository.complete_scheduler_wakeup(
                start.record.wakeup_id,
                lease=lease,
                completed_at=observed_now,
            )
        finally:
            repository.release_lease(lease)
