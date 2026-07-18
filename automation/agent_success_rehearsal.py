"""One create-only live success rehearsal outside production and canonical data."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from automation.agent_credentials import validate_agent_credential_context
from automation.agent_run_notifications import deliver_agent_run_email
from automation.codex_agent import SubprocessCodexInvoker, run_claimed_codex_agent
from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.due_policy import claim_due_agent_run
from automation.event_dates import (
    EventDateEstimate,
    EventDateTarget,
    initialize_event_dates,
)
from automation.local_service.agent_control import (
    validate_agent_production_root,
    validate_agent_source,
)
from automation.resend_notifications import (
    ResendNotificationTransport,
    recipient_fingerprints,
)
from postprocessing.validate_year import validate


TARGET_VENUE = "colt"
TARGET_YEAR = 2011
_AUTHORIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


class AgentSuccessRehearsalError(ValueError):
    """Raised before unsafe or out-of-scope rehearsal behavior."""


class _HistoricalDateProvider:
    name = "rehearsal-fixed-history"
    model = "none"
    prompt_version = "v1"

    def estimate(self, request) -> EventDateEstimate:
        if request.venue_id != TARGET_VENUE or request.year != TARGET_YEAR:
            raise AgentSuccessRehearsalError("rehearsal target changed")
        return EventDateEstimate(date(TARGET_YEAR, 7, 7), "Fixed historical date.")


def _private_directory(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink() \
            or metadata.st_uid != os.geteuid() \
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise AgentSuccessRehearsalError("rehearsal directory is unsafe")
    return path


def _clone_source(source: Path, destination: Path) -> None:
    completed = subprocess.run(
        ("git", "clone", "--no-hardlinks", "--quiet", str(source), str(destination)),
        text=True, capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise AgentSuccessRehearsalError("rehearsal source clone failed")
    subprocess.run(
        ("git", "remote", "remove", "origin"), cwd=destination,
        text=True, capture_output=True, check=True,
    )


def _independent_validation(worktree: Path) -> tuple[int, dict[str, int]]:
    metadata = worktree / "data" / "metadata" / TARGET_VENUE \
        / f"{TARGET_VENUE}_{TARGET_YEAR}.json"
    try:
        papers = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentSuccessRehearsalError(
            "successful agent result lacks readable rehearsal metadata"
        ) from exc
    if not isinstance(papers, list) or not papers:
        raise AgentSuccessRehearsalError(
            "successful agent result lacks nonempty rehearsal metadata"
        )
    issues = validate(papers, worktree / "data", level="archival")
    if issues:
        raise AgentSuccessRehearsalError(
            f"independent archival validation failed: {sorted(issues)}"
        )
    return len(papers), issues


def run(args) -> dict[str, object]:
    """Execute the fixed authorized rehearsal and return secret-free proof."""
    if not all((args.authorize_codex_live, args.authorize_downloads_live,
                args.authorize_resend_live)):
        raise AgentSuccessRehearsalError("all live rehearsal effects require authority")
    if not _AUTHORIZATION_ID.fullmatch(args.authorization_id):
        raise AgentSuccessRehearsalError("rehearsal authorization identity is invalid")

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
    credentials = validate_agent_credential_context(
        args.internal_root, require_codex_auth=True
    )
    installed_source = validate_agent_source(
        external_root / "agent-source", configuration.agent_source_commit
    )

    container = external_root / "agent-success-rehearsals"
    if not container.exists():
        container.mkdir(mode=0o700, parents=False)
    _private_directory(container)
    root = _private_directory(container / args.authorization_id, create=True)
    source = root / "source"
    _clone_source(installed_source, source)
    execution = root / "execution"
    execution.mkdir(mode=0o700)
    state = root / "control-state.sqlite3"
    now = datetime.now(timezone.utc)

    initialized = initialize_event_dates(
        state, (EventDateTarget(TARGET_VENUE, TARGET_YEAR),),
        _HistoricalDateProvider(), clock=lambda: now,
    )
    if initialized.scheduled_count != 1:
        raise AgentSuccessRehearsalError("rehearsal schedule initialization failed")
    claimed = claim_due_agent_run(
        state, clock=lambda: now, policy=configuration.agent.due_policy
    )
    if claimed.claim is None:
        raise AgentSuccessRehearsalError("rehearsal run was not claimed")
    runs_root = execution / "agent-runs"
    worktree = runs_root / claimed.claim.run_id.split(":", 1)[-1][:16]
    codex_environment = credentials.codex_environment()
    codex_environment["SCRAPER_DATA_ROOT"] = str(worktree / "data")
    codex_environment["SCRAPER_LOG_FILE"] = str(worktree / "scraper.log")
    outcome = run_claimed_codex_agent(
        state, source, runs_root, claimed.claim,
        clock=lambda: datetime.now(timezone.utc),
        invoker=SubprocessCodexInvoker(codex_environment),
        policy=configuration.agent.due_policy,
        config=configuration.agent.codex,
    )
    paper_count = None
    validation_issues = None
    if outcome.result.disposition == "success":
        paper_count, validation_issues = _independent_validation(outcome.worktree_path)

    transport = ResendNotificationTransport(
        api_key=secrets.resend_api_key,
        email_from=secrets.email_from,
        email_to=secrets.email_to,
    )
    delivery = deliver_agent_run_email(
        state, claimed.claim.run_id, transport,
        clock=lambda: datetime.now(timezone.utc),
        notification_namespace=args.authorization_id,
    )
    with ControlStateRepository(
        state, writer=Writer.LOCAL_CONTROL_PLANE,
        clock=lambda: datetime.now(timezone.utc),
    ) as repository:
        attempt = repository.get_agent_run_attempt(claimed.claim.run_id)
        artifact = repository.get_agent_execution_artifact(claimed.claim.run_id)
        report = repository.get_agent_run_report(claimed.claim.run_id)
    proved = bool(
        attempt and attempt.disposition == "success"
        and artifact and artifact.lifecycle == "terminal"
        and report and report.status == "delivered"
        and validation_issues == {}
    )
    return {
        "authorization_id": args.authorization_id,
        "target": {"venue_id": TARGET_VENUE, "year": TARGET_YEAR},
        "disposition": outcome.result.disposition,
        "papers": paper_count,
        "independent_validation_issues": validation_issues,
        "artifact_lifecycle": artifact.lifecycle if artifact else None,
        "report_status": delivery.status,
        "proved_success": proved,
        "rehearsal_root_created": True,
        "source_remote_count": len(subprocess.run(
            ("git", "remote"), cwd=source, text=True,
            capture_output=True, check=True,
        ).stdout.splitlines()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--internal-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--external-root", type=Path, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--authorize-codex-live", action="store_true")
    parser.add_argument("--authorize-downloads-live", action="store_true")
    parser.add_argument("--authorize-resend-live", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    result = run(build_parser().parse_args(argv))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["proved_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
