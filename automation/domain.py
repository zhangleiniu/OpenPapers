"""Pure Phase 0 state, ownership, idempotency, and safety vocabulary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from automation.contracts import (
    ContractName,
    artifact_fingerprint,
    validate_contract,
)


class LifecycleState(str, Enum):
    UNKNOWN = "unknown"
    SCHEDULED = "scheduled"
    CONFERENCE_ENDED = "conference_ended"
    PAPER_LIST_RELEASED = "paper_list_released"
    METADATA_READY = "metadata_ready"
    PDF_PARTIAL = "pdf_partial"
    PDF_READY = "pdf_ready"
    INGESTION_QUEUED = "ingestion_queued"
    INGESTING = "ingesting"
    VALIDATED = "validated"
    PUBLISHED = "published"


class LifecycleKind(str, Enum):
    ANNUAL = "annual"
    CONTINUOUS = "continuous"


class BlockerCode(str, Enum):
    NO_PUBLIC_LIST = "no_public_list"
    NO_PDF = "no_pdf"
    UNKNOWN_DOWNLOAD_SOURCE = "unknown_download_source"
    UNSUPPORTED_SCRAPER = "unsupported_scraper"
    SCRAPER_FAILED = "scraper_failed"
    VALIDATION_FAILED = "validation_failed"
    AGENT_PENDING = "agent_pending"
    CODEX_PATCH_PENDING = "codex_patch_pending"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    CRAWL_POLICY_DENIED = "crawl_policy_denied"


class ActionType(str, Enum):
    RECHECK_AT = "recheck_at"
    NOTIFY_TRANSITION = "notify_transition"
    CREATE_OR_UPDATE_CASE = "create_or_update_case"
    QUEUE_EXISTING_SCRAPER = "queue_existing_scraper"
    QUEUE_CODEX_DIAGNOSIS = "queue_codex_diagnosis"
    REQUEST_HUMAN_REVIEW = "request_human_review"
    PREPARE_PROMOTION_CANDIDATE = "prepare_promotion_candidate"


class Permission(str, Enum):
    MONITOR = "monitor"
    METADATA_FETCH = "metadata_fetch"
    PDF_FETCH_FOR_PROCESSING = "pdf_fetch_for_processing"
    STORE_INTERNAL_COPY = "store_internal_copy"
    REDISTRIBUTE_METADATA = "redistribute_metadata"
    REDISTRIBUTE_PDF = "redistribute_pdf"


class TransitionActor(str, Enum):
    DETERMINISTIC_VERIFIER = "deterministic_verifier"
    JOB_RESULT_CONSUMER = "job_result_consumer"
    HUMAN = "human"


class Writer(str, Enum):
    CLOUD_CONTROL_PLANE = "cloud_control_plane"
    LOCAL_CONTROL_PLANE = "local_control_plane"
    MAC_WORKER = "mac_worker"


class ArtifactKind(str, Enum):
    CONTROL_STATE = "control_state"
    SOURCE_SNAPSHOT = "source_snapshot"
    DISCOVERY_RESULT = "discovery_result"
    VERIFICATION_RESULT = "verification_result"
    JOB_RESULT = "job_result"
    MANIFEST = "manifest"
    CODEX_RESULT = "codex_result"


class DomainError(ValueError):
    """Base class for rejected Phase 0 domain operations."""


class InvalidTransitionError(DomainError):
    """Raised when lifecycle movement is not explicitly allowed."""


class EvidenceReplayConflictError(DomainError):
    """Raised when a stable transition/evidence identity changes meaning."""


class DuplicateJobResultError(DomainError):
    """Raised when a job ID is reused with a different immutable result."""


class OwnershipError(DomainError):
    """Raised when a writer attempts to create an artifact it does not own."""


class SecretBoundaryError(DomainError):
    """Raised when a contract artifact contains a credential-shaped key."""


_ALLOWED_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.UNKNOWN: frozenset({
        LifecycleState.SCHEDULED,
        LifecycleState.CONFERENCE_ENDED,
        LifecycleState.PAPER_LIST_RELEASED,
        LifecycleState.METADATA_READY,
        LifecycleState.PDF_PARTIAL,
        LifecycleState.PDF_READY,
    }),
    LifecycleState.SCHEDULED: frozenset({
        LifecycleState.CONFERENCE_ENDED,
        LifecycleState.PAPER_LIST_RELEASED,
        LifecycleState.METADATA_READY,
        LifecycleState.PDF_PARTIAL,
        LifecycleState.PDF_READY,
    }),
    LifecycleState.CONFERENCE_ENDED: frozenset({
        LifecycleState.PAPER_LIST_RELEASED,
        LifecycleState.METADATA_READY,
        LifecycleState.PDF_PARTIAL,
        LifecycleState.PDF_READY,
    }),
    LifecycleState.PAPER_LIST_RELEASED: frozenset({
        LifecycleState.METADATA_READY,
        LifecycleState.PDF_PARTIAL,
        LifecycleState.PDF_READY,
    }),
    LifecycleState.METADATA_READY: frozenset({
        LifecycleState.PDF_PARTIAL,
        LifecycleState.PDF_READY,
        LifecycleState.INGESTION_QUEUED,
    }),
    LifecycleState.PDF_PARTIAL: frozenset({LifecycleState.PDF_READY}),
    LifecycleState.PDF_READY: frozenset({LifecycleState.INGESTION_QUEUED}),
    LifecycleState.INGESTION_QUEUED: frozenset({LifecycleState.INGESTING}),
    LifecycleState.INGESTING: frozenset({LifecycleState.VALIDATED}),
    LifecycleState.VALIDATED: frozenset({LifecycleState.PUBLISHED}),
    LifecycleState.PUBLISHED: frozenset(),
}


_WRITER_OWNERSHIP: dict[ArtifactKind, frozenset[Writer]] = {
    ArtifactKind.CONTROL_STATE: frozenset({
        Writer.CLOUD_CONTROL_PLANE,
        Writer.LOCAL_CONTROL_PLANE,
    }),
    ArtifactKind.SOURCE_SNAPSHOT: frozenset({Writer.CLOUD_CONTROL_PLANE}),
    ArtifactKind.DISCOVERY_RESULT: frozenset({Writer.CLOUD_CONTROL_PLANE}),
    ArtifactKind.VERIFICATION_RESULT: frozenset({Writer.CLOUD_CONTROL_PLANE}),
    ArtifactKind.JOB_RESULT: frozenset({Writer.MAC_WORKER}),
    ArtifactKind.MANIFEST: frozenset({Writer.MAC_WORKER}),
    ArtifactKind.CODEX_RESULT: frozenset({Writer.MAC_WORKER}),
}


_SECRET_KEYS = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
})
_SECRET_KEY_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_cookie",
    "_cookies",
    "_credential",
    "_credentials",
    "_password",
    "_passwd",
    "_private_key",
    "_secret",
    "_token",
    "_tokens",
)


@dataclass(frozen=True)
class TransitionRequest:
    transition_id: str
    to_state: LifecycleState | str
    evidence_ids: tuple[str, ...]
    reason: str
    actor: TransitionActor | str
    at: str


@dataclass(frozen=True)
class TransitionOutcome:
    state: dict[str, Any]
    applied: bool


def allowed_transitions(state: LifecycleState | str) -> frozenset[LifecycleState]:
    """Return the explicit next states for a lifecycle state."""
    try:
        current = LifecycleState(state)
    except ValueError as exc:
        raise InvalidTransitionError(f"unknown lifecycle state: {state!r}") from exc
    return _ALLOWED_TRANSITIONS[current]


def _coerce_request(request: TransitionRequest) -> tuple[LifecycleState, TransitionActor]:
    try:
        target = LifecycleState(request.to_state)
    except ValueError as exc:
        raise InvalidTransitionError(
            f"unknown lifecycle target: {request.to_state!r}") from exc
    try:
        actor = TransitionActor(request.actor)
    except ValueError as exc:
        raise InvalidTransitionError(
            f"actor cannot authorize a transition: {request.actor!r}") from exc
    if not request.evidence_ids or len(set(request.evidence_ids)) != len(request.evidence_ids):
        raise InvalidTransitionError(
            "a transition requires unique, non-empty evidence IDs")
    return target, actor


def apply_transition(
    state: Mapping[str, Any],
    request: TransitionRequest,
    *,
    lifecycle_kind: LifecycleKind | str = LifecycleKind.ANNUAL,
) -> TransitionOutcome:
    """Apply one evidence-backed transition or return an idempotent replay."""
    validate_contract(ContractName.CONFERENCE_STATE, state)
    target, actor = _coerce_request(request)
    try:
        kind = LifecycleKind(lifecycle_kind)
    except ValueError as exc:
        raise InvalidTransitionError(
            f"unknown lifecycle kind: {lifecycle_kind!r}") from exc
    if kind is LifecycleKind.CONTINUOUS and target is LifecycleState.CONFERENCE_ENDED:
        raise InvalidTransitionError(
            "continuous publications cannot enter conference_ended")

    evidence = tuple(request.evidence_ids)
    for recorded in state["transition_history"]:
        same_identity = recorded["transition_id"] == request.transition_id
        same_evidence = set(recorded["evidence_ids"]) == set(evidence)
        equivalent = (
            recorded["new_state"] == target.value
            and same_evidence
            and recorded["reason"] == request.reason
            and recorded["actor"] == actor.value
        )
        if same_identity and not equivalent:
            raise EvidenceReplayConflictError(
                f"transition ID {request.transition_id!r} changed meaning")
        if equivalent:
            return TransitionOutcome(deepcopy(dict(state)), applied=False)
        if same_evidence:
            raise EvidenceReplayConflictError(
                "the same evidence cannot authorize different transitions")

    current = LifecycleState(state["lifecycle_state"])
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"transition {current.value} -> {target.value} is not allowed")

    updated = deepcopy(dict(state))
    updated["lifecycle_state"] = target.value
    updated["updated_at"] = request.at
    existing_evidence = list(updated["evidence_ids"])
    existing_evidence.extend(
        item for item in evidence if item not in existing_evidence)
    updated["evidence_ids"] = existing_evidence
    updated["transition_history"].append({
        "transition_id": request.transition_id,
        "previous_state": current.value,
        "new_state": target.value,
        "evidence_ids": list(evidence),
        "reason": request.reason,
        "actor": actor.value,
        "at": request.at,
    })
    assert_secret_free(updated)
    validate_contract(ContractName.CONFERENCE_STATE, updated)
    return TransitionOutcome(updated, applied=True)


class JobResultRegistry:
    """In-memory expression of the immutable ``job-results/<job-id>`` rule."""

    def __init__(self) -> None:
        self._results: dict[str, tuple[str, dict[str, Any]]] = {}

    def accept(self, result: Mapping[str, Any]) -> bool:
        """Return true for a first result, false for an identical replay."""
        assert_secret_free(result)
        validate_contract(ContractName.JOB_RESULT, result)
        job_id = result["job_id"]
        fingerprint = artifact_fingerprint(result)
        previous = self._results.get(job_id)
        if previous is not None:
            if previous[0] == fingerprint:
                return False
            raise DuplicateJobResultError(
                f"job {job_id!r} already has a different immutable result")
        self._results[job_id] = (fingerprint, deepcopy(dict(result)))
        return True

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Return a defensive copy of an accepted result."""
        stored = self._results.get(job_id)
        return deepcopy(stored[1]) if stored is not None else None


def assert_writer_allowed(
    writer: Writer | str,
    artifact: ArtifactKind | str,
) -> None:
    """Reject writes that violate the cloud/Mac single-owner boundary."""
    try:
        resolved_writer = Writer(writer)
        resolved_artifact = ArtifactKind(artifact)
    except ValueError as exc:
        raise OwnershipError(f"unknown writer or artifact: {writer!r}, {artifact!r}") from exc
    if resolved_writer not in _WRITER_OWNERSHIP[resolved_artifact]:
        raise OwnershipError(
            f"{resolved_writer.value} cannot write {resolved_artifact.value}")


def _normalized_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def assert_secret_free(payload: Any, path: Sequence[str] = ()) -> None:
    """Reject credential-shaped keys anywhere in a contract artifact."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            normalized = _normalized_key(key)
            if (normalized in _SECRET_KEYS
                    or normalized.startswith("authorization_")
                    or normalized.endswith(_SECRET_KEY_SUFFIXES)):
                location = ".".join((*path, str(key)))
                raise SecretBoundaryError(
                    f"credential-shaped field is forbidden at {location}")
            assert_secret_free(value, (*path, str(key)))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            assert_secret_free(value, (*path, str(index)))
