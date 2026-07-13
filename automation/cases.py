"""Pure P3.1 unresolved-case state and human-control semantics."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from automation.contracts import ContractName, validate_contract
from automation.domain import BlockerCode, assert_secret_free


class CaseDomainError(ValueError):
    """Raised when a case observation, state, or control fails closed."""


class CaseControl(str, Enum):
    """Human controls implemented by P3.1."""

    RESOLVE = "resolve"
    SNOOZE = "snooze"
    IGNORE = "ignore"
    REACTIVATE = "reactivate"


@dataclass(frozen=True)
class CaseObservation:
    """One idempotently addressable blocker observation."""

    event_id: str
    venue_id: str
    year: int
    blocker: BlockerCode | str
    summary: str
    evidence_ids: tuple[str, ...]
    observed_at: str


@dataclass(frozen=True)
class CaseControlRequest:
    """One idempotently addressable human control request."""

    event_id: str
    action: CaseControl | str
    at: str
    reason: str | None = None
    snoozed_until: str | None = None


@dataclass(frozen=True)
class CaseMutation:
    """The next validated case state and its observable delta."""

    state: dict[str, Any]
    changed: bool
    meaningful_change: bool
    reactivated: bool


_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{2,127}$")
_VENUE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_UNRESOLVED_STATUSES = frozenset({"open", "stalled", "dormant", "snoozed"})
_CLOSED_STATUSES = frozenset({"resolved", "ignored", "wont_fix"})


def _parse_timestamp(value: str, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise CaseDomainError(f"{field} must be a datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaseDomainError(f"{field} must be a valid datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CaseDomainError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: str, *, field: str) -> str:
    return _parse_timestamp(value, field=field).isoformat().replace("+00:00", "Z")


def _validate_id(value: str, *, field: str) -> None:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise CaseDomainError(f"{field} is not a stable artifact ID")


def _validate_venue_year(venue_id: str, year: int) -> None:
    if not isinstance(venue_id, str) or _VENUE_PATTERN.fullmatch(venue_id) is None:
        raise CaseDomainError("venue_id is invalid")
    if (
        not isinstance(year, int)
        or isinstance(year, bool)
        or not 1900 <= year <= 2200
    ):
        raise CaseDomainError("year must be an integer between 1900 and 2200")


def _validate_summary(value: str, *, field: str = "summary") -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 5000:
        raise CaseDomainError(f"{field} must contain 1 to 5000 non-blank characters")


def _blocker(value: BlockerCode | str) -> BlockerCode:
    try:
        return BlockerCode(value)
    except (TypeError, ValueError) as exc:
        raise CaseDomainError(f"unknown blocker: {value!r}") from exc


def _control(value: CaseControl | str) -> CaseControl:
    try:
        return CaseControl(value)
    except (TypeError, ValueError) as exc:
        raise CaseDomainError(f"unknown case control: {value!r}") from exc


def derive_case_id(venue_id: str, year: int, blocker: BlockerCode | str) -> str:
    """Derive the one stable case identity for a venue/year/blocker key."""
    _validate_venue_year(venue_id, year)
    resolved = _blocker(blocker)
    blocker_slug = resolved.value.replace("_", "-")
    case_id = f"case:{venue_id}:{year}:{blocker_slug}"
    _validate_id(case_id, field="case_id")
    return case_id


def _observation_event(observation: CaseObservation) -> dict[str, Any]:
    _validate_id(observation.event_id, field="event_id")
    case_id = derive_case_id(
        observation.venue_id, observation.year, observation.blocker
    )
    _validate_summary(observation.summary)
    evidence = tuple(observation.evidence_ids)
    if not evidence or len(set(evidence)) != len(evidence):
        raise CaseDomainError(
            "case observations require unique, non-empty evidence IDs"
        )
    for evidence_id in evidence:
        _validate_id(evidence_id, field="evidence_id")
    payload = {
        "event_id": observation.event_id,
        "event_kind": "observation",
        "case_id": case_id,
        "venue_id": observation.venue_id,
        "year": observation.year,
        "blocker": _blocker(observation.blocker).value,
        "summary": observation.summary,
        "evidence_ids": sorted(evidence),
        "at": _timestamp(observation.observed_at, field="observed_at"),
    }
    assert_secret_free(payload)
    return payload


def _control_event(case_id: str, request: CaseControlRequest) -> dict[str, Any]:
    _validate_id(case_id, field="case_id")
    _validate_id(request.event_id, field="event_id")
    action = _control(request.action)
    at = _timestamp(request.at, field="control at")
    reason = request.reason
    if reason is not None:
        _validate_summary(reason, field="control reason")
    if action in {CaseControl.RESOLVE, CaseControl.IGNORE} and reason is None:
        raise CaseDomainError(f"{action.value} requires a non-blank reason")
    if action is CaseControl.SNOOZE:
        if request.snoozed_until is None:
            raise CaseDomainError("snooze requires a future snoozed_until")
        snoozed_until = _timestamp(
            request.snoozed_until, field="snoozed_until"
        )
        if _parse_timestamp(snoozed_until, field="snoozed_until") <= _parse_timestamp(
            at, field="control at"
        ):
            raise CaseDomainError("snoozed_until must be in the future")
    else:
        if request.snoozed_until is not None:
            raise CaseDomainError(
                f"{action.value} cannot carry a snoozed_until value"
            )
        snoozed_until = None
    payload = {
        "event_id": request.event_id,
        "event_kind": "control",
        "case_id": case_id,
        "action": action.value,
        "at": at,
        "reason": reason,
        "snoozed_until": snoozed_until,
    }
    assert_secret_free(payload)
    return payload


def case_event_payload(
    event: CaseObservation | CaseControlRequest,
    *,
    case_id: str | None = None,
) -> dict[str, Any]:
    """Return the canonical semantic payload for one case event."""
    if isinstance(event, CaseObservation):
        if case_id is not None:
            raise CaseDomainError("an observation derives its own case_id")
        return _observation_event(event)
    if isinstance(event, CaseControlRequest):
        if case_id is None:
            raise CaseDomainError("a control event requires case_id")
        return _control_event(case_id, event)
    raise CaseDomainError("unknown case event type")


def validate_case_event_payload(payload: Mapping[str, Any]) -> None:
    """Revalidate a stored P3.1 event without applying it."""
    if not isinstance(payload, Mapping):
        raise CaseDomainError("case event must be an object")
    event_kind = payload.get("event_kind")
    if event_kind == "observation":
        required = {
            "event_id",
            "event_kind",
            "case_id",
            "venue_id",
            "year",
            "blocker",
            "summary",
            "evidence_ids",
            "at",
        }
        if set(payload) != required:
            raise CaseDomainError("stored observation event fields are invalid")
        rebuilt = case_event_payload(
            CaseObservation(
                event_id=payload["event_id"],
                venue_id=payload["venue_id"],
                year=payload["year"],
                blocker=payload["blocker"],
                summary=payload["summary"],
                evidence_ids=tuple(payload["evidence_ids"]),
                observed_at=payload["at"],
            )
        )
    elif event_kind == "control":
        required = {
            "event_id",
            "event_kind",
            "case_id",
            "action",
            "at",
            "reason",
            "snoozed_until",
        }
        if set(payload) != required:
            raise CaseDomainError("stored control event fields are invalid")
        rebuilt = case_event_payload(
            CaseControlRequest(
                event_id=payload["event_id"],
                action=payload["action"],
                at=payload["at"],
                reason=payload["reason"],
                snoozed_until=payload["snoozed_until"],
            ),
            case_id=payload["case_id"],
        )
    else:
        raise CaseDomainError("stored case event kind is invalid")
    if dict(payload) != rebuilt:
        raise CaseDomainError("stored case event is not canonical")


def validate_case_state(state: Mapping[str, Any]) -> None:
    """Apply P3.1 semantic checks in addition to the v1 JSON contract."""
    assert_secret_free(state)
    validate_contract(ContractName.CASE_STATE, state)
    expected_id = derive_case_id(state["venue_id"], state["year"], state["blocker"])
    if state["case_id"] != expected_id:
        raise CaseDomainError("case_id does not match venue/year/blocker")
    if not state["evidence_ids"]:
        raise CaseDomainError("case state requires at least one evidence ID")
    _validate_summary(state["summary"])
    first = _parse_timestamp(state["first_observed_at"], field="first_observed_at")
    checked = _parse_timestamp(state["last_checked_at"], field="last_checked_at")
    meaningful = _parse_timestamp(
        state["last_meaningful_change_at"], field="last_meaningful_change_at"
    )
    if checked < first or meaningful < first:
        raise CaseDomainError("case timestamps regress before first_observed_at")
    status = state["status"]
    if status == "snoozed":
        if state["snoozed_until"] is None:
            raise CaseDomainError("snoozed case requires snoozed_until")
        _parse_timestamp(state["snoozed_until"], field="snoozed_until")
    elif state["snoozed_until"] is not None:
        raise CaseDomainError("only a snoozed case may retain snoozed_until")
    if status in _CLOSED_STATUSES:
        if state["resolution"] is None:
            raise CaseDomainError("closed case requires a resolution")
        _validate_summary(state["resolution"], field="resolution")
    elif state["resolution"] is not None:
        raise CaseDomainError("unresolved case cannot retain a resolution")


def _reject_time_regression(state: Mapping[str, Any], at: str) -> None:
    event_at = _parse_timestamp(at, field="event at")
    latest = max(
        _parse_timestamp(state["last_checked_at"], field="last_checked_at"),
        _parse_timestamp(
            state["last_meaningful_change_at"],
            field="last_meaningful_change_at",
        ),
    )
    if event_at < latest:
        raise CaseDomainError("case event time cannot regress")


def observe_case(
    current: Mapping[str, Any] | None,
    observation: CaseObservation,
) -> CaseMutation:
    """Create or update one stable case without persistence or effects."""
    event = case_event_payload(observation)
    at = event["at"]
    if current is None:
        state = {
            "schema_version": 1,
            "case_id": event["case_id"],
            "venue_id": event["venue_id"],
            "year": event["year"],
            "blocker": event["blocker"],
            "status": "open",
            "summary": event["summary"],
            "evidence_ids": list(event["evidence_ids"]),
            "first_observed_at": at,
            "last_checked_at": at,
            "last_meaningful_change_at": at,
            "snoozed_until": None,
            "resolution": None,
        }
        validate_case_state(state)
        return CaseMutation(
            state=state,
            changed=True,
            meaningful_change=True,
            reactivated=False,
        )

    validate_case_state(current)
    if current["case_id"] != event["case_id"]:
        raise CaseDomainError("observation does not match the current case key")
    _reject_time_regression(current, at)
    updated = deepcopy(dict(current))
    previous_evidence = set(updated["evidence_ids"])
    observed_evidence = set(event["evidence_ids"])
    has_new_evidence = not observed_evidence.issubset(previous_evidence)
    summary_changed = updated["summary"] != event["summary"]
    meaningful_change = has_new_evidence or summary_changed
    reactivated = updated["status"] == "dormant" and has_new_evidence

    updated["summary"] = event["summary"]
    updated["evidence_ids"] = sorted(previous_evidence | observed_evidence)
    updated["last_checked_at"] = at
    if meaningful_change:
        updated["last_meaningful_change_at"] = at
    if reactivated:
        updated["status"] = "open"
        updated["snoozed_until"] = None
        updated["resolution"] = None
    validate_case_state(updated)
    return CaseMutation(
        state=updated,
        changed=updated != dict(current),
        meaningful_change=meaningful_change,
        reactivated=reactivated,
    )


def control_case(
    current: Mapping[str, Any],
    request: CaseControlRequest,
) -> CaseMutation:
    """Apply one P3.1 human control to a validated case state."""
    validate_case_state(current)
    event = case_event_payload(request, case_id=current["case_id"])
    _reject_time_regression(current, event["at"])
    action = CaseControl(event["action"])
    status = current["status"]
    if action in {CaseControl.RESOLVE, CaseControl.IGNORE}:
        if status not in _UNRESOLVED_STATUSES:
            raise CaseDomainError(f"{action.value} requires an active case")
    elif action is CaseControl.SNOOZE:
        if status not in _UNRESOLVED_STATUSES:
            raise CaseDomainError("snooze requires an active case")
    elif action is CaseControl.REACTIVATE and status == "open":
        raise CaseDomainError("reactivate requires a non-open case")

    updated = deepcopy(dict(current))
    updated["last_meaningful_change_at"] = event["at"]
    if action is CaseControl.RESOLVE:
        updated["status"] = "resolved"
        updated["resolution"] = event["reason"]
        updated["snoozed_until"] = None
    elif action is CaseControl.IGNORE:
        updated["status"] = "ignored"
        updated["resolution"] = event["reason"]
        updated["snoozed_until"] = None
    elif action is CaseControl.SNOOZE:
        updated["status"] = "snoozed"
        updated["resolution"] = None
        updated["snoozed_until"] = event["snoozed_until"]
    else:
        updated["status"] = "open"
        updated["resolution"] = None
        updated["snoozed_until"] = None
    validate_case_state(updated)
    return CaseMutation(
        state=updated,
        changed=updated != dict(current),
        meaningful_change=True,
        reactivated=action is CaseControl.REACTIVATE,
    )
