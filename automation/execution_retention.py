"""P5.5 durable retention of verified queue_existing_scraper actions.

This module only turns a P2.5 ``ActionIntent`` already produced by a
lease-protected local reduction into a durably retained execution job. It is
deliberately free of any process, subprocess, launcher, or
mac_worker/staging-execution import so it can be safely composed inside
`local_control_plane.py`. The separate bounded dispatch step that claims a
retained job and calls an injected P5.4 effect lives in
`automation/execution_dispatch.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from automation.control_state import (
    ControlStateRepository,
    ExecutionRetentionOutcome,
    LeaseHandle,
)
from automation.domain import ActionType
from automation.lifecycle import ActionIntent


class ExecutionRetentionError(ValueError):
    """Raised when P5.5 retention input is unsafe or not a typed action."""


def retain_execution_actions(
    repository: ControlStateRepository,
    actions: Sequence[ActionIntent],
    *,
    source_verification_id: str,
    lease: LeaseHandle,
    enqueued_at: datetime | str,
) -> tuple[ExecutionRetentionOutcome, ...]:
    """Persist only queue_existing_scraper actions as durable execution jobs.

    This must be called from inside the same lease-protected local reduction
    that produced ``actions``, so a caller cannot submit arbitrary job JSON
    or turn discovery/verification output directly into execution authority.
    """
    if not isinstance(repository, ControlStateRepository):
        raise ExecutionRetentionError(
            "repository must be a ControlStateRepository"
        )
    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise ExecutionRetentionError("actions must be a typed sequence")

    outcomes: list[ExecutionRetentionOutcome] = []
    seen: dict[str, ActionIntent] = {}
    for action in actions:
        if not isinstance(action, ActionIntent):
            raise ExecutionRetentionError(
                "actions must contain only P2.5 ActionIntent values"
            )
        if action.action_type is not ActionType.QUEUE_EXISTING_SCRAPER:
            continue
        previous = seen.get(action.action_id)
        if previous is not None:
            if previous != action:
                raise ExecutionRetentionError(
                    "one action ID cannot have different meanings"
                )
            continue
        seen[action.action_id] = action
        outcomes.append(
            repository.retain_existing_scraper_action(
                action,
                source_verification_id=source_verification_id,
                lease=lease,
                enqueued_at=enqueued_at,
            )
        )
    return tuple(outcomes)
