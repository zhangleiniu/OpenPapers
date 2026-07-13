"""P3.3 notification intents, redaction, and injected delivery boundary."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from automation.contracts import (
    ContractName,
    ContractValidationError,
    validate_contract,
)
from automation.domain import BlockerCode, SecretBoundaryError, assert_secret_free
from automation.reminders import CaseDigest, ReminderCadence


MAX_DIGEST_ITEMS = 100
MAX_SUMMARY_CHARS = 2_000
MAX_MESSAGE_CHARS = 100_000


class NotificationError(ValueError):
    """Raised when notification data or delivery semantics fail closed."""


class NotificationKind(str, Enum):
    IMMEDIATE = "immediate"
    DIGEST = "digest"


class FailureCategory(str, Enum):
    """Bounded, secret-free transport failure categories."""

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    AUTHENTICATION = "authentication"
    INVALID_RECIPIENT = "invalid_recipient"
    REJECTED = "rejected"
    PAYLOAD_INVALID = "payload_invalid"
    PROTOCOL_ERROR = "protocol_error"
    UNKNOWN = "unknown"


_RETRYABLE_FAILURES = frozenset(
    {
        FailureCategory.TIMEOUT,
        FailureCategory.RATE_LIMITED,
        FailureCategory.UNAVAILABLE,
        FailureCategory.UNKNOWN,
    }
)
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{2,127}$")
_VENUE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_AUTH_PATTERN = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+"
)
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|secret|token|cookie|"
    r"private[_-]?key|client[_-]?secret)\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
    r"[A-Za-z0-9_-]{8,}\b"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "key",
        "password",
        "sig",
        "signature",
        "token",
        "x-amz-credential",
        "x-amz-signature",
        "x-goog-credential",
        "x-goog-signature",
    }
)
_CASE_STATUSES = frozenset({"open", "stalled", "dormant", "snoozed"})


@dataclass(frozen=True)
class NotificationIntent:
    """One immutable, strictly validated notification message."""

    notification_id: str
    kind: NotificationKind
    source_ids: tuple[str, ...]
    created_at: str
    subject: str
    body: str
    evidence_ids: tuple[str, ...]
    run_ids: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        if not isinstance(self.kind, NotificationKind):
            raise NotificationError("notification kind must use NotificationKind")
        if not all(
            isinstance(values, tuple)
            for values in (self.source_ids, self.evidence_ids, self.run_ids)
        ):
            raise NotificationError("notification ID collections must be tuples")
        return {
            "schema_version": 1,
            "notification_id": self.notification_id,
            "kind": self.kind.value,
            "source_ids": list(self.source_ids),
            "created_at": self.created_at,
            "subject": self.subject,
            "body": self.body,
            "evidence_ids": list(self.evidence_ids),
            "run_ids": list(self.run_ids),
        }


@dataclass(frozen=True)
class TransportReceipt:
    """Secret-free acknowledgement returned by an injected transport."""

    receipt_id: str


class TransportFailure(Exception):
    """Typed transport failure that deliberately carries no raw error text."""

    def __init__(self, category: FailureCategory | str) -> None:
        try:
            self.category = FailureCategory(category)
        except (TypeError, ValueError) as exc:
            raise NotificationError(
                f"unknown transport failure category: {category!r}"
            ) from exc
        super().__init__(self.category.value)


@dataclass(frozen=True)
class FailureDecision:
    category: FailureCategory
    retryable: bool


@dataclass(frozen=True)
class DeliveryOutcome:
    """Observable result of one delivery coordination request."""

    notification_id: str
    status: str
    attempted: bool
    attempt_number: int | None
    failure_category: FailureCategory | None
    receipt_id: str | None


@runtime_checkable
class NotificationTransport(Protocol):
    """Effect boundary implemented only by fakes in P3.3."""

    def send(
        self,
        intent: NotificationIntent,
        *,
        idempotency_key: str,
    ) -> TransportReceipt:
        """Attempt one delivery using the stable notification identity."""


class NotificationDeliveryStore(Protocol):
    """Structural store interface used without importing SQLite here."""

    def prepare_notification_delivery(
        self,
        intent: NotificationIntent,
        *,
        lease: Any,
        started_at: datetime | str,
    ) -> Any | None: ...

    def complete_notification_delivery(
        self,
        notification_id: str,
        attempt_number: int,
        *,
        status: str,
        lease: Any,
        completed_at: datetime | str,
        failure_category: str | None = None,
        receipt_id: str | None = None,
    ) -> Any: ...

    def get_notification(self, notification_id: str) -> Any | None: ...


def _utc_timestamp(value: datetime | str, *, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NotificationError(f"{field} must be a valid datetime") from exc
    else:
        raise NotificationError(f"{field} must be a datetime or string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NotificationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_id(prefix: str, parts: Sequence[str]) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _validated_ids(
    values: Sequence[str],
    *,
    field: str,
    required: bool,
    maximum: int,
) -> tuple[str, ...]:
    resolved = tuple(values)
    if required and not resolved:
        raise NotificationError(f"{field} requires at least one stable ID")
    if len(resolved) > maximum:
        raise NotificationError(f"{field} exceeds its {maximum}-ID bound")
    if len(set(resolved)) != len(resolved):
        raise NotificationError(f"{field} must contain unique IDs")
    for value in resolved:
        if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
            raise NotificationError(f"{field} contains an invalid stable ID")
        if redact_text(value) != value:
            raise NotificationError(f"{field} contains credential-shaped text")
    return tuple(sorted(resolved))


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;)]":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "[REDACTED_UNSAFE_URL]" + trailing
    if parsed.username is not None or parsed.password is not None:
        return "[REDACTED_URL_CREDENTIALS]" + trailing
    query = parse_qsl(parsed.query, keep_blank_values=True)
    changed = False
    safe_query: list[tuple[str, str]] = []
    for key, value in query:
        if key.lower() in _SENSITIVE_QUERY_KEYS:
            safe_query.append((key, "[REDACTED]"))
            changed = True
        else:
            safe_query.append((key, value))
    if not changed:
        return raw + trailing
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(safe_query),
            parsed.fragment,
        )
    ) + trailing


def redact_text(value: str) -> str:
    """Return bounded message text with common credential forms removed."""
    if not isinstance(value, str):
        raise NotificationError("notification text must be a string")
    redacted = _PRIVATE_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", value)
    redacted = _URL_PATTERN.sub(_redact_url, redacted)
    redacted = _AUTH_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}=[REDACTED]", redacted
    )
    redacted = _JWT_PATTERN.sub("[REDACTED_JWT]", redacted)
    return redacted


def _summary(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotificationError("summary must be non-blank text")
    redacted = redact_text(value.strip())
    if len(redacted) > MAX_SUMMARY_CHARS:
        redacted = redacted[:MAX_SUMMARY_CHARS] + " [TRUNCATED]"
    return redacted


def _message_text(lines: Sequence[str]) -> str:
    value = "\n".join(lines)
    if not value.strip() or len(value) > MAX_MESSAGE_CHARS:
        raise NotificationError("rendered notification exceeds its message bound")
    return value


def validate_notification_intent(
    intent: NotificationIntent | Mapping[str, Any],
) -> None:
    """Apply schema and P3.3 semantic checks to an intent or stored payload."""
    payload = (
        intent.to_payload()
        if isinstance(intent, NotificationIntent)
        else dict(intent)
    )
    try:
        assert_secret_free(payload)
        validate_contract(ContractName.NOTIFICATION_INTENT, payload)
    except (SecretBoundaryError, ContractValidationError) as exc:
        raise NotificationError(f"notification intent is invalid: {exc}") from exc
    canonical_created = _utc_timestamp(payload["created_at"], field="created_at")
    if canonical_created != payload["created_at"]:
        raise NotificationError("created_at must be canonical UTC")
    source_ids = _validated_ids(
        payload["source_ids"], field="source_ids", required=True, maximum=100
    )
    evidence_ids = _validated_ids(
        payload["evidence_ids"], field="evidence_ids", required=True, maximum=1000
    )
    run_ids = _validated_ids(
        payload["run_ids"], field="run_ids", required=False, maximum=100
    )
    if list(source_ids) != payload["source_ids"]:
        raise NotificationError("source_ids must be in canonical order")
    if list(evidence_ids) != payload["evidence_ids"]:
        raise NotificationError("evidence_ids must be in canonical order")
    if list(run_ids) != payload["run_ids"]:
        raise NotificationError("run_ids must be in canonical order")
    if redact_text(payload["subject"]) != payload["subject"]:
        raise NotificationError("subject contains unredacted credential text")
    if redact_text(payload["body"]) != payload["body"]:
        raise NotificationError("body contains unredacted credential text")
    expected_id = _stable_id(
        f"notification:{payload['kind']}", tuple(payload["source_ids"])
    )
    if payload["notification_id"] != expected_id:
        raise NotificationError("notification_id does not match its stable sources")


def notification_intent_from_payload(payload: Mapping[str, Any]) -> NotificationIntent:
    """Rebuild and validate a defensive intent from persisted JSON."""
    validate_notification_intent(payload)
    return NotificationIntent(
        notification_id=payload["notification_id"],
        kind=NotificationKind(payload["kind"]),
        source_ids=tuple(payload["source_ids"]),
        created_at=payload["created_at"],
        subject=payload["subject"],
        body=payload["body"],
        evidence_ids=tuple(payload["evidence_ids"]),
        run_ids=tuple(payload["run_ids"]),
    )


def build_immediate_notification(
    *,
    event_id: str,
    occurred_at: datetime | str,
    venue_id: str,
    year: int,
    summary: str,
    evidence_ids: Sequence[str],
    run_ids: Sequence[str] = (),
) -> NotificationIntent:
    """Build one stable immediate intent from an explicitly supplied event."""
    if not isinstance(venue_id, str) or _VENUE_PATTERN.fullmatch(venue_id) is None:
        raise NotificationError("venue_id is invalid")
    if not isinstance(year, int) or isinstance(year, bool) or not 1900 <= year <= 2200:
        raise NotificationError("year must be an integer between 1900 and 2200")
    sources = _validated_ids((event_id,), field="source_ids", required=True, maximum=1)
    evidence = _validated_ids(
        evidence_ids, field="evidence_ids", required=True, maximum=1000
    )
    runs = _validated_ids(run_ids, field="run_ids", required=False, maximum=100)
    created_at = _utc_timestamp(occurred_at, field="occurred_at")
    safe_summary = _summary(summary)
    lines = [
        "Immediate automation notification",
        f"Venue: {venue_id}",
        f"Year: {year}",
        f"Summary: {safe_summary}",
        "Evidence references:",
        *(f"- {evidence_id}" for evidence_id in evidence),
        "Run references:",
        *(f"- {run_id}" for run_id in runs),
    ]
    if not runs:
        lines.append("- none")
    intent = NotificationIntent(
        notification_id=_stable_id("notification:immediate", sources),
        kind=NotificationKind.IMMEDIATE,
        source_ids=sources,
        created_at=created_at,
        subject=f"OpenPapers immediate: {venue_id.upper()} {year}",
        body=_message_text(lines),
        evidence_ids=evidence,
        run_ids=runs,
    )
    validate_notification_intent(intent)
    return intent


def reminder_source_id(
    case_id: str,
    cadence: ReminderCadence | str,
    slot: int,
    due_at: datetime,
) -> str:
    """Return the stable source claim for one case reminder slot."""
    try:
        resolved_cadence = ReminderCadence(cadence)
    except (TypeError, ValueError) as exc:
        raise NotificationError("reminder cadence is invalid") from exc
    _validated_ids((case_id,), field="case_id", required=True, maximum=1)
    if not isinstance(slot, int) or isinstance(slot, bool) or slot < 1:
        raise NotificationError("reminder slot must be a positive integer")
    return _stable_id(
        "reminder",
        (
            case_id,
            resolved_cadence.value,
            str(slot),
            _utc_timestamp(due_at, field="due_at"),
        ),
    )


def build_digest_notification(
    digest: CaseDigest,
    *,
    run_ids: Sequence[str] = (),
) -> NotificationIntent:
    """Build one stable grouped digest intent from explicit P3.2 data."""
    if not isinstance(digest, CaseDigest):
        raise NotificationError("digest must be P3.2 CaseDigest data")
    generated_at = _utc_timestamp(digest.generated_at, field="generated_at")
    seen_cadences: set[ReminderCadence] = set()
    seen_cases: set[str] = set()
    items = []
    for group in digest.groups:
        if not isinstance(group.cadence, ReminderCadence):
            raise NotificationError("digest group cadence is invalid")
        if group.cadence in seen_cadences or not group.items:
            raise NotificationError("digest groups must be unique and non-empty")
        seen_cadences.add(group.cadence)
        for item in group.items:
            if item.cadence is not group.cadence:
                raise NotificationError("digest item cadence does not match its group")
            _validated_ids(
                (item.case_id,), field="case_id", required=True, maximum=1
            )
            if item.case_id in seen_cases:
                raise NotificationError("digest contains a duplicate case")
            seen_cases.add(item.case_id)
            if (
                not isinstance(item.venue_id, str)
                or _VENUE_PATTERN.fullmatch(item.venue_id) is None
            ):
                raise NotificationError("digest venue_id is invalid")
            if (
                not isinstance(item.year, int)
                or isinstance(item.year, bool)
                or not 1900 <= item.year <= 2200
            ):
                raise NotificationError("digest year is invalid")
            try:
                BlockerCode(item.blocker)
            except (TypeError, ValueError) as exc:
                raise NotificationError("digest blocker is invalid") from exc
            if item.status not in _CASE_STATUSES:
                raise NotificationError("digest status is invalid")
            if (
                not isinstance(item.age_days, int)
                or isinstance(item.age_days, bool)
                or item.age_days < 0
            ):
                raise NotificationError("digest age_days is invalid")
            if (
                not isinstance(item.slot, int)
                or isinstance(item.slot, bool)
                or item.slot < 1
            ):
                raise NotificationError("digest slot is invalid")
            meaningful_at = _utc_timestamp(
                item.last_meaningful_change_at,
                field="last_meaningful_change_at",
            )
            due_at = _utc_timestamp(item.due_at, field="due_at")
            if due_at > generated_at or meaningful_at > generated_at:
                raise NotificationError("digest item time exceeds generation time")
            _validated_ids(
                item.evidence_ids,
                field="digest evidence_ids",
                required=True,
                maximum=1000,
            )
            items.append(item)
    if digest.due_count != len(items) or not items:
        raise NotificationError("digest must contain at least one consistent due item")
    if len(items) > MAX_DIGEST_ITEMS:
        raise NotificationError(
            f"digest exceeds its {MAX_DIGEST_ITEMS}-item delivery bound"
        )
    runs = _validated_ids(run_ids, field="run_ids", required=False, maximum=100)
    sources = tuple(
        sorted(
            reminder_source_id(
                item.case_id,
                item.cadence,
                item.slot,
                item.due_at,
            )
            for item in items
        )
    )
    if len(set(sources)) != len(sources):
        raise NotificationError("digest contains duplicate reminder slots")
    evidence = _validated_ids(
        tuple(
            sorted(
                {
                    evidence_id
                    for item in items
                    for evidence_id in item.evidence_ids
                }
            )
        ),
        field="evidence_ids",
        required=True,
        maximum=1000,
    )
    lines = [
        "OpenPapers unresolved-case digest",
        f"Generated at: {generated_at}",
        f"Due cases: {len(items)}",
    ]
    for group in digest.groups:
        lines.append("")
        lines.append(f"{group.cadence.value.upper()} ({len(group.items)})")
        for item in group.items:
            lines.extend(
                (
                    f"- Case: {item.case_id}",
                    f"  Venue/year: {item.venue_id} {item.year}",
                    f"  Blocker/status: {item.blocker} / {item.status}",
                    "  Age/slot: "
                    f"{item.age_days} days / {item.cadence.value}:{item.slot}",
                    f"  Due at: {_utc_timestamp(item.due_at, field='due_at')}",
                    f"  Summary: {_summary(item.summary)}",
                    "  Evidence: " + ", ".join(sorted(item.evidence_ids)),
                )
            )
    lines.extend(("", "Run references:"))
    lines.extend(f"- {run_id}" for run_id in runs)
    if not runs:
        lines.append("- none")
    intent = NotificationIntent(
        notification_id=_stable_id("notification:digest", sources),
        kind=NotificationKind.DIGEST,
        source_ids=sources,
        created_at=generated_at,
        subject=f"OpenPapers unresolved cases: {len(items)} due",
        body=_message_text(lines),
        evidence_ids=evidence,
        run_ids=runs,
    )
    validate_notification_intent(intent)
    return intent


def classify_transport_failure(error: BaseException) -> FailureDecision:
    """Classify a transport exception without retaining its raw text."""
    category = (
        error.category
        if isinstance(error, TransportFailure)
        else FailureCategory.UNKNOWN
    )
    return FailureDecision(category, category in _RETRYABLE_FAILURES)


def _validated_receipt(receipt: object) -> TransportReceipt:
    if not isinstance(receipt, TransportReceipt):
        raise TransportFailure(FailureCategory.PROTOCOL_ERROR)
    _validated_ids(
        (receipt.receipt_id,), field="receipt_id", required=True, maximum=1
    )
    return receipt


def validate_receipt_id(receipt_id: str) -> None:
    """Validate a secret-free opaque transport acknowledgement ID."""
    _validated_ids((receipt_id,), field="receipt_id", required=True, maximum=1)


def deliver_notification(
    intent: NotificationIntent,
    *,
    repository: NotificationDeliveryStore,
    lease: Any,
    transport: NotificationTransport,
    now: datetime | str,
) -> DeliveryOutcome:
    """Coordinate one fake/injected attempt around durable state.

    P3.3 deliberately does not find events or due cases. The caller supplies an
    already constructed intent. Transport I/O occurs after the repository has
    committed an in-flight claim and outside any SQLite transaction.
    """
    validate_notification_intent(intent)
    attempt = repository.prepare_notification_delivery(
        intent, lease=lease, started_at=now
    )
    if attempt is None:
        record = repository.get_notification(intent.notification_id)
        if record is None:
            raise NotificationError("suppressed delivery has no durable record")
        category = (
            FailureCategory(record.last_failure_category)
            if record.last_failure_category is not None
            else None
        )
        return DeliveryOutcome(
            notification_id=intent.notification_id,
            status=record.status,
            attempted=False,
            attempt_number=None,
            failure_category=category,
            receipt_id=record.receipt_id,
        )

    try:
        receipt = _validated_receipt(
            transport.send(intent, idempotency_key=intent.notification_id)
        )
    except TransportFailure as exc:
        decision = classify_transport_failure(exc)
        status = "retryable" if decision.retryable else "permanent_failure"
        record = repository.complete_notification_delivery(
            intent.notification_id,
            attempt.attempt_number,
            status=status,
            lease=lease,
            completed_at=now,
            failure_category=decision.category.value,
        )
        return DeliveryOutcome(
            notification_id=intent.notification_id,
            status=record.status,
            attempted=True,
            attempt_number=attempt.attempt_number,
            failure_category=decision.category,
            receipt_id=None,
        )

    record = repository.complete_notification_delivery(
        intent.notification_id,
        attempt.attempt_number,
        status="delivered",
        lease=lease,
        completed_at=now,
        receipt_id=receipt.receipt_id,
    )
    return DeliveryOutcome(
        notification_id=intent.notification_id,
        status=record.status,
        attempted=True,
        attempt_number=attempt.attempt_number,
        failure_category=None,
        receipt_id=receipt.receipt_id,
    )
