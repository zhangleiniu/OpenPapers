"""Evidence-driven, side-effect-free scheduling for conference-year checks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from automation.contracts import ContractName, validate_contract
from automation.domain import LifecycleKind, LifecycleState, assert_secret_free


class NextCheckReason(str, Enum):
    UNKNOWN_SCHEDULE_FALLBACK = "unknown_schedule_fallback"
    BEFORE_VERIFIED_MILESTONE = "before_verified_milestone"
    EXPECTED_RELEASE = "expected_release"
    POST_CONFERENCE_RELEASE_BACKOFF = "post_conference_release_backoff"
    MAXIMUM_SILENCE_GUARD = "maximum_silence_guard"


@dataclass(frozen=True)
class NextCheck:
    at: datetime
    reason: NextCheckReason
    milestone: str | None


@dataclass(frozen=True)
class _Candidate:
    at: datetime
    reason: NextCheckReason
    milestone: str | None
    priority: int


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"datetime must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _milestone_at(state: Mapping[str, Any], name: str) -> datetime | None:
    milestone = state["milestones"].get(name)
    return _parse_datetime(milestone["at"]) if milestone is not None else None


def _at_or_after_minimum(value: datetime, minimum: datetime) -> datetime:
    return value if value >= minimum else minimum


def compute_next_check(
    state: Mapping[str, Any],
    policy: Mapping[str, Any],
    now: datetime,
    *,
    lifecycle_kind: LifecycleKind | str = LifecycleKind.ANNUAL,
) -> NextCheck | None:
    """Compute the next useful check from verified milestones and policy.

    This function never performs discovery or trusts an unvalidated candidate.
    Its inputs must already satisfy the Phase 0 state and policy contracts.
    """
    validate_contract(ContractName.CONFERENCE_STATE, state)
    validate_contract(ContractName.POLICY_CONFIG, policy)
    assert_secret_free(state)
    resolved_now = _utc(now)
    kind = LifecycleKind(lifecycle_kind)
    if state["lifecycle_state"] == LifecycleState.PUBLISHED.value:
        return None

    scheduling = policy["scheduling"]
    minimum = resolved_now + timedelta(
        hours=scheduling["minimum_recheck_interval_hours"])
    maximum = resolved_now + timedelta(days=scheduling["max_silence_days"])
    candidates: list[_Candidate] = []

    releases = {
        "paper_list_expected": "paper_list_released",
        "proceedings_expected": "proceedings_released",
    }
    for expected_name, observed_name in releases.items():
        if state["milestones"][observed_name] is not None:
            continue
        expected_at = _milestone_at(state, expected_name)
        if expected_at is not None:
            candidates.append(_Candidate(
                _at_or_after_minimum(expected_at, minimum),
                NextCheckReason.EXPECTED_RELEASE,
                expected_name,
                0,
            ))

    lead = timedelta(days=scheduling["verified_milestone_lead_days"])
    for name in ("acceptance_notification", "conference_start"):
        milestone_at = _milestone_at(state, name)
        if milestone_at is not None and milestone_at > resolved_now:
            candidates.append(_Candidate(
                _at_or_after_minimum(milestone_at - lead, minimum),
                NextCheckReason.BEFORE_VERIFIED_MILESTONE,
                name,
                1,
            ))

    proceedings_released = state["milestones"]["proceedings_released"]
    conference_end = (
        None if kind is LifecycleKind.CONTINUOUS
        else _milestone_at(state, "conference_end")
    )
    if conference_end is not None and proceedings_released is None:
        backoff_days = scheduling["post_conference_release_backoff_days"]
        post_event_checks = [
            conference_end + timedelta(days=days) for days in backoff_days
        ]
        next_post_event = next(
            (candidate for candidate in post_event_checks if candidate >= minimum),
            resolved_now + timedelta(
                days=scheduling["unknown_schedule_interval_days"]),
        )
        candidates.append(_Candidate(
            next_post_event,
            NextCheckReason.POST_CONFERENCE_RELEASE_BACKOFF,
            "conference_end",
            2,
        ))

    if candidates:
        candidate = min(
            candidates,
            key=lambda item: (item.at, item.priority, item.milestone or ""),
        )
    else:
        candidate = _Candidate(
            resolved_now + timedelta(
                days=scheduling["unknown_schedule_interval_days"]),
            NextCheckReason.UNKNOWN_SCHEDULE_FALLBACK,
            None,
            3,
        )

    if candidate.at > maximum:
        return NextCheck(
            maximum,
            NextCheckReason.MAXIMUM_SILENCE_GUARD,
            candidate.milestone,
        )
    return NextCheck(candidate.at, candidate.reason, candidate.milestone)


def schedule_next_check(
    state: Mapping[str, Any],
    policy: Mapping[str, Any],
    now: datetime,
    *,
    lifecycle_kind: LifecycleKind | str = LifecycleKind.ANNUAL,
) -> dict[str, Any]:
    """Return a validated state copy with a recomputed ``next_check_at``."""
    resolved_now = _utc(now)
    next_check = compute_next_check(
        state,
        policy,
        resolved_now,
        lifecycle_kind=lifecycle_kind,
    )
    updated = deepcopy(dict(state))
    updated["next_check_at"] = (
        _format_datetime(next_check.at) if next_check is not None else None)
    updated["next_check_reason"] = (
        next_check.reason.value if next_check is not None else None)
    updated["updated_at"] = _format_datetime(resolved_now)
    validate_contract(ContractName.CONFERENCE_STATE, updated)
    return updated
