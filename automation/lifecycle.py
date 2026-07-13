"""Pure P2.5 verification reduction, scheduling, and typed action routing."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, TypeAlias

from automation.contracts import (
    ContractName,
    artifact_fingerprint,
    validate_contract,
)
from automation.domain import (
    ActionType,
    BlockerCode,
    LifecycleKind,
    LifecycleState,
    TransitionActor,
    TransitionRequest,
    apply_transition,
    assert_secret_free,
)
from automation.scheduling import schedule_next_check
from automation.verification import (
    SourceTrust,
    classify_source,
    validate_verification_result,
)


class LifecycleReductionError(ValueError):
    """Raised when a verified bundle cannot be reduced safely."""


@dataclass(frozen=True)
class RecheckPayload:
    at: str
    reason: str


@dataclass(frozen=True)
class TransitionNoticePayload:
    transition_id: str
    previous_state: str
    new_state: str


@dataclass(frozen=True)
class CasePayload:
    blocker_codes: tuple[str, ...]
    verification_status: str


@dataclass(frozen=True)
class HumanReviewPayload:
    reasons: tuple[str, ...]
    verification_status: str


@dataclass(frozen=True)
class QueueExistingScraperPayload:
    readiness: str
    scraper_module: str
    scraper_class: str


ActionPayload: TypeAlias = (
    RecheckPayload
    | TransitionNoticePayload
    | CasePayload
    | HumanReviewPayload
    | QueueExistingScraperPayload
)


_ACTION_PAYLOAD_TYPES: dict[ActionType, type[ActionPayload]] = {
    ActionType.RECHECK_AT: RecheckPayload,
    ActionType.NOTIFY_TRANSITION: TransitionNoticePayload,
    ActionType.CREATE_OR_UPDATE_CASE: CasePayload,
    ActionType.REQUEST_HUMAN_REVIEW: HumanReviewPayload,
    ActionType.QUEUE_EXISTING_SCRAPER: QueueExistingScraperPayload,
}


@dataclass(frozen=True)
class ActionIntent:
    """One closed, immutable action description that performs no effect."""

    action_id: str
    action_type: ActionType
    venue_id: str
    year: int
    evidence_ids: tuple[str, ...]
    payload: ActionPayload

    def __post_init__(self) -> None:
        expected = _ACTION_PAYLOAD_TYPES.get(self.action_type)
        if expected is None or not isinstance(self.payload, expected):
            raise LifecycleReductionError(
                f"payload does not match action type {self.action_type.value}"
            )
        if not self.evidence_ids or len(set(self.evidence_ids)) != len(
            self.evidence_ids
        ):
            raise LifecycleReductionError(
                "action intents require unique, non-empty evidence IDs"
            )
        assert_secret_free(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible defensive representation."""
        payload = asdict(self.payload)
        for key, value in tuple(payload.items()):
            if isinstance(value, tuple):
                payload[key] = list(value)
        return {
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "venue_id": self.venue_id,
            "year": self.year,
            "evidence_ids": list(self.evidence_ids),
            "payload": payload,
        }


@dataclass(frozen=True)
class ReductionOutcome:
    """The next validated state and the inert actions derived from its delta."""

    state: dict[str, Any]
    actions: tuple[ActionIntent, ...]
    consumed: bool
    transition_applied: bool


@dataclass(frozen=True)
class _Authority:
    source_id: str
    snapshot_id: str
    url: str
    source_type: str


_FACET_ORDER: dict[str, tuple[str, ...]] = {
    "conference_status": ("unknown", "scheduled", "ended"),
    "paper_list_status": ("unknown", "unavailable", "partial", "released"),
    "metadata_status": ("unknown", "unavailable", "partial", "ready"),
    "pdf_status": ("unknown", "unavailable", "partial", "ready"),
    "proceedings_status": (
        "unknown",
        "unavailable",
        "provisional",
        "archival",
    ),
}

_LIFECYCLE_ORDER = tuple(LifecycleState)
_MANAGED_BLOCKERS = frozenset(
    {
        BlockerCode.NO_PUBLIC_LIST.value,
        BlockerCode.NO_PDF.value,
        BlockerCode.UNKNOWN_DOWNLOAD_SOURCE.value,
        BlockerCode.UNSUPPORTED_SCRAPER.value,
        BlockerCode.HUMAN_REVIEW_REQUIRED.value,
        BlockerCode.CRAWL_POLICY_DENIED.value,
    }
)
_EXECUTION_BLOCKERS = frozenset(
    {
        BlockerCode.UNSUPPORTED_SCRAPER.value,
        BlockerCode.HUMAN_REVIEW_REQUIRED.value,
        BlockerCode.CRAWL_POLICY_DENIED.value,
    }
)
_CONTINUOUS_CONFERENCE_MILESTONES = frozenset(
    {"conference_start", "conference_end", "acceptance_notification"}
)


def _parse_datetime(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LifecycleReductionError(f"{field} is not a valid datetime") from exc
    else:
        raise LifecycleReductionError(f"{field} must be a datetime or string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LifecycleReductionError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime | str, *, field: str) -> str:
    return _parse_datetime(value, field=field).isoformat().replace("+00:00", "Z")


def _catalog_venue(
    catalog: Mapping[str, Any], venue_id: str
) -> Mapping[str, Any]:
    validate_contract(ContractName.VENUE_CATALOG, catalog)
    for venue in catalog["venues"]:
        if venue["venue_id"] == venue_id:
            return venue
    raise LifecycleReductionError(f"venue {venue_id!r} is absent from the catalog")


def initial_conference_state(
    catalog: Mapping[str, Any],
    venue_id: str,
    year: int,
    *,
    at: datetime | str,
) -> dict[str, Any]:
    """Build a strict empty conference-year state for one catalog venue."""
    _catalog_venue(catalog, venue_id)
    state = {
        "schema_version": 1,
        "venue_id": venue_id,
        "year": year,
        "lifecycle_state": LifecycleState.UNKNOWN.value,
        "facets": {
            "conference_status": "unknown",
            "paper_list_status": "unknown",
            "metadata_status": "unknown",
            "pdf_status": "unknown",
            "proceedings_status": "unknown",
        },
        "milestones": {
            "conference_start": None,
            "conference_end": None,
            "acceptance_notification": None,
            "paper_list_expected": None,
            "proceedings_expected": None,
            "paper_list_released": None,
            "proceedings_released": None,
        },
        "next_check_at": None,
        "next_check_reason": None,
        "evidence_ids": [],
        "blockers": [],
        "transition_history": [],
        "updated_at": _timestamp(at, field="initial state time"),
    }
    validate_contract(ContractName.CONFERENCE_STATE, state)
    return state


def _authorities(
    result: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> dict[str, _Authority]:
    authorities: dict[str, _Authority] = {}
    for observation in result["source_observations"]:
        if (
            observation["fetch_status"] != "fetched"
            or observation["policy_decision"] != "allowed"
            or observation["http_status"] != 200
            or observation["snapshot_id"] is None
        ):
            continue
        classification = classify_source(
            catalog, result["venue_id"], observation["url"]
        )
        if (
            classification.trust is SourceTrust.UNTRUSTED
            or observation["source_trust"] != classification.trust.value
        ):
            continue
        authority = _Authority(
            source_id=observation["source_id"],
            snapshot_id=observation["snapshot_id"],
            url=observation["url"],
            source_type=classification.trust.value,
        )
        authorities[authority.source_id] = authority
        authorities[authority.snapshot_id] = authority
    return authorities


def _supporting_authority(
    evidence_ids: Sequence[str], authorities: Mapping[str, _Authority]
) -> _Authority | None:
    supported = {authorities[item] for item in evidence_ids if item in authorities}
    if not supported:
        return None
    return min(
        supported,
        key=lambda item: (
            0 if item.source_type == SourceTrust.OFFICIAL.value else 1,
            item.url,
            item.source_id,
        ),
    )


def _append_unique(target: list[str], values: Sequence[str]) -> None:
    target.extend(value for value in values if value not in target)


def _promote_facet(
    state: dict[str, Any], name: str, value: str
) -> bool:
    order = _FACET_ORDER[name]
    current = state["facets"][name]
    if order.index(value) <= order.index(current):
        return False
    state["facets"][name] = value
    return True


def _milestone_at(date: str) -> str:
    return f"{date}T00:00:00Z"


def _promote_milestone(
    state: dict[str, Any],
    name: str,
    milestone: Mapping[str, Any],
    *,
    conflict_on_difference: bool = True,
) -> tuple[bool, bool]:
    """Return ``(promoted, conflict)`` without replacing retained evidence."""
    existing = state["milestones"][name]
    if existing is None:
        state["milestones"][name] = deepcopy(dict(milestone))
        return True, False
    if existing["at"] == milestone["at"]:
        return False, False
    return False, conflict_on_difference


def _release_milestone(
    *,
    verified_at: str,
    authority: _Authority,
    evidence_ids: Sequence[str],
) -> dict[str, Any]:
    return {
        "at": verified_at,
        "status": "observed",
        "source_type": authority.source_type,
        "source_url": authority.url,
        "evidence_ids": sorted(set(evidence_ids)),
        "observed_at": verified_at,
    }


def _verified_milestone(
    item: Mapping[str, Any], *, verified_at: str
) -> dict[str, Any]:
    return {
        "at": _milestone_at(item["date"]),
        "status": "verified",
        "source_type": item["source_type"],
        "source_url": item["source_url"],
        "evidence_ids": sorted(set(item["evidence_ids"])),
        "observed_at": verified_at,
    }


def _highest_lifecycle_candidate(
    state: Mapping[str, Any],
    support: Mapping[str, Sequence[str]],
    verified_at: datetime,
    lifecycle_kind: LifecycleKind,
) -> tuple[LifecycleState, tuple[str, ...], str] | None:
    candidates: list[tuple[LifecycleState, tuple[str, ...], str]] = []
    facets = state["facets"]
    if lifecycle_kind is LifecycleKind.ANNUAL:
        if facets["conference_status"] == "ended" and support.get(
            "conference_status"
        ):
            candidates.append(
                (
                    LifecycleState.CONFERENCE_ENDED,
                    tuple(support["conference_status"]),
                    "conference_status=ended",
                )
            )
        elif facets["conference_status"] == "scheduled" and support.get(
            "conference_status"
        ):
            candidates.append(
                (
                    LifecycleState.SCHEDULED,
                    tuple(support["conference_status"]),
                    "conference_status=scheduled",
                )
            )
        conference_end = state["milestones"]["conference_end"]
        if conference_end is not None and support.get("conference_end"):
            end_at = _parse_datetime(
                conference_end["at"], field="conference_end milestone"
            )
            if end_at <= verified_at:
                candidates.append(
                    (
                        LifecycleState.CONFERENCE_ENDED,
                        tuple(support["conference_end"]),
                        "verified conference_end milestone elapsed",
                    )
                )
            else:
                candidates.append(
                    (
                        LifecycleState.SCHEDULED,
                        tuple(support["conference_end"]),
                        "future conference_end milestone verified",
                    )
                )
        if state["milestones"]["conference_start"] is not None and support.get(
            "conference_start"
        ):
            candidates.append(
                (
                    LifecycleState.SCHEDULED,
                    tuple(support["conference_start"]),
                    "conference_start milestone verified",
                )
            )
    facet_targets = (
        ("paper_list_status", "released", LifecycleState.PAPER_LIST_RELEASED),
        ("metadata_status", "ready", LifecycleState.METADATA_READY),
        ("pdf_status", "partial", LifecycleState.PDF_PARTIAL),
        ("pdf_status", "ready", LifecycleState.PDF_READY),
    )
    for name, value, target in facet_targets:
        if state["facets"][name] == value and support.get(name):
            candidates.append(
                (target, tuple(support[name]), f"{name}={value}")
            )
    if not candidates:
        return None
    return max(candidates, key=lambda item: _LIFECYCLE_ORDER.index(item[0]))


def _finding_reasons(result: Mapping[str, Any]) -> set[str]:
    return {
        finding["reason_code"]
        for finding in result["findings"]
        if finding["status"] != "verified"
    }


def _managed_blockers(
    state: Mapping[str, Any],
    result: Mapping[str, Any],
    venue: Mapping[str, Any],
    review_reasons: Sequence[str],
) -> set[str]:
    blockers = set(state["blockers"]) & {
        BlockerCode.HUMAN_REVIEW_REQUIRED.value,
        BlockerCode.CRAWL_POLICY_DENIED.value,
    }
    reasons = _finding_reasons(result)
    observation_decisions = {
        item["policy_decision"] for item in result["source_observations"]
    }
    if "denied" in observation_decisions:
        blockers.add(BlockerCode.CRAWL_POLICY_DENIED.value)
    if observation_decisions & {
        "review_required",
        "permission_missing",
        "request_budget_exhausted",
    }:
        blockers.add(BlockerCode.HUMAN_REVIEW_REQUIRED.value)
    if result["overall_status"] in {
        "partially_verified",
        "review_required",
        "conflicting",
        "error",
    } or review_reasons:
        blockers.add(BlockerCode.HUMAN_REVIEW_REQUIRED.value)
    if "pdf_invalid_url" in reasons:
        blockers.add(BlockerCode.UNKNOWN_DOWNLOAD_SOURCE.value)
    if reasons & {"identity_mismatch", "year_mismatch", "implausible_paper_count"}:
        blockers.add(BlockerCode.HUMAN_REVIEW_REQUIRED.value)
    if state["facets"]["paper_list_status"] == "released" and state["facets"][
        "pdf_status"
    ] != "ready":
        blockers.add(BlockerCode.NO_PDF.value)
    capabilities = set(venue["scraper"]["capabilities"])
    if state["facets"]["pdf_status"] == "ready" and not {
        "metadata",
        "pdf",
    }.issubset(capabilities):
        blockers.add(BlockerCode.UNSUPPORTED_SCRAPER.value)
    return blockers


def _action(
    action_type: ActionType,
    *,
    venue_id: str,
    year: int,
    evidence_ids: Sequence[str],
    payload: ActionPayload,
) -> ActionIntent:
    evidence = tuple(sorted(set(evidence_ids)))
    identity = {
        "action_type": action_type.value,
        "venue_id": venue_id,
        "year": year,
        "evidence_ids": list(evidence),
        "payload": asdict(payload),
    }
    fingerprint = artifact_fingerprint(identity)
    return ActionIntent(
        action_id=f"action:{fingerprint[:32]}",
        action_type=action_type,
        venue_id=venue_id,
        year=year,
        evidence_ids=evidence,
        payload=payload,
    )


def _route_actions(
    state: Mapping[str, Any],
    result: Mapping[str, Any],
    venue: Mapping[str, Any],
    *,
    transition_applied: bool,
    supported_facets: Mapping[str, Sequence[str]],
    review_reasons: Sequence[str],
) -> tuple[ActionIntent, ...]:
    evidence_ids = (result["verification_id"],)
    actions: list[ActionIntent] = []
    if state["next_check_at"] is not None:
        actions.append(
            _action(
                ActionType.RECHECK_AT,
                venue_id=state["venue_id"],
                year=state["year"],
                evidence_ids=evidence_ids,
                payload=RecheckPayload(
                    at=state["next_check_at"],
                    reason=state["next_check_reason"],
                ),
            )
        )
    if transition_applied:
        transition = state["transition_history"][-1]
        actions.append(
            _action(
                ActionType.NOTIFY_TRANSITION,
                venue_id=state["venue_id"],
                year=state["year"],
                evidence_ids=transition["evidence_ids"],
                payload=TransitionNoticePayload(
                    transition_id=transition["transition_id"],
                    previous_state=transition["previous_state"],
                    new_state=transition["new_state"],
                ),
            )
        )
    if state["blockers"]:
        actions.append(
            _action(
                ActionType.CREATE_OR_UPDATE_CASE,
                venue_id=state["venue_id"],
                year=state["year"],
                evidence_ids=evidence_ids,
                payload=CasePayload(
                    blocker_codes=tuple(state["blockers"]),
                    verification_status=result["overall_status"],
                ),
            )
        )
    if BlockerCode.HUMAN_REVIEW_REQUIRED.value in state["blockers"]:
        reasons = tuple(sorted(set(review_reasons))) or (
            "verification_requires_review",
        )
        actions.append(
            _action(
                ActionType.REQUEST_HUMAN_REVIEW,
                venue_id=state["venue_id"],
                year=state["year"],
                evidence_ids=evidence_ids,
                payload=HumanReviewPayload(
                    reasons=reasons,
                    verification_status=result["overall_status"],
                ),
            )
        )
    can_queue = (
        result["overall_status"] == "verified"
        and state["lifecycle_state"] == LifecycleState.PDF_READY.value
        and state["facets"]["pdf_status"] == "ready"
        and result["verified_facets"]["pdf_status"] is not None
        and result["verified_facets"]["pdf_status"]["value"] == "ready"
        and supported_facets.get("pdf_status")
        and not (_EXECUTION_BLOCKERS & set(state["blockers"]))
    )
    if can_queue:
        actions.append(
            _action(
                ActionType.QUEUE_EXISTING_SCRAPER,
                venue_id=state["venue_id"],
                year=state["year"],
                evidence_ids=(
                    result["verification_id"],
                    *supported_facets["pdf_status"],
                ),
                payload=QueueExistingScraperPayload(
                    readiness="pdf_ready",
                    scraper_module=venue["scraper"]["module"],
                    scraper_class=venue["scraper"]["class_name"],
                ),
            )
        )
    return tuple(actions)


def reduce_verification(
    state: Mapping[str, Any],
    discovery: Mapping[str, Any],
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> ReductionOutcome:
    """Reduce one strict verification bundle without performing its actions."""
    validate_contract(ContractName.CONFERENCE_STATE, state)
    validate_contract(ContractName.POLICY_CONFIG, policy)
    validate_verification_result(result, request, discovery)
    assert_secret_free(state)
    if state["venue_id"] != result["venue_id"] or state["year"] != result["year"]:
        raise LifecycleReductionError(
            "conference state identity does not match verification result"
        )
    venue = _catalog_venue(catalog, result["venue_id"])
    lifecycle_kind = LifecycleKind(venue["lifecycle"]["kind"])
    if result["verification_id"] in state["evidence_ids"]:
        return ReductionOutcome(deepcopy(dict(state)), (), False, False)

    updated = deepcopy(dict(state))
    authorities = _authorities(result, catalog)
    verified_at_text = _timestamp(
        result["verified_at"], field="verification time"
    )
    verified_at = _parse_datetime(verified_at_text, field="verification time")
    supported: dict[str, tuple[str, ...]] = {}
    review_reasons: list[str] = []
    promoted_evidence: list[str] = [result["verification_id"]]

    for name, facet in result["verified_facets"].items():
        if facet is None:
            continue
        if lifecycle_kind is LifecycleKind.CONTINUOUS and name == "conference_status":
            review_reasons.append("continuous_venue_conference_facet")
            continue
        authority = _supporting_authority(facet["evidence_ids"], authorities)
        if authority is None:
            review_reasons.append(f"{name}_lacks_authoritative_evidence")
            continue
        evidence = tuple(sorted(set(facet["evidence_ids"])))
        promoted = _promote_facet(updated, name, facet["value"])
        if promoted or updated["facets"][name] == facet["value"]:
            supported[name] = evidence
        if promoted:
            _append_unique(promoted_evidence, evidence)
        if name == "paper_list_status" and facet["value"] == "released":
            promoted, conflict = _promote_milestone(
                updated,
                "paper_list_released",
                _release_milestone(
                    verified_at=verified_at_text,
                    authority=authority,
                    evidence_ids=evidence,
                ),
                conflict_on_difference=False,
            )
            if promoted:
                _append_unique(promoted_evidence, evidence)
            if conflict:
                review_reasons.append("paper_list_released_milestone_conflict")
        if name == "proceedings_status":
            promoted, conflict = _promote_milestone(
                updated,
                "proceedings_released",
                _release_milestone(
                    verified_at=verified_at_text,
                    authority=authority,
                    evidence_ids=evidence,
                ),
                conflict_on_difference=False,
            )
            if promoted:
                _append_unique(promoted_evidence, evidence)
            if conflict:
                review_reasons.append("proceedings_released_milestone_conflict")

    for item in result["verified_milestones"]:
        name = item["milestone_type"]
        if (
            lifecycle_kind is LifecycleKind.CONTINUOUS
            and name in _CONTINUOUS_CONFERENCE_MILESTONES
        ):
            review_reasons.append(f"continuous_venue_{name}")
            continue
        authority = _supporting_authority(item["evidence_ids"], authorities)
        classification = classify_source(
            catalog, result["venue_id"], item["source_url"]
        )
        if (
            authority is None
            or authority.url != item["source_url"]
            or classification.trust.value != item["source_type"]
            or classification.trust is SourceTrust.UNTRUSTED
        ):
            review_reasons.append(f"{name}_lacks_authoritative_evidence")
            continue
        promoted, conflict = _promote_milestone(
            updated,
            name,
            _verified_milestone(item, verified_at=verified_at_text),
        )
        evidence = tuple(sorted(set(item["evidence_ids"])))
        if not conflict:
            supported[name] = evidence
        if promoted:
            _append_unique(promoted_evidence, evidence)
        if conflict:
            review_reasons.append(f"{name}_milestone_conflict")

    current = LifecycleState(updated["lifecycle_state"])
    candidate = _highest_lifecycle_candidate(
        updated, supported, verified_at, lifecycle_kind
    )
    transition_applied = False
    if (
        candidate is not None
        and _LIFECYCLE_ORDER.index(candidate[0])
        > _LIFECYCLE_ORDER.index(current)
    ):
        target, evidence_ids, reason = candidate
        transition_identity = artifact_fingerprint(
            {
                "verification_id": result["verification_id"],
                "target": target.value,
            }
        )
        transition = apply_transition(
            updated,
            TransitionRequest(
                transition_id=f"transition:{transition_identity[:32]}",
                to_state=target,
                evidence_ids=tuple(
                    dict.fromkeys((result["verification_id"], *evidence_ids))
                ),
                reason=f"{reason} from deterministic verification",
                actor=TransitionActor.DETERMINISTIC_VERIFIER,
                at=verified_at_text,
            ),
            lifecycle_kind=lifecycle_kind,
        )
        updated = transition.state
        transition_applied = transition.applied

    _append_unique(updated["evidence_ids"], promoted_evidence)
    unmanaged = set(updated["blockers"]) - _MANAGED_BLOCKERS
    managed = _managed_blockers(
        updated, result, venue, review_reasons
    )
    updated["blockers"] = sorted(unmanaged | managed)
    updated = schedule_next_check(
        updated,
        policy,
        verified_at,
        lifecycle_kind=lifecycle_kind,
    )
    validate_contract(ContractName.CONFERENCE_STATE, updated)
    actions = _route_actions(
        updated,
        result,
        venue,
        transition_applied=transition_applied,
        supported_facets=supported,
        review_reasons=review_reasons,
    )
    return ReductionOutcome(updated, actions, True, transition_applied)
