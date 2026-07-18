"""Explicit bounded cleanup for managed agent worktrees."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from automation.control_state import (
    DEFAULT_LEASE_TTL_SECONDS,
    AgentExecutionArtifactRecord,
    ControlStateRepository,
)
from automation.domain import Writer


@dataclass(frozen=True)
class WorktreeRetentionPolicy:
    max_retained: int = 10
    max_age: timedelta = timedelta(days=30)
    max_removals_per_run: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.max_retained, int) or isinstance(
            self.max_retained, bool
        ) or self.max_retained < 1:
            raise ValueError("max_retained must be a positive integer")
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        if not isinstance(self.max_removals_per_run, int) or isinstance(
            self.max_removals_per_run, bool
        ) or not 1 <= self.max_removals_per_run <= 20:
            raise ValueError("max_removals_per_run must be between 1 and 20")


@dataclass(frozen=True)
class WorktreeRetentionOutcome:
    run_id: str
    status: str
    worktree_path: Path


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None \
            or value.utcoffset() is None:
        raise ValueError("retention clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def prune_agent_worktrees(
    state_path: Path,
    repository_root: Path,
    runs_root: Path,
    *,
    clock: Callable[[], datetime],
    policy: WorktreeRetentionPolicy = WorktreeRetentionPolicy(),
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> tuple[WorktreeRetentionOutcome, ...]:
    """Remove only old/excess terminal worktrees registered under runs_root."""
    now = _utc(clock())
    repository_root = Path(repository_root).resolve()
    runs_root = Path(runs_root).resolve()
    with ControlStateRepository(
        Path(state_path), writer=Writer.LOCAL_CONTROL_PLANE, clock=clock
    ) as repository:
        lease = repository.acquire_lease(
            "agent-worktree-retention", ttl_seconds=lease_ttl_seconds
        )
        try:
            retained = [
                artifact for artifact in repository.list_agent_execution_artifacts()
                if artifact.lifecycle == "terminal"
                and artifact.retention_status != "removed"
                and Path(artifact.runs_root) == runs_root
            ]
            retained.sort(key=lambda item: (item.completed_at or "", item.run_id))
            newest = {item.run_id for item in retained[-policy.max_retained:]}
            cutoff = now - policy.max_age
            candidates = [
                item for item in retained
                if item.run_id not in newest
                or datetime.fromisoformat(
                    str(item.completed_at).replace("Z", "+00:00")
                ) < cutoff
            ][:policy.max_removals_per_run]
            outcomes: list[WorktreeRetentionOutcome] = []
            for artifact in candidates:
                worktree = Path(artifact.worktree_path)
                status = "removed"
                failure = None
                completed = subprocess.run(
                    ("git", "worktree", "remove", "--force", str(worktree)),
                    cwd=repository_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if completed.returncode != 0:
                    status = "removal_failed"
                    failure = "git_remove_failed"
                repository.record_agent_worktree_retention(
                    artifact.run_id,
                    status=status,
                    recorded_at=_utc(clock()),
                    failure_category=failure,
                    lease=lease,
                )
                outcomes.append(
                    WorktreeRetentionOutcome(artifact.run_id, status, worktree)
                )
            return tuple(outcomes)
        finally:
            repository.release_lease(lease)
