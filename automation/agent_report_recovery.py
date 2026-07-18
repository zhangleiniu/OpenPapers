"""Explicitly recover one isolated success report from an idempotency collision."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from automation.agent_run_notifications import deliver_agent_run_email
from automation.agent_success_rehearsal import (
    AgentSuccessRehearsalError,
    _AUTHORIZATION_ID,
    _independent_validation,
    _private_directory,
)
from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.local_service.agent_control import validate_agent_production_root
from automation.resend_notifications import (
    ResendNotificationTransport,
    recipient_fingerprints,
)


def _worktree_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise AgentSuccessRehearsalError("recovery worktree path is invalid")
    return path


def run(args) -> dict[str, object]:
    if not args.authorize_resend_live:
        raise AgentSuccessRehearsalError("report recovery requires resend authority")
    for value in (args.authorization_id, args.recovery_id):
        if not _AUTHORIZATION_ID.fullmatch(value):
            raise AgentSuccessRehearsalError("report recovery identity is invalid")
    if args.authorization_id == args.recovery_id:
        raise AgentSuccessRehearsalError("report recovery identity must be new")

    repository_root = Path(args.repository_root).resolve()
    external_root = Path(args.external_root).resolve()
    configuration, secrets = validate_agent_production_root(
        args.internal_root, repository_root
    )
    if not configuration.external_effects_enabled or secrets is None:
        raise AgentSuccessRehearsalError("installed agent production is not enabled")
    if recipient_fingerprints(secrets.email_to) \
            != configuration.agent.resend_recipient_sha256s:
        raise AgentSuccessRehearsalError("approved recipients changed")

    container = _private_directory(external_root / "agent-success-rehearsals")
    root = _private_directory(container / args.authorization_id)
    state = root / "control-state.sqlite3"
    now = datetime.now(timezone.utc)
    with ControlStateRepository(
        state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: now,
    ) as repository:
        history = repository.agent_run_history("colt", 2011)
        if len(history) != 1 or history[0].disposition != "success":
            raise AgentSuccessRehearsalError("recovery target is not one success")
        run_id = history[0].run_id
        artifact = repository.get_agent_execution_artifact(run_id)
        report = repository.get_agent_run_report(run_id)
        if artifact is None or artifact.lifecycle != "terminal" \
                or report is None or report.status != "permanent_failure" \
                or report.last_failure_category != "protocol_error":
            raise AgentSuccessRehearsalError("report is not recoverable")
        worktree = _worktree_path(artifact.worktree_path)

    paper_count, issues = _independent_validation(worktree)
    transport = ResendNotificationTransport(
        api_key=secrets.resend_api_key,
        email_from=secrets.email_from,
        email_to=secrets.email_to,
    )
    delivery = deliver_agent_run_email(
        state, run_id, transport, clock=lambda: datetime.now(timezone.utc),
        notification_namespace=args.recovery_id,
        retry_permanent_protocol_error=True,
    )
    return {
        "authorization_id": args.authorization_id,
        "recovery_id": args.recovery_id,
        "disposition": "success",
        "papers": paper_count,
        "independent_validation_issues": issues,
        "report_status": delivery.status,
        "delivery_attempt": delivery.attempt_number,
        "proved_success": delivery.status == "delivered" and issues == {},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--internal-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--external-root", type=Path, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--recovery-id", required=True)
    parser.add_argument("--authorize-resend-live", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    result = run(build_parser().parse_args(argv))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["proved_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
