"""Pure P3.2 unresolved-case reminder aging and grouped digest data."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from automation.cases import CaseDomainError, validate_case_state
from automation.contracts import (
    ContractName,
    ContractValidationError,
    validate_contract,
)
from automation.domain import SecretBoundaryError


class ReminderPolicyError(ValueError):
    """Raised when reminder inputs or policy fail closed."""


class ReminderCadence(str, Enum):
    """Configured digest urgency from most active to least frequent."""

    WEEKLY = "weekly"
    MONTHLY = "monthly"
    DORMANT = "dormant"


@dataclass(frozen=True)
class ReminderAssessment:
    """One defensive case-state projection and its current due slot."""

    state: dict[str, Any]
    age_days: int
    status_changed: bool
    cadence: ReminderCadence | None
    slot: int | None
    due_at: datetime | None


@dataclass(frozen=True)
class DigestItem:
    """Validated case data needed by a later notification boundary."""

    case_id: str
    venue_id: str
    year: int
    blocker: str
    status: str
    summary: str
    evidence_ids: tuple[str, ...]
    last_meaningful_change_at: datetime
    age_days: int
    cadence: ReminderCadence
    slot: int
    due_at: datetime


@dataclass(frozen=True)
class DigestGroup:
    """All currently due cases sharing one urgency/cadence."""

    cadence: ReminderCadence
    items: tuple[DigestItem, ...]


@dataclass(frozen=True)
class CaseDigest:
    """One deterministic projection containing every currently due case."""

    generated_at: datetime
    groups: tuple[DigestGroup, ...]
    due_count: int


_CLOSED_STATUSES = frozenset({"resolved", "ignored", "wont_fix"})
_CADENCE_ORDER = (
    ReminderCadence.WEEKLY,
    ReminderCadence.MONTHLY,
    ReminderCadence.DORMANT,
)


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ReminderPolicyError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReminderPolicyError(f"{field} must include a timezone")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: str, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ReminderPolicyError(f"{field} must be a datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReminderPolicyError(f"{field} must be a valid datetime") from exc
    return _utc(parsed, field=field)


def _validate_policy(policy: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        validate_contract(ContractName.POLICY_CONFIG, policy)
    except ContractValidationError as exc:
        raise ReminderPolicyError(f"reminder policy is invalid: {exc}") from exc
    reminders = policy["reminders"]
    if not (
        reminders["weekly_until_days"]
        < reminders["monthly_until_days"]
        <= reminders["dormant_after_days"]
    ):
        raise ReminderPolicyError(
            "reminder windows must progress from weekly to monthly to dormant"
        )
    if reminders["weekly_interval_days"] > reminders["weekly_until_days"]:
        raise ReminderPolicyError("weekly reminder interval exceeds its window")
    if reminders["monthly_interval_days"] > reminders["monthly_until_days"]:
        raise ReminderPolicyError("monthly reminder interval exceeds its window")
    return reminders


def _validate_state(state: Mapping[str, Any]) -> None:
    try:
        validate_case_state(state)
    except (CaseDomainError, ContractValidationError, SecretBoundaryError) as exc:
        raise ReminderPolicyError(f"case state is invalid: {exc}") from exc


def _latest_interval_slot(
    meaningful_at: datetime,
    age: timedelta,
    *,
    interval_days: int,
) -> tuple[int, datetime] | None:
    interval = timedelta(days=interval_days)
    slot = age // interval
    if slot < 1:
        return None
    return int(slot), meaningful_at + slot * interval


def _derive_active_status(
    current_status: str,
    age: timedelta,
    *,
    weekly_until_days: int,
    dormant_after_days: int,
) -> str:
    if current_status == "dormant":
        return "dormant"
    if age >= timedelta(days=dormant_after_days):
        return "dormant"
    if age > timedelta(days=weekly_until_days):
        return "stalled"
    return "open"


def evaluate_case_reminder(
    state: Mapping[str, Any],
    policy: Mapping[str, Any],
    now: datetime,
) -> ReminderAssessment:
    """Age one case and return its stable due slot without any effect.

    Immediate first-observation messages are deliberately outside P3.2. A
    non-null cadence describes digest data only; it does not mean a reminder
    has been persisted, delivered, or acknowledged.
    """
    _validate_state(state)
    reminders = _validate_policy(policy)
    resolved_now = _utc(now, field="now")
    first_observed_at = _parse_timestamp(
        state["first_observed_at"], field="first_observed_at"
    )
    last_checked_at = _parse_timestamp(
        state["last_checked_at"], field="last_checked_at"
    )
    meaningful_at = _parse_timestamp(
        state["last_meaningful_change_at"], field="last_meaningful_change_at"
    )
    if resolved_now < max(first_observed_at, last_checked_at, meaningful_at):
        raise ReminderPolicyError("now precedes retained case history")

    age = resolved_now - meaningful_at
    age_days = int(age // timedelta(days=1))
    updated = deepcopy(dict(state))
    original_status = updated["status"]
    if original_status in _CLOSED_STATUSES:
        return ReminderAssessment(
            state=updated,
            age_days=age_days,
            status_changed=False,
            cadence=None,
            slot=None,
            due_at=None,
        )

    if original_status == "snoozed":
        snoozed_until = _parse_timestamp(
            updated["snoozed_until"], field="snoozed_until"
        )
        if resolved_now < snoozed_until:
            return ReminderAssessment(
                state=updated,
                age_days=age_days,
                status_changed=False,
                cadence=None,
                slot=None,
                due_at=None,
            )
        updated["snoozed_until"] = None

    updated["status"] = _derive_active_status(
        original_status,
        age,
        weekly_until_days=reminders["weekly_until_days"],
        dormant_after_days=reminders["dormant_after_days"],
    )
    validate_case_state(updated)
    status_changed = updated["status"] != original_status

    cadence: ReminderCadence | None = None
    slot_due: tuple[int, datetime] | None = None
    if updated["status"] == "dormant":
        dormant_at = timedelta(days=reminders["dormant_after_days"])
        if age >= dormant_at:
            dormant_elapsed = age - dormant_at
            dormant_slot = int(
                dormant_elapsed // timedelta(days=reminders["dormant_interval_days"])
            )
            slot_due = (
                dormant_slot + 1,
                meaningful_at
                + dormant_at
                + dormant_slot
                * timedelta(days=reminders["dormant_interval_days"]),
            )
            cadence = ReminderCadence.DORMANT
    elif age <= timedelta(days=reminders["weekly_until_days"]):
        slot_due = _latest_interval_slot(
            meaningful_at,
            age,
            interval_days=reminders["weekly_interval_days"],
        )
        if slot_due is not None:
            cadence = ReminderCadence.WEEKLY
    elif age < timedelta(days=reminders["dormant_after_days"]):
        if age <= timedelta(days=reminders["monthly_until_days"]):
            slot_due = _latest_interval_slot(
                meaningful_at,
                age,
                interval_days=reminders["monthly_interval_days"],
            )
            if slot_due is not None:
                cadence = ReminderCadence.MONTHLY

    return ReminderAssessment(
        state=updated,
        age_days=age_days,
        status_changed=status_changed,
        cadence=cadence,
        slot=slot_due[0] if slot_due is not None else None,
        due_at=slot_due[1] if slot_due is not None else None,
    )


def build_case_digest(
    states: Iterable[Mapping[str, Any]],
    policy: Mapping[str, Any],
    now: datetime,
) -> CaseDigest:
    """Return all due cases in stable weekly/monthly/dormant groups."""
    _validate_policy(policy)  # Validate even when the digest input is empty.
    resolved_now = _utc(now, field="now")
    grouped: dict[ReminderCadence, list[DigestItem]] = {
        cadence: [] for cadence in _CADENCE_ORDER
    }
    case_ids: set[str] = set()
    for state in states:
        assessment = evaluate_case_reminder(state, policy, resolved_now)
        case_id = assessment.state["case_id"]
        if case_id in case_ids:
            raise ReminderPolicyError(f"duplicate case in digest input: {case_id}")
        case_ids.add(case_id)
        if assessment.cadence is None:
            continue
        if assessment.slot is None or assessment.due_at is None:
            raise ReminderPolicyError("due reminder is missing stable slot data")
        item = DigestItem(
            case_id=case_id,
            venue_id=assessment.state["venue_id"],
            year=assessment.state["year"],
            blocker=assessment.state["blocker"],
            status=assessment.state["status"],
            summary=assessment.state["summary"],
            evidence_ids=tuple(assessment.state["evidence_ids"]),
            last_meaningful_change_at=_parse_timestamp(
                assessment.state["last_meaningful_change_at"],
                field="last_meaningful_change_at",
            ),
            age_days=assessment.age_days,
            cadence=assessment.cadence,
            slot=assessment.slot,
            due_at=assessment.due_at,
        )
        grouped[assessment.cadence].append(item)

    groups = tuple(
        DigestGroup(
            cadence=cadence,
            items=tuple(
                sorted(
                    grouped[cadence],
                    key=lambda item: (item.due_at, item.case_id),
                )
            ),
        )
        for cadence in _CADENCE_ORDER
        if grouped[cadence]
    )
    return CaseDigest(
        generated_at=resolved_now,
        groups=groups,
        due_count=sum(len(group.items) for group in groups),
    )
