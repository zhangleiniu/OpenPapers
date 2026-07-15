"""P2.8S: the authorized live canary for the exact P2.8 composition.

This module runs `automation.production_wakeup.run_production_control_wakeup`
with both of its private injection seams left empty, so
`ProductionDiscoveryEffect` constructs the real `GeminiSearchGroundingProvider`
(Vertex AI, Application Default Credentials) and `ProductionVerificationEffect`
constructs the real `LiveHttpFetcher`. It changes nothing in
`automation/production_wakeup.py` itself; it only supplies a private marked
root, one preselected archival venue/year, and a bounded sanitized evidence
summary around that unmodified boundary.

It preselects `colt`/2025 rather than accepting an operator-chosen
venue/year: P5.S already proved (181/181 valid archival PDFs) that this
exact venue/year is reachable through domains the P2.7 production crawl
policy already approves (`learningtheory.org`, `proceedings.mlr.press`), so
there is no after-the-fact temptation to pick whichever venue a live
discovery call happened to favor.

Nothing here is imported by or connected to
`automation/local_service/production.py`. This module is never installed or
scheduled; it exists to be invoked exactly once by
`automation/run_production_wakeup_canary.py` under explicit `--live`
authorization.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from automation.control_state import ControlStateRepository
from automation.discovery import DiscoveryError, safe_error_summary, utc_now
from automation.domain import Writer
from automation.local_control_plane import LocalControlCompositionError
from automation.production_discovery import AutomaticDiscoveryError
from automation.production_verification import AutomaticVerificationError
from automation.production_wakeup import (
    ProductionControlPlaneConfig,
    ProductionControlPlaneConfigError,
    run_production_control_wakeup,
)
from automation.verification import FetchBoundaryError


CANARY_VENUE_ID = "colt"
CANARY_YEAR = 2025

_MARKER_NAME = ".p2-8s-live-canary.v1.json"
_MARKER_VERSION = 1
_MARKER_PURPOSE = "p2_8s_live_canary"
_SUMMARY_NAME = "summary.v1.json"
_PRODUCTION_MARKER_NAME = ".production-control.v1.json"
_ISOLATED_SHADOW_MARKER_NAME = ".isolated-shadow.v1.json"
_MAX_JSON_BYTES = 65_536

# Every one of these is raised only *before* the corresponding effect (or a
# later stage of the same bounded wakeup) completes; none leaves partial
# provider/fetch state uncommitted. See automation/production_wakeup.py and
# its P2.6/P2.7 dependencies for the exact refusal points.
_REFUSAL_ERRORS: tuple[type[BaseException], ...] = (
    ProductionControlPlaneConfigError,
    DiscoveryError,
    AutomaticDiscoveryError,
    AutomaticVerificationError,
    FetchBoundaryError,
    LocalControlCompositionError,
)


class CanaryRootError(ValueError):
    """Raised when the P2.8S canary root or its marker cannot be trusted."""


@dataclass(frozen=True)
class CanaryRootState:
    """A validated private root and the one stamped wakeup identity in it."""

    root: Path
    control_state_path: Path
    automation_root: Path
    review_root: Path
    scheduled_for: datetime
    venue_id: str
    year: int


@dataclass(frozen=True)
class CanaryOutcome:
    """Bounded, secret-free result of one canary invocation."""

    replayed: bool
    outcome: str
    refusal_category: str | None
    selection_count: int
    verification_ids: tuple[str, ...]
    retained_jobs: tuple[dict[str, Any], ...]


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalized(path: Path, *, field: str) -> Path:
    resolved = Path(path)
    if not resolved.is_absolute():
        raise CanaryRootError(f"{field} must be an absolute path")
    if Path(os.path.normpath(resolved)) != resolved:
        raise CanaryRootError(f"{field} must be a normalized path")
    return resolved


def _private_directory(path: Path, *, create: bool) -> None:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError:
        raise CanaryRootError(f"canary directory is unavailable: {path.name}") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise CanaryRootError(f"canary directory metadata is unsafe: {path.name}")


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_JSON_BYTES:
        raise CanaryRootError("canary artifact exceeds its bound")
    return encoded + b"\n"


def _retain_create_once(path: Path, content: bytes) -> None:
    """Create ``path`` atomically, accepting only byte-identical replay."""
    _private_directory(path.parent, create=False)
    if path.exists() or path.is_symlink():
        try:
            metadata = path.lstat()
            existing = path.read_bytes()
        except OSError:
            raise CanaryRootError(f"{path.name} is unreadable") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or existing != content
        ):
            raise CanaryRootError(
                f"{path.name} already has different or unsafe content"
            )
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = None
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError:
        if not path.exists():
            raise CanaryRootError(f"{path.name} immutable create raced") from None
        _retain_create_once(path, content)
    except OSError:
        raise CanaryRootError(f"{path.name} could not be retained") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_marker(marker_path: Path) -> Mapping[str, Any]:
    try:
        metadata = marker_path.lstat()
        payload = json.loads(marker_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise CanaryRootError("canary marker is unreadable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or marker_path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        or not isinstance(payload, dict)
    ):
        raise CanaryRootError("canary marker metadata is unsafe")
    return payload


def prepare_canary_root(
    root: Path, *, clock: Callable[[], datetime]
) -> CanaryRootState:
    """Validate and exactly mark a private P2.8S canary root.

    A brand-new empty root is stamped with the one fixed venue/year and the
    ``scheduled_for`` timestamp read from ``clock()`` at that moment. A root
    already carrying that exact marker instead returns the previously
    stamped ``scheduled_for`` unchanged, which is what lets a second
    invocation reach the accepted exact-replay path with zero further
    provider/fetch calls. Any other nonempty root, or a root that also
    carries a production or host-shadow marker, fails closed.
    """
    normalized = _normalized(Path(root), field="canary_root")
    _private_directory(normalized, create=True)
    if (normalized / _PRODUCTION_MARKER_NAME).exists():
        raise CanaryRootError(
            "canary root carries a production-control marker and cannot be used"
        )
    if (normalized / _ISOLATED_SHADOW_MARKER_NAME).exists():
        raise CanaryRootError(
            "canary root carries a host-shadow marker and cannot be used"
        )

    control_root = normalized / "control"
    automation_root = normalized / "automation"
    review_root = normalized / "review"
    marker_path = normalized / _MARKER_NAME

    try:
        existing_names = {item.name for item in normalized.iterdir()}
    except OSError:
        raise CanaryRootError("canary root cannot be inspected") from None
    allowed_names = {_MARKER_NAME, "control", "automation", "review"}
    if existing_names and _MARKER_NAME not in existing_names:
        raise CanaryRootError("nonempty canary root is not marked")
    if not existing_names <= allowed_names:
        raise CanaryRootError("canary root contains unknown entries")

    if marker_path.exists():
        marker = _read_marker(marker_path)
        if (
            marker.get("schema_version") != _MARKER_VERSION
            or marker.get("purpose") != _MARKER_PURPOSE
            or marker.get("venue_id") != CANARY_VENUE_ID
            or marker.get("year") != CANARY_YEAR
            or not isinstance(marker.get("scheduled_for"), str)
        ):
            raise CanaryRootError("canary marker is invalid or has drifted")
        try:
            scheduled_for = datetime.fromisoformat(
                str(marker["scheduled_for"]).replace("Z", "+00:00")
            )
        except ValueError:
            raise CanaryRootError("canary marker scheduled_for is invalid") from None
        if scheduled_for.tzinfo is None:
            raise CanaryRootError("canary marker scheduled_for must be aware")
        scheduled_for = scheduled_for.astimezone(timezone.utc)
    else:
        candidate = clock()
        if not isinstance(candidate, datetime) or candidate.tzinfo is None:
            raise CanaryRootError("clock must return an aware datetime")
        scheduled_for = candidate.astimezone(timezone.utc)
        marker = {
            "schema_version": _MARKER_VERSION,
            "purpose": _MARKER_PURPOSE,
            "venue_id": CANARY_VENUE_ID,
            "year": CANARY_YEAR,
            "scheduled_for": _timestamp(scheduled_for),
        }
        _retain_create_once(marker_path, _canonical_bytes(marker))

    for path in (control_root, automation_root, review_root):
        _private_directory(path, create=True)

    return CanaryRootState(
        root=normalized,
        control_state_path=control_root / "state.sqlite3",
        automation_root=automation_root,
        review_root=review_root,
        scheduled_for=scheduled_for,
        venue_id=CANARY_VENUE_ID,
        year=CANARY_YEAR,
    )


def seed_due_conference_state(
    control_state_path: Path,
    *,
    venue_id: str,
    year: int,
    due_at: datetime,
) -> None:
    """Seed the one canonical all-``unknown`` due row a fresh database needs.

    A brand-new local-owned database has no conference-state row at all, so
    ``run_local_control_wakeup`` would select nothing and call neither
    effect. This stores exactly the same all-``unknown`` v1 shape already
    used everywhere else in this codebase for a never-before-observed
    venue/year, with ``next_check_at`` set to ``due_at`` so it becomes due on
    the first wakeup. If a row already exists (a replay run, or a rerun
    after an earlier refusal advanced nothing), this is a no-op: only the
    real wakeup composition may advance conference state.
    """
    if not isinstance(due_at, datetime) or due_at.tzinfo is None:
        raise CanaryRootError("due_at must be an aware datetime")
    due = due_at.astimezone(timezone.utc)
    with ControlStateRepository(
        control_state_path, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: due,
    ) as repository:
        if repository.get_conference_state(venue_id, year) is not None:
            return
        lease = repository.acquire_lease("p2-8s-live-canary-seed")
        try:
            state = {
                "schema_version": 1,
                "venue_id": venue_id,
                "year": year,
                "lifecycle_state": "unknown",
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
                "next_check_at": _timestamp(due),
                "next_check_reason": "unknown_schedule_fallback",
                "evidence_ids": [],
                "blockers": [],
                "transition_history": [],
                "updated_at": _timestamp(due),
            }
            repository.store_conference_state(
                state, expected_revision=0, lease=lease, stored_at=due,
            )
        finally:
            repository.release_lease(lease)


def _record_summary(state: CanaryRootState, outcome: CanaryOutcome) -> None:
    payload = {
        "schema_version": 1,
        "purpose": _MARKER_PURPOSE,
        "venue_id": state.venue_id,
        "year": state.year,
        "scheduled_for": _timestamp(state.scheduled_for),
        "outcome": outcome.outcome,
        "refusal_category": outcome.refusal_category,
        "selection_count": outcome.selection_count,
        "verification_ids": list(outcome.verification_ids),
        "retained_jobs": list(outcome.retained_jobs),
    }
    _retain_create_once(state.review_root / _SUMMARY_NAME, _canonical_bytes(payload))


def run_canary(
    root: Path,
    *,
    gemini_project: str,
    gemini_location: str,
    gemini_model: str,
    clock: Callable[[], datetime] = utc_now,
    _discovery_provider_factory=None,
    _verification_fetcher=None,
) -> CanaryOutcome:
    """Run the exact P2.8 composition once, live, inside a marked root.

    Leaving ``_discovery_provider_factory``/``_verification_fetcher`` at
    their default ``None`` is what makes this call live: those are the same
    private test-only seams P2.6/P2.7/P2.8 already expose for their own
    fixture tests, and this module's own tests are the only caller that ever
    supplies a fake through them.
    """
    root_state = prepare_canary_root(root, clock=clock)
    seed_due_conference_state(
        root_state.control_state_path,
        venue_id=root_state.venue_id,
        year=root_state.year,
        due_at=root_state.scheduled_for,
    )
    config = ProductionControlPlaneConfig(
        control_state_path=root_state.control_state_path,
        automation_root=root_state.automation_root,
        gemini_project=gemini_project,
        gemini_location=gemini_location,
        gemini_model=gemini_model,
    )
    try:
        result = run_production_control_wakeup(
            config,
            scheduled_for=root_state.scheduled_for,
            clock=clock,
            _discovery_provider_factory=_discovery_provider_factory,
            _verification_fetcher=_verification_fetcher,
        )
    except _REFUSAL_ERRORS as exc:
        category = (
            safe_error_summary(exc)
            if isinstance(exc, DiscoveryError)
            else type(exc).__name__
        )
        outcome = CanaryOutcome(
            replayed=False,
            outcome="refused",
            refusal_category=category,
            selection_count=0,
            verification_ids=(),
            retained_jobs=(),
        )
        _record_summary(root_state, outcome)
        return outcome

    if result.replayed:
        # Nothing new happened: the decisive evidence is whatever the first
        # (non-replayed) invocation already recorded in review/summary.v1.json.
        return CanaryOutcome(
            replayed=True,
            outcome="replayed",
            refusal_category=None,
            selection_count=0,
            verification_ids=(),
            retained_jobs=(),
        )

    retained_jobs = tuple(
        {
            "job_id": retention.record.job_id,
            "action_type": retention.record.action.get("action_type"),
            "state": retention.record.state,
        }
        for selection in result.selections
        for retention in selection.execution_retentions
        if retention.applied
    )
    verification_ids = tuple(
        verification_id
        for selection in result.selections
        for verification_id in selection.verification_ids
    )
    outcome = CanaryOutcome(
        replayed=False,
        outcome="action_retained" if retained_jobs else "no_action",
        refusal_category=None,
        selection_count=len(result.selections),
        verification_ids=verification_ids,
        retained_jobs=retained_jobs,
    )
    _record_summary(root_state, outcome)
    return outcome
