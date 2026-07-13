"""P3.S synthetic notification canary; isolated from P3.4 shadow output."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from automation.cases import derive_case_id, validate_case_state
from automation.configuration import load_policy_config
from automation.control_state import ControlStateRepository
from automation.notifications import (
    FailureCategory,
    NotificationIntent,
    NotificationTransport,
    TransportFailure,
    build_digest_notification,
    deliver_notification,
    validate_notification_intent,
)
from automation.reminders import CaseDigest, build_case_digest
from automation.resend_notifications import recipient_fingerprint


MARKER_NAME = "canary-request.v1.json"
RESULT_NAME = "canary-result.v1.json"
STATE_NAME = "control-state.sqlite3"


class NotificationCanaryError(ValueError):
    """Raised when the manual canary cannot remain inside its boundary."""


@dataclass(frozen=True)
class CanaryRun:
    result: dict[str, Any]
    replayed: bool


def _utc(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NotificationCanaryError(f"{field} is invalid") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise NotificationCanaryError(f"{field} is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NotificationCanaryError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value, field="timestamp").isoformat().replace("+00:00", "Z")


def _synthetic_state(
    *, venue_id: str, blocker: str, age_days: int, now: datetime
) -> dict[str, Any]:
    observed = now - timedelta(days=age_days)
    stamp = _timestamp(observed)
    state = {
        "schema_version": 1,
        "case_id": derive_case_id(venue_id, 2099, blocker),
        "venue_id": venue_id,
        "year": 2099,
        "blocker": blocker,
        "status": "open",
        "summary": (
            f"Synthetic P3.S {blocker} test event; no conference state or "
            "production evidence is represented."
        ),
        "evidence_ids": [f"synthetic-evidence:p3s:{venue_id}"],
        "first_observed_at": stamp,
        "last_checked_at": stamp,
        "last_meaningful_change_at": stamp,
        "snoozed_until": None,
        "resolution": None,
    }
    validate_case_state(state)
    return state


def build_synthetic_canary(now: datetime) -> tuple[CaseDigest, NotificationIntent]:
    """Build one fixed weekly/monthly/dormant synthetic digest."""
    resolved = _utc(now, field="canary time")
    states = (
        _synthetic_state(
            venue_id="p3s-weekly",
            blocker="no_pdf",
            age_days=7,
            now=resolved,
        ),
        _synthetic_state(
            venue_id="p3s-monthly",
            blocker="no_public_list",
            age_days=30,
            now=resolved,
        ),
        _synthetic_state(
            venue_id="p3s-dormant",
            blocker="unknown_download_source",
            age_days=84,
            now=resolved,
        ),
    )
    digest = build_case_digest(states, load_policy_config(), resolved)
    if (
        digest.due_count != 3
        or tuple(group.cadence.value for group in digest.groups)
        != ("weekly", "monthly", "dormant")
        or any(len(group.items) != 1 for group in digest.groups)
    ):
        raise NotificationCanaryError("synthetic fatigue fixture is inconsistent")
    intent = build_digest_notification(
        digest, run_ids=("synthetic-run:p3s:delivery-canary",)
    )
    intent = replace(
        intent,
        subject=f"[P3.S SYNTHETIC CANARY] {intent.subject}",
        body=(
            "P3.S SYNTHETIC DELIVERY CANARY — TEST DATA ONLY\n"
            "This message does not describe a real conference or retained case.\n\n"
            + intent.body
        ),
    )
    validate_notification_intent(intent)
    return digest, intent


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotificationCanaryError(f"invalid canary artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise NotificationCanaryError(f"invalid canary artifact: {path.name}")
    return value


def _write_new(path: Path, value: Mapping[str, Any]) -> None:
    serialized = json.dumps(value, indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
    except FileExistsError as exc:
        raise NotificationCanaryError(f"canary artifact already exists: {path.name}") from exc


def _marker(
    *, generated_at: datetime, recipient_sha256: str, intent: NotificationIntent
) -> dict[str, Any]:
    return {
        "canary_version": 1,
        "external_request_limit": 1,
        "generated_at": _timestamp(generated_at),
        "notification_id": intent.notification_id,
        "recipient_sha256": recipient_sha256,
        "source_count": len(intent.source_ids),
        "synthetic_only": True,
    }


def _load_or_create_marker(
    root: Path,
    *,
    recipient_sha256: str,
    now: datetime,
) -> tuple[dict[str, Any], CaseDigest, NotificationIntent, bool]:
    marker_path = root / MARKER_NAME
    if root.exists() and not root.is_dir():
        raise NotificationCanaryError("canary output root must be a directory")
    if not root.exists():
        root.mkdir(parents=True)
    existing = tuple(root.iterdir())
    if marker_path not in existing:
        if existing:
            raise NotificationCanaryError(
                "existing output root is not a marked P3.S canary"
            )
        generated_at = _utc(now, field="canary time")
        digest, intent = build_synthetic_canary(generated_at)
        marker = _marker(
            generated_at=generated_at,
            recipient_sha256=recipient_sha256,
            intent=intent,
        )
        _write_new(marker_path, marker)
        return marker, digest, intent, False

    marker = _json(marker_path)
    expected_keys = {
        "canary_version",
        "external_request_limit",
        "generated_at",
        "notification_id",
        "recipient_sha256",
        "source_count",
        "synthetic_only",
    }
    if set(marker) != expected_keys:
        raise NotificationCanaryError("canary marker has unknown or missing fields")
    generated_at = _utc(marker["generated_at"], field="marker generated_at")
    digest, intent = build_synthetic_canary(generated_at)
    expected = _marker(
        generated_at=generated_at,
        recipient_sha256=recipient_sha256,
        intent=intent,
    )
    if marker != expected:
        raise NotificationCanaryError("canary marker does not match this request")
    return marker, digest, intent, True


class _FailureTransport:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, intent, *, idempotency_key):
        self.calls += 1
        raise TransportFailure(FailureCategory.RATE_LIMITED)


def run_local_drills(intent: NotificationIntent, now: datetime) -> dict[str, Any]:
    """Exercise failure and isolated-root rollback without external I/O."""
    temporary_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="openpapers-p3s-drill-") as directory:
        temporary_path = Path(directory)
        transport = _FailureTransport()
        with ControlStateRepository(temporary_path / STATE_NAME) as repository:
            lease = repository.acquire_lease("p3s-failure-drill")
            try:
                outcome = deliver_notification(
                    intent,
                    repository=repository,
                    lease=lease,
                    transport=transport,
                    now=now,
                )
                case_state_untouched = repository.list_cases() == ()
                attempts = repository.notification_attempt_history(
                    intent.notification_id
                )
            finally:
                repository.release_lease(lease)
    return {
        "case_state_untouched": case_state_untouched,
        "external_request_count": 0,
        "failure_category": (
            outcome.failure_category.value
            if outcome.failure_category is not None
            else None
        ),
        "failure_attempt_count": len(attempts),
        "retryable_failure_recorded": outcome.status == "retryable",
        "rollback_root_removed": (
            temporary_path is not None and not temporary_path.exists()
        ),
    }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _result(
    *,
    marker: Mapping[str, Any],
    digest: CaseDigest,
    intent: NotificationIntent,
    outcome: Any,
    request_count: int,
    attempt_count: int,
    drills: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "canary_version": 1,
        "delivery": {
            "attempt_count": attempt_count,
            "attempted": outcome.attempted,
            "external_request_count": request_count,
            "failure_category": (
                outcome.failure_category.value
                if outcome.failure_category is not None
                else None
            ),
            "notification_id": intent.notification_id,
            "receipt_sha256": (
                _sha256(outcome.receipt_id)
                if outcome.receipt_id is not None
                else None
            ),
            "status": outcome.status,
        },
        "drills": dict(drills),
        "fatigue": {
            "body_characters": len(intent.body),
            "body_lines": len(intent.body.splitlines()),
            "due_count": digest.due_count,
            "group_count": len(digest.groups),
            "groups": [group.cadence.value for group in digest.groups],
            "subject_characters": len(intent.subject),
        },
        "generated_at": marker["generated_at"],
        "recipient_sha256": marker["recipient_sha256"],
        "synthetic_only": True,
    }


def run_notification_canary(
    *,
    output_root: Path,
    approved_recipient_sha256: str,
    api_key: str,
    email_from: str,
    email_to: str,
    transport_factory: Callable[..., NotificationTransport],
    now: datetime,
) -> CanaryRun:
    if (
        not isinstance(approved_recipient_sha256, str)
        or len(approved_recipient_sha256) != 64
        or any(c not in "0123456789abcdef" for c in approved_recipient_sha256)
    ):
        raise NotificationCanaryError(
            "approved recipient fingerprint must be 64 lowercase hex characters"
        )
    actual_recipient_sha256 = recipient_fingerprint(email_to)
    if actual_recipient_sha256 != approved_recipient_sha256:
        raise NotificationCanaryError("approved recipient fingerprint mismatch")
    if not api_key or not email_from:
        raise NotificationCanaryError("canary credentials and sender are required")

    root = output_root.expanduser().resolve()
    marker, digest, intent, replayed = _load_or_create_marker(
        root,
        recipient_sha256=actual_recipient_sha256,
        now=now,
    )
    drills = run_local_drills(intent, _utc(marker["generated_at"], field="generated_at"))
    with ControlStateRepository(root / STATE_NAME) as repository:
        lease = repository.acquire_lease("p3s-delivery-canary")
        try:
            existing = repository.get_notification(intent.notification_id)
            if (
                existing is not None
                and existing.attempt_count > 0
                and existing.status == "retryable"
            ):
                raise NotificationCanaryError(
                    "bounded canary root cannot retry a consumed attempt"
                )
            transport = transport_factory(
                api_key=api_key,
                email_from=email_from,
                email_to=email_to,
            )
            outcome = deliver_notification(
                intent,
                repository=repository,
                lease=lease,
                transport=transport,
                now=marker["generated_at"],
            )
            attempts = repository.notification_attempt_history(
                intent.notification_id
            )
            if repository.list_cases() != ():
                raise NotificationCanaryError("canary repository contains case state")
        finally:
            repository.release_lease(lease)

    result_path = root / RESULT_NAME
    candidate = _result(
        marker=marker,
        digest=digest,
        intent=intent,
        outcome=outcome,
        request_count=getattr(transport, "request_count", -1),
        attempt_count=len(attempts),
        drills=drills,
    )
    if candidate["delivery"]["external_request_count"] not in {0, 1}:
        raise NotificationCanaryError("transport exceeded or hid its request count")
    if result_path.exists():
        stored = _json(result_path)
        if stored["delivery"]["notification_id"] != intent.notification_id:
            raise NotificationCanaryError("stored canary result identity changed")
        if outcome.status != stored["delivery"]["status"]:
            raise NotificationCanaryError("stored canary delivery status changed")
        result = stored
    else:
        _write_new(result_path, candidate)
        result = candidate
    return CanaryRun(result=result, replayed=replayed)
