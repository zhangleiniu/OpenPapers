"""Operator commands for states the automation deliberately fail-closes.

The control plane is strict and one-way by design: interrupted work becomes
a durable ambiguity, and completion normally only comes from a successful
agent run. Each command here is the audited exit for one of those states,
replacing hand-rolled SQL surgery.

Every command defaults to a read-only dry run and mutates only with
``--apply``. Database work happens under the single-writer lease; file work
writes atomically, then re-validates. Output is bounded JSON and never
contains credentials, addresses, or private file contents.

Run as the dedicated service role from the installed runtime, e.g.:

    sudo -u _openpapers <installed-python> -m automation.agent_operations \
        recover-event-date --state <internal>/control/state.sqlite3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from automation.control_state import (
    ControlStateRepository,
    EventDateAttemptClaim,
)
from automation.domain import Writer
from automation.local_service.agent_control import validate_agent_production_root
from automation.local_service.production import (
    PRODUCTION_CONFIG,
    ProductionControlError,
    _canonical,
    _configuration,
    _fingerprint,
    _private_file,
    validate_production_root,
)


_LEASE_OWNER = "event-date-initializer"


class AgentOperationError(ValueError):
    """Raised when an operator command cannot proceed safely."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_private_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o600)
    tmp.replace(path)


def recover_interrupted_event_date(
    state_path: Path,
    *,
    retry_delay: timedelta = timedelta(minutes=10),
    apply: bool = False,
    clock: Callable[[], datetime] = _now,
) -> dict[str, Any]:
    """Close one ambiguously interrupted date attempt as a bounded retry.

    A process killed mid-lookup leaves its schedule/attempt pair 'active'
    forever, blocking every later wake. This rebuilds the claim from the
    stored attempt row and applies the same ``complete_event_date_retry``
    transition an expected provider failure uses. It refuses unless exactly
    one active pair exists and both rows agree; a genuinely live lookup also
    still holds the control lease, which this command must acquire.
    """
    now = clock()
    with ControlStateRepository(
        Path(state_path), writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: now
    ) as repository:
        connection = repository._connection
        schedules = connection.execute(
            "SELECT * FROM event_date_schedule WHERE status='active'"
        ).fetchall()
        attempts = connection.execute(
            "SELECT * FROM event_date_attempt WHERE outcome='active'"
        ).fetchall()
        if len(schedules) != 1 or len(attempts) != 1:
            raise AgentOperationError(
                f"expected exactly one active schedule+attempt, found "
                f"{len(schedules)}/{len(attempts)}"
            )
        schedule, attempt = dict(schedules[0]), dict(attempts[0])
        if schedule["active_attempt_id"] != attempt["attempt_id"] \
                or (schedule["venue_id"], schedule["year"]) \
                != (attempt["venue_id"], attempt["year"]):
            raise AgentOperationError("active schedule/attempt rows disagree")
        retry_at = now + retry_delay
        summary: dict[str, Any] = {
            "command": "recover-event-date",
            "venue_id": attempt["venue_id"],
            "year": attempt["year"],
            "attempt_number": attempt["attempt_number"],
            "started_at": attempt["started_at"],
            "retry_at": _utc_text(retry_at),
            "applied": apply,
        }
        if not apply:
            return summary
        claim = EventDateAttemptClaim(
            attempt_id=attempt["attempt_id"],
            venue_id=attempt["venue_id"],
            year=attempt["year"],
            attempt_number=attempt["attempt_number"],
            started_at=attempt["started_at"],
            provider_name=attempt["provider_name"],
            provider_model=attempt["provider_model"],
            prompt_version=attempt["prompt_version"],
        )
        lease = repository.acquire_lease(_LEASE_OWNER)
        try:
            record = repository.complete_event_date_retry(
                claim,
                failure_category="operator_interrupted",
                completed_at=now,
                retry_at=retry_at,
                lease=lease,
            )
        finally:
            repository.release_lease(lease)
        summary["status"] = record.status
        return summary


def mark_schedule_completed(
    state_path: Path,
    venue_id: str,
    year: int,
    *,
    event_date: str | None = None,
    apply: bool = False,
    chain_next_year_interval: int | None = None,
    clock: Callable[[], datetime] = _now,
) -> dict[str, Any]:
    """Mark one venue/year completed because its canonical scrape exists.

    The automation only learns "done" from a successful agent run; a venue
    scraped manually before enrollment would otherwise get a full, wasteful
    re-scrape. Two lifecycle shapes are handled: a target with an agent
    schedule flips to 'completed' (run history and last_disposition remain
    untouched); a target still stuck in date lookups additionally needs
    ``event_date`` (the approximate first day, operator-attested) so its
    date stage can be closed with explicit ``provider='operator'``
    provenance and a completed agent schedule inserted. Live rows refuse.

    ``chain_next_year_interval``, when given, also registers
    ``(venue_id, year + chain_next_year_interval)`` for date discovery under
    the same lease right after completion — the operator's equivalent of
    the successor chaining an automatic ``success`` disposition triggers
    (see ``agent_production.AgentProductionEffect._chain_successor``). Pass
    the venue's real cadence (1 for an annual venue, 2 for ICCV/ECCV); leave
    it ``None`` for a venue with no reliable interval (e.g. NAACL) so no
    successor is guessed.
    """
    if event_date is not None:
        canonical = date.fromisoformat(event_date).isoformat()
        if canonical != event_date:
            raise AgentOperationError("event date must be a canonical ISO date")
    if chain_next_year_interval is not None and (
        not isinstance(chain_next_year_interval, int)
        or isinstance(chain_next_year_interval, bool)
        or chain_next_year_interval < 1
    ):
        raise AgentOperationError(
            "chain_next_year_interval must be a positive integer"
        )
    now = clock()
    now_text = _utc_text(now)
    with ControlStateRepository(
        Path(state_path), writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: now
    ) as repository:
        connection = repository._connection
        event = connection.execute(
            "SELECT status FROM event_date_schedule WHERE venue_id=? AND year=?",
            (venue_id, year),
        ).fetchone()
        agent = connection.execute(
            "SELECT status FROM agent_schedule WHERE venue_id=? AND year=?",
            (venue_id, year),
        ).fetchone()
        if event is None:
            raise AgentOperationError(
                f"{venue_id}/{year} is not a registered target"
            )
        if agent is not None and agent["status"] == "active":
            raise AgentOperationError(f"{venue_id}/{year} has a live agent run")
        if event["status"] == "active":
            raise AgentOperationError(f"{venue_id}/{year} has a live date lookup")
        if agent is not None and agent["status"] == "completed":
            return {
                "command": "mark-completed", "venue_id": venue_id,
                "year": year, "applied": False, "already": "completed",
            }
        needs_terminalize = agent is None
        if needs_terminalize and event_date is None:
            raise AgentOperationError(
                f"{venue_id}/{year} has no agent schedule yet; pass "
                "--event-date with the conference's approximate first day"
            )
        summary: dict[str, Any] = {
            "command": "mark-completed",
            "venue_id": venue_id,
            "year": year,
            "shape": "terminalize_date_stage" if needs_terminalize
            else "complete_agent_schedule",
            "applied": apply,
        }
        if chain_next_year_interval is not None:
            summary["chain_successor_year"] = year + chain_next_year_interval
        if not apply:
            return summary
        lease = repository.acquire_lease(_LEASE_OWNER)
        try:
            with repository._write_transaction() as tx:
                if needs_terminalize:
                    cursor = tx.execute(
                        "UPDATE event_date_schedule SET status='scheduled', "
                        "estimated_event_date=?, estimated_at=?, "
                        "provider_name='operator', provider_model='operator', "
                        "prompt_version='operator', active_attempt_id=NULL, "
                        "last_failure_category=NULL, updated_at=? "
                        "WHERE venue_id=? AND year=? AND status='pending'",
                        (event_date, now_text, now_text, venue_id, year),
                    )
                    if cursor.rowcount != 1:
                        raise AgentOperationError(
                            f"{venue_id}/{year} changed state mid-flight"
                        )
                    tx.execute(
                        "INSERT INTO agent_schedule (venue_id, year, status, "
                        "next_check_at, attempt_count, active_run_id, "
                        "consecutive_failures, last_disposition, last_run_at, "
                        "suggested_retry_at, last_gate_reason, updated_at) "
                        "VALUES (?, ?, 'completed', NULL, 0, NULL, 0, NULL, "
                        "NULL, NULL, NULL, ?)",
                        (venue_id, year, now_text),
                    )
                else:
                    cursor = tx.execute(
                        "UPDATE agent_schedule SET status='completed', "
                        "next_check_at=NULL, active_run_id=NULL, "
                        "last_gate_reason=NULL, updated_at=? "
                        "WHERE venue_id=? AND year=? "
                        "AND status NOT IN ('active','completed')",
                        (now_text, venue_id, year),
                    )
                    if cursor.rowcount != 1:
                        raise AgentOperationError(
                            f"{venue_id}/{year} changed state mid-flight"
                        )
            # A separate transaction, still under the same lease: SQLite has
            # no nested transactions, so this cannot join the completion
            # write above atomically. If the process dies in between, the
            # completion is already durable (a rerun reports
            # already="completed") and the successor is simply not yet
            # registered — recoverable by rerunning this command or by the
            # calendar-rollover safety net in agent_production.py.
            if chain_next_year_interval is not None:
                repository.register_event_date_target(
                    venue_id, year + chain_next_year_interval,
                    registered_at=now, lease=lease,
                )
        finally:
            repository.release_lease(lease)
        # Re-read through the validating reader so a constraint this command
        # violated would surface here rather than in the next real wake.
        record = repository.get_agent_schedule(venue_id, year)
        assert record is not None
        summary["status"] = record.status
        return summary


def update_monitor_configuration(
    internal_root: Path,
    repository_root: Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Update registry_sha256/expected_source_count to match the deployed
    registry.

    The private monitor configuration pins both the exact bytes and the
    total source count of ``automation/conferences.json``. Run this whenever
    a deployed runtime changes the registry.
    """
    internal_root = Path(internal_root)
    repository_root = Path(repository_root)
    registry_path = repository_root / "automation" / "conferences.json"
    validate_production_root(internal_root)
    registry_bytes = registry_path.read_bytes()
    from automation.monitor import load_registry  # deferred: imports core config

    count = sum(len(entry["sources"]) for entry in load_registry(registry_path))
    config_bytes = _private_file(internal_root / PRODUCTION_CONFIG)
    payload = asdict(_configuration(json.loads(config_bytes)))
    payload["schema_version"] = 1
    before = {
        "registry_sha256": payload["registry_sha256"],
        "expected_source_count": payload["expected_source_count"],
    }
    payload["registry_sha256"] = _fingerprint(registry_bytes)
    payload["expected_source_count"] = count
    summary: dict[str, Any] = {
        "command": "update-monitor-config",
        "before": before,
        "after": {
            "registry_sha256": payload["registry_sha256"],
            "expected_source_count": count,
        },
        "changed": before["registry_sha256"] != payload["registry_sha256"]
        or before["expected_source_count"] != count,
        "applied": apply,
    }
    if not apply or not summary["changed"]:
        return summary
    _atomic_private_write(
        internal_root / PRODUCTION_CONFIG, _canonical(payload)
    )
    validate_agent_production_root(internal_root, repository_root)
    summary["validated"] = True
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    recover = commands.add_parser("recover-event-date")
    recover.add_argument("--state", required=True, type=Path)
    recover.add_argument("--retry-minutes", default=10, type=int)
    recover.add_argument("--apply", action="store_true")

    completed = commands.add_parser("mark-completed")
    completed.add_argument("--state", required=True, type=Path)
    completed.add_argument("--venue", required=True)
    completed.add_argument("--year", required=True, type=int)
    completed.add_argument("--event-date")
    completed.add_argument(
        "--chain-next-year-interval", type=int, default=None,
        help="also register venue/(year + N) for date discovery, e.g. 1 "
        "for an annual venue or 2 for ICCV/ECCV; omit for a venue with no "
        "reliable interval (e.g. NAACL)",
    )
    completed.add_argument("--apply", action="store_true")

    default_repository = Path(__file__).resolve().parents[1]
    update_config = commands.add_parser("update-monitor-config")
    update_config.add_argument("--internal-root", required=True, type=Path)
    update_config.add_argument(
        "--repository-root", default=default_repository, type=Path
    )
    update_config.add_argument("--apply", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "recover-event-date":
            summary = recover_interrupted_event_date(
                args.state,
                retry_delay=timedelta(minutes=args.retry_minutes),
                apply=args.apply,
            )
        elif args.command == "mark-completed":
            summary = mark_schedule_completed(
                args.state, args.venue, args.year,
                event_date=args.event_date, apply=args.apply,
                chain_next_year_interval=args.chain_next_year_interval,
            )
        else:
            os.chdir(args.repository_root)
            summary = update_monitor_configuration(
                args.internal_root, args.repository_root, apply=args.apply
            )
    except (AgentOperationError, ProductionControlError, ValueError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
