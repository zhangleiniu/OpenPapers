"""Run-centric composition and replay-safe delivery for agent email reports."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from automation.control_state import (
    DEFAULT_LEASE_TTL_SECONDS,
    AgentRunReportError,
    ControlStateRepository,
)
from automation.domain import Writer
from automation.notifications import (
    FailureCategory,
    NotificationIntent,
    NotificationKind,
    NotificationTransport,
    TransportFailure,
    TransportReceipt,
    classify_transport_failure,
    redact_text,
    validate_notification_intent,
)


@dataclass(frozen=True)
class AgentRunEmailOutcome:
    run_id: str
    status: str
    attempted: bool
    attempt_number: int | None
    failure_category: str | None
    receipt_id: str | None


_NOTIFICATION_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None \
            or value.utcoffset() is None:
        raise ValueError("agent report clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _notification_sources(
    run_id: str, namespace: str | None = None,
) -> tuple[str, ...]:
    if namespace is not None and (
        not isinstance(namespace, str)
        or _NOTIFICATION_NAMESPACE.fullmatch(namespace) is None
    ):
        raise AgentRunReportError("agent report namespace is invalid")
    if namespace is None:
        return (run_id,)
    return tuple(sorted((run_id, f"agent-report-scope:{namespace}")))


def _notification_id(source_ids: tuple[str, ...]) -> str:
    digest = hashlib.sha256("\0".join(source_ids).encode("utf-8")).hexdigest()
    return f"notification:immediate:{digest}"


def build_agent_run_email(
    repository: ControlStateRepository, run_id: str,
    *, notification_namespace: str | None = None,
) -> NotificationIntent:
    """Compose one bounded report from immutable agent-run state."""
    attempt = repository.get_agent_run_attempt(run_id)
    artifact = repository.get_agent_execution_artifact(run_id)
    report = repository.get_agent_run_report(run_id)
    if attempt is None or artifact is None or report is None:
        raise AgentRunReportError("agent run review state is incomplete")
    if attempt.disposition == "active" or artifact.lifecycle != "terminal":
        raise AgentRunReportError("agent run report is not terminal")
    changed = artifact.changed_files[:100]
    changed_lines = [f"- {item}" for item in changed] or ["- none"]
    if len(artifact.changed_files) > len(changed):
        changed_lines.append(
            f"- [TRUNCATED {len(artifact.changed_files) - len(changed)} entries]"
        )
    retry = report.next_check_at or f"stopped ({report.schedule_status})"
    body = redact_text("\n".join((
        "OpenPapers agent run report",
        f"Run: {run_id}",
        f"Venue/year: {attempt.venue_id} {attempt.year}",
        f"Disposition: {attempt.disposition}",
        f"Explanation: {attempt.explanation}",
        f"Worktree: {artifact.worktree_path}",
        f"Branch: {artifact.branch_name}",
        f"Retry state: {retry}",
        "Changed files:",
        *changed_lines,
    )))
    if len(body) > 100_000:
        raise AgentRunReportError("agent run email exceeds its message bound")
    evidence_id = "agent-artifact:" + hashlib.sha256(
        run_id.encode("utf-8")
    ).hexdigest()
    sources = _notification_sources(run_id, notification_namespace)
    intent = NotificationIntent(
        notification_id=_notification_id(sources),
        kind=NotificationKind.IMMEDIATE,
        source_ids=sources,
        created_at=report.created_at,
        subject=f"OpenPapers agent: {attempt.venue_id.upper()} {attempt.year} "
        f"{attempt.disposition}",
        body=body,
        evidence_ids=(evidence_id,),
        run_ids=(run_id,),
    )
    validate_notification_intent(intent)
    return intent


def deliver_agent_run_email(
    state_path: Path,
    run_id: str,
    transport: NotificationTransport,
    *,
    clock: Callable[[], datetime],
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    notification_namespace: str | None = None,
    retry_permanent_protocol_error: bool = False,
) -> AgentRunEmailOutcome:
    """Attempt one pending/retryable report; terminal replays do no I/O."""
    now = _utc(clock())
    with ControlStateRepository(
        Path(state_path), writer=Writer.LOCAL_CONTROL_PLANE, clock=clock
    ) as repository:
        lease = repository.acquire_lease(
            "agent-run-email", ttl_seconds=lease_ttl_seconds
        )
        try:
            intent = build_agent_run_email(
                repository, run_id,
                notification_namespace=notification_namespace,
            )
            delivery = repository.prepare_agent_run_report_delivery(
                run_id, started_at=now, lease=lease,
                retry_permanent_protocol_error=retry_permanent_protocol_error,
            )
            if delivery is None:
                report = repository.get_agent_run_report(run_id)
                if report is None:
                    raise AgentRunReportError("suppressed agent report disappeared")
                return AgentRunEmailOutcome(
                    run_id, report.status, False, None,
                    report.last_failure_category, report.receipt_id,
                )
            try:
                receipt = transport.send(
                    intent, idempotency_key=intent.notification_id
                )
                if not isinstance(receipt, TransportReceipt):
                    raise TransportFailure(FailureCategory.PROTOCOL_ERROR)
            except TransportFailure as exc:
                decision = classify_transport_failure(exc)
                status = "retryable" if decision.retryable else "permanent_failure"
                report = repository.complete_agent_run_report_delivery(
                    delivery.report_id,
                    delivery.attempt_number,
                    status=status,
                    completed_at=_utc(clock()),
                    failure_category=decision.category.value,
                    lease=lease,
                )
                return AgentRunEmailOutcome(
                    run_id, report.status, True, delivery.attempt_number,
                    report.last_failure_category, None,
                )
            report = repository.complete_agent_run_report_delivery(
                delivery.report_id,
                delivery.attempt_number,
                status="delivered",
                completed_at=_utc(clock()),
                receipt_id=receipt.receipt_id,
                lease=lease,
            )
            return AgentRunEmailOutcome(
                run_id, report.status, True, delivery.attempt_number,
                None, report.receipt_id,
            )
        finally:
            repository.release_lease(lease)
