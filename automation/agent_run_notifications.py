"""Run-centric composition and replay-safe delivery for agent email reports."""

from __future__ import annotations

import hashlib
import json
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


_MAX_METADATA_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class ProductionComparison:
    """Paper-count comparison between a run's output and canonical data."""

    line: str
    subject_suffix: str = ""
    id_warning: str | None = None


def _metadata_ids(path: Path) -> list[str]:
    if path.stat().st_size > _MAX_METADATA_BYTES:
        raise ValueError("metadata file is too large")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("metadata is not a record list")
    ids = [record.get("id") for record in payload if isinstance(record, dict)]
    if len(ids) != len(payload) or not all(isinstance(i, str) and i for i in ids):
        raise ValueError("metadata records lack ids")
    return ids


def compare_with_production(
    worktree: Path, canonical_root: Path | None, venue_id: str, year: int,
) -> ProductionComparison:
    """Compare a success run's metadata with the canonical corpus.

    Both files are only read; any problem degrades to an explanatory line
    instead of failing the report. The worktree output is immutable once the
    run is terminal; the canonical file is read at composition time.
    """
    relative = Path("metadata") / venue_id / f"{venue_id}_{year}.json"
    if canonical_root is None:
        return ProductionComparison(
            "Production comparison: not configured (canonical_data_root unset)"
        )
    try:
        run_ids = _metadata_ids(Path(worktree) / "data" / relative)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return ProductionComparison(
            f"Production comparison: run metadata unavailable ({type(exc).__name__})"
        )
    canonical_path = Path(canonical_root) / relative
    if not canonical_path.exists():
        return ProductionComparison(
            f"Production comparison: run has {len(run_ids)} papers; production "
            "has no file for this venue/year yet (new data).",
            f" ({len(run_ids)} new)",
        )
    try:
        production_ids = _metadata_ids(canonical_path)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return ProductionComparison(
            "Production comparison: production metadata unreadable "
            f"({type(exc).__name__})"
        )
    run_set, production_set = set(run_ids), set(production_ids)
    added = len(run_set - production_set)
    missing = len(production_set - run_set)
    line = (
        f"Production comparison: run {len(run_ids)} papers vs production "
        f"{len(production_ids)}; {added} new, {missing} production ids absent "
        "from the run."
    )
    if missing == 0 and added == 0:
        return ProductionComparison(
            line + " Production already matches; nothing to promote.",
            " (matches production)",
        )
    warning = None
    if missing:
        warning = (
            f"WARNING: {missing} existing production ids are not in this run. "
            "If the run uses a different id scheme, do not promote its data "
            "as-is (it would rename papers); rescrape over the canonical "
            "metadata so existing ids are kept."
        )
    suffix = f" (+{added} vs production)" if added and not missing else (
        f" (+{added}/-{missing} vs production)"
    )
    return ProductionComparison(line, suffix, warning)


def _schedule_line(disposition: str, next_check_at: str | None,
                   schedule_status: str) -> str:
    if next_check_at is None:
        if schedule_status == "completed":
            return "Schedule: completed (no further checks)"
        return f"Schedule: stopped ({schedule_status})"
    label = "Retry at" if disposition == "failed" else "Next check"
    return f"{label}: {next_check_at}"


def build_agent_run_email(
    repository: ControlStateRepository, run_id: str,
    *, notification_namespace: str | None = None,
    canonical_data_root: Path | None = None,
) -> NotificationIntent:
    """Compose one bounded report from immutable agent-run state.

    ``canonical_data_root`` (the production corpus) is optional; when given,
    a ``success`` report compares the run's paper ids with production.
    """
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
    lines = [
        f"OpenPapers agent run report: {attempt.venue_id} {attempt.year} "
        f"{attempt.disposition}",
        f"Explanation: {attempt.explanation}",
    ]
    subject_suffix = ""
    if attempt.disposition == "success":
        comparison = compare_with_production(
            Path(artifact.worktree_path), canonical_data_root,
            attempt.venue_id, attempt.year,
        )
        subject_suffix = comparison.subject_suffix
        lines.append(comparison.line)
        if comparison.id_warning:
            lines.append(comparison.id_warning)
    lines.append(_schedule_line(
        attempt.disposition, report.next_check_at, report.schedule_status
    ))
    if attempt.disposition == "success":
        data_root = (
            str(canonical_data_root) if canonical_data_root is not None
            else "<canonical corpus>"
        )
        lines += [
            "Promotion: nothing has been copied into production. Dry run "
            "first, from the installed runtime directory as the service "
            "user (add --apply after review):",
            f"  python -m automation.agent_operations promote-run --state "
            f"<state> --run-id {run_id} --data-root {data_root} "
            "--repository-root <primary checkout>",
            "  See 'Promoting a successful run' in the operations guide.",
        ]
    lines += ["Changed files:", *changed_lines]
    lines += [
        "Details:",
        f"  Run: {run_id}",
        f"  Worktree: {artifact.worktree_path}",
        f"  Branch: {artifact.branch_name}",
    ]
    body = redact_text("\n".join(lines))
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
        f"{attempt.disposition}{subject_suffix}",
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
    canonical_data_root: Path | None = None,
) -> AgentRunEmailOutcome:
    """Attempt one pending/retryable report; terminal replays do no I/O.

    A ``not_ready`` disposition ("checked, nothing published yet") never
    reaches a human inbox: it is the overwhelming majority of runs and
    carries nothing to review, so it is marked delivered with a sentinel
    receipt instead of sent.
    """
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
                canonical_data_root=canonical_data_root,
            )
            attempt = repository.get_agent_run_attempt(run_id)
            if attempt is None:
                raise AgentRunReportError("agent run review state is incomplete")
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
            if attempt.disposition == "not_ready":
                report = repository.complete_agent_run_report_delivery(
                    delivery.report_id, delivery.attempt_number,
                    status="delivered", completed_at=_utc(clock()),
                    receipt_id=f"suppressed:{attempt.disposition}",
                    lease=lease,
                )
                return AgentRunEmailOutcome(
                    run_id, report.status, False, delivery.attempt_number,
                    None, report.receipt_id,
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
