"""P3.4 local case/reminder integration with persistent shadow output only."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from automation.cases import CaseObservation
from automation.control_state import (
    CaseWriteOutcome,
    ControlStateRepository,
    LeaseHandle,
    NotificationWriteOutcome,
)
from automation.domain import ActionType, BlockerCode
from automation.lifecycle import (
    ActionIntent,
    CasePayload,
    TransitionNoticePayload,
)
from automation.notifications import (
    build_digest_notification,
    build_immediate_notification,
    reminder_source_id,
)
from automation.reminders import CaseDigest, DigestGroup, build_case_digest


class NotificationIntegrationError(ValueError):
    """Raised when P3.4 integration input is not safe typed action data."""


@dataclass(frozen=True)
class ActionIntegrationOutcome:
    """Durable case writes and pending outputs derived from local actions."""

    case_writes: tuple[CaseWriteOutcome, ...]
    notification_writes: tuple[NotificationWriteOutcome, ...]
    ignored_action_ids: tuple[str, ...]


@dataclass(frozen=True)
class DigestIntegrationOutcome:
    """One filtered due digest and its optional pending shadow output."""

    digest: CaseDigest
    notification_write: NotificationWriteOutcome | None
    claimed_source_ids: tuple[str, ...]


def _case_event_id(action_id: str, blocker: str) -> str:
    fingerprint = hashlib.sha256(
        f"{action_id}\0{blocker}".encode("utf-8")
    ).hexdigest()
    return f"case-event:{fingerprint}"


def _case_summary(blocker: str, verification_status: str) -> str:
    return (
        f"Automation observed blocker {blocker!r}; deterministic verification "
        f"status is {verification_status!r}."
    )


def _transition_summary(payload: TransitionNoticePayload) -> str:
    return (
        f"Lifecycle transition {payload.transition_id!r} changed state from "
        f"{payload.previous_state!r} to {payload.new_state!r}."
    )


def integrate_action_intents(
    repository: ControlStateRepository,
    actions: Sequence[ActionIntent],
    *,
    lease: LeaseHandle,
    occurred_at: datetime | str,
    run_ids: Sequence[str] = (),
) -> ActionIntegrationOutcome:
    """Persist cases and pending immediate intents without delivery authority.

    Each case event is committed before its notification registration begins.
    That deliberate transaction boundary lets exact replay recover a missing
    shadow output without erasing or duplicating the durable case.
    """
    if not isinstance(repository, ControlStateRepository):
        raise NotificationIntegrationError(
            "repository must be a ControlStateRepository"
        )
    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise NotificationIntegrationError("actions must be a typed sequence")

    case_writes: list[CaseWriteOutcome] = []
    notification_writes: list[NotificationWriteOutcome] = []
    ignored_action_ids: list[str] = []
    seen: dict[str, ActionIntent] = {}
    for action in actions:
        if not isinstance(action, ActionIntent):
            raise NotificationIntegrationError(
                "actions must contain only P2.5 ActionIntent values"
            )
        previous = seen.get(action.action_id)
        if previous is not None:
            if previous != action:
                raise NotificationIntegrationError(
                    "one action ID cannot have different meanings"
                )
            continue
        seen[action.action_id] = action

        if action.action_type is ActionType.NOTIFY_TRANSITION:
            if not isinstance(action.payload, TransitionNoticePayload):
                raise NotificationIntegrationError(
                    "transition action has the wrong payload"
                )
            intent = build_immediate_notification(
                event_id=action.action_id,
                occurred_at=occurred_at,
                venue_id=action.venue_id,
                year=action.year,
                summary=_transition_summary(action.payload),
                evidence_ids=action.evidence_ids,
                run_ids=run_ids,
            )
            notification_writes.append(
                repository.register_notification_intent(
                    intent,
                    lease=lease,
                    registered_at=occurred_at,
                )
            )
            continue

        if action.action_type is ActionType.CREATE_OR_UPDATE_CASE:
            if not isinstance(action.payload, CasePayload):
                raise NotificationIntegrationError(
                    "case action has the wrong payload"
                )
            blockers = tuple(sorted(set(action.payload.blocker_codes)))
            if not blockers:
                raise NotificationIntegrationError(
                    "case action must contain at least one blocker"
                )
            if len(blockers) != len(action.payload.blocker_codes):
                raise NotificationIntegrationError(
                    "case action blocker codes must be unique"
                )
            for blocker_value in blockers:
                try:
                    blocker = BlockerCode(blocker_value)
                except (TypeError, ValueError) as exc:
                    raise NotificationIntegrationError(
                        "case action contains an invalid blocker"
                    ) from exc
                observation = CaseObservation(
                    event_id=_case_event_id(action.action_id, blocker.value),
                    venue_id=action.venue_id,
                    year=action.year,
                    blocker=blocker,
                    summary=_case_summary(
                        blocker.value, action.payload.verification_status
                    ),
                    evidence_ids=action.evidence_ids,
                    observed_at=(
                        occurred_at.isoformat()
                        if isinstance(occurred_at, datetime)
                        else occurred_at
                    ),
                )
                case_write = repository.observe_case(observation, lease=lease)
                case_writes.append(case_write)
                if not case_write.event.meaningful_change:
                    continue
                event = case_write.event.event
                intent = build_immediate_notification(
                    event_id=case_write.event.event_id,
                    occurred_at=case_write.event.event_at,
                    venue_id=case_write.record.venue_id,
                    year=case_write.record.year,
                    summary=case_write.record.state["summary"],
                    evidence_ids=tuple(event["evidence_ids"]),
                    run_ids=run_ids,
                )
                notification_writes.append(
                    repository.register_notification_intent(
                        intent,
                        lease=lease,
                        registered_at=case_write.event.event_at,
                    )
                )
            continue

        ignored_action_ids.append(action.action_id)

    return ActionIntegrationOutcome(
        case_writes=tuple(case_writes),
        notification_writes=tuple(notification_writes),
        ignored_action_ids=tuple(ignored_action_ids),
    )


def _unclaimed_digest(
    repository: ControlStateRepository,
    digest: CaseDigest,
) -> tuple[CaseDigest, tuple[str, ...]]:
    groups: list[DigestGroup] = []
    claimed: list[str] = []
    for group in digest.groups:
        items = []
        for item in group.items:
            source_id = reminder_source_id(
                item.case_id,
                item.cadence,
                item.slot,
                item.due_at,
            )
            if repository.get_notification_by_source(source_id) is not None:
                claimed.append(source_id)
            else:
                items.append(item)
        if items:
            groups.append(DigestGroup(cadence=group.cadence, items=tuple(items)))
    filtered = CaseDigest(
        generated_at=digest.generated_at,
        groups=tuple(groups),
        due_count=sum(len(group.items) for group in groups),
    )
    return filtered, tuple(sorted(claimed))


def persist_due_digest_shadow(
    repository: ControlStateRepository,
    *,
    policy: Mapping[str, Any],
    lease: LeaseHandle,
    now: datetime,
    run_ids: Sequence[str] = (),
) -> DigestIntegrationOutcome:
    """Persist one grouped pending digest for all unclaimed due case slots."""
    if not isinstance(repository, ControlStateRepository):
        raise NotificationIntegrationError(
            "repository must be a ControlStateRepository"
        )
    current = tuple(record.state for record in repository.list_cases())
    digest = build_case_digest(current, policy, now)
    filtered, claimed = _unclaimed_digest(repository, digest)
    if filtered.due_count == 0:
        return DigestIntegrationOutcome(
            digest=filtered,
            notification_write=None,
            claimed_source_ids=claimed,
        )
    intent = build_digest_notification(filtered, run_ids=run_ids)
    write = repository.register_notification_intent(
        intent,
        lease=lease,
        registered_at=now,
    )
    return DigestIntegrationOutcome(
        digest=filtered,
        notification_write=write,
        claimed_source_ids=claimed,
    )
