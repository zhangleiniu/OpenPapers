"""Thin P2.5 composition of retained verification and control state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from automation.control_state import (
    ControlStateRepository,
    LeaseHandle,
    StateWriteOutcome,
    VerificationRecord,
)
from automation.lifecycle import (
    ReductionOutcome,
    initial_conference_state,
    reduce_verification,
)


class ControlPlaneError(ValueError):
    """Raised when a coordinator input is not retained control-state history."""


@dataclass(frozen=True)
class VerificationConsumptionOutcome:
    """Persistent state write plus inert actions for one retained record."""

    reduction: ReductionOutcome
    state_write: StateWriteOutcome


def consume_verification_record(
    repository: ControlStateRepository,
    record: VerificationRecord,
    *,
    catalog: Mapping[str, Any],
    policy: Mapping[str, Any],
    lease: LeaseHandle,
) -> VerificationConsumptionOutcome:
    """Reduce and optimistically persist one retained verification record."""
    retained = next(
        (
            candidate
            for candidate in repository.replay_verifications(
                venue_id=record.result.get("venue_id"),
                year=record.result.get("year"),
            )
            if candidate.sequence == record.sequence
        ),
        None,
    )
    if retained is None or retained != record:
        raise ControlPlaneError(
            "verification record is absent from retained repository history"
        )
    record = retained
    current = repository.get_conference_state(
        record.result["venue_id"], record.result["year"]
    )
    state = (
        current.state
        if current is not None
        else initial_conference_state(
            catalog,
            record.result["venue_id"],
            record.result["year"],
            at=record.result["verified_at"],
        )
    )
    reduction = reduce_verification(
        state,
        record.discovery,
        record.request,
        record.result,
        catalog=catalog,
        policy=policy,
    )
    state_write = repository.store_conference_state(
        reduction.state,
        expected_revision=current.revision if current is not None else 0,
        lease=lease,
        stored_at=record.received_at,
    )
    return VerificationConsumptionOutcome(reduction, state_write)
