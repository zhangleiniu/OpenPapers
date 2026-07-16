"""Audit and explicitly control installed agent external effects."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from automation.agent_credentials import (
    AgentCredentialError,
    validate_agent_credential_context,
)
from automation.control_state import CONTROL_SCHEMA_VERSION
from automation.control_state_migration import (
    ControlStateMigrationError,
    audit_control_state,
)
from automation.domain import Writer
from automation.local_service.agent_control import (
    activate_agent_production_root,
    rehearse_disabled_agent_activation,
    restore_disabled_agent_production_root,
    validate_agent_production_root,
    validate_agent_source,
)
from automation.local_service.production import ProductionControlError
from automation.local_service.service import LOCAL_SERVICE_LABEL
from automation.resend_notifications import recipient_fingerprints


_CLOUD_PROOF_FIELDS = {
    "schema_version",
    "cloud_schedule_paused",
    "active_cloud_executions",
    "checked_at",
}
_CLOUD_PROOF_MAX_AGE_SECONDS = 15 * 60
_CLOUD_PROOF_MAX_FUTURE_SKEW_SECONDS = 60
_LAUNCHCTL = Path("/bin/launchctl")


class AgentActivationError(ValueError):
    """Raised when activation authority or readiness cannot be proven."""


@dataclass(frozen=True)
class CloudDrainProof:
    schedule_paused: bool
    active_executions: int
    checked_at: datetime


@dataclass(frozen=True)
class ActivationReadiness:
    ready: bool
    external_effects_enabled: bool
    schema_version: int
    state_quick_check_ok: bool
    active_event_date_attempts: int
    active_agent_runs: int
    in_flight_reports: int
    codex_auth_present: bool
    google_adc_present: bool
    recipient_count: int
    disk_ready: bool
    agent_source_ready: bool
    cloud_schedule_paused: bool
    active_cloud_executions: int
    service_loaded: bool


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AgentActivationError(f"{field} is invalid")
    return value.astimezone(timezone.utc)


def read_cloud_drain_proof(
    path: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CloudDrainProof:
    """Validate one short-lived, address-free paused/drained cloud proof."""
    proof_path = Path(path)
    try:
        metadata = proof_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or proof_path.is_symlink() \
                or metadata.st_uid != os.geteuid() \
                or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO) \
                or not 2 <= metadata.st_size <= 4096:
            raise AgentActivationError("cloud proof is unsafe")
        payload = json.loads(proof_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentActivationError("cloud proof is unavailable") from exc
    if not isinstance(payload, dict) or set(payload) != _CLOUD_PROOF_FIELDS \
            or type(payload.get("schema_version")) is not int \
            or payload.get("schema_version") != 1 \
            or payload.get("cloud_schedule_paused") is not True \
            or type(payload.get("active_cloud_executions")) is not int \
            or payload.get("active_cloud_executions") != 0 \
            or not isinstance(payload.get("checked_at"), str):
        raise AgentActivationError("cloud proof is invalid")
    try:
        checked_at = datetime.fromisoformat(
            payload["checked_at"].replace("Z", "+00:00")
        )
        checked_at = _utc(checked_at, field="cloud proof timestamp")
        observed_at = _utc(clock(), field="clock")
    except (ValueError, AgentActivationError) as exc:
        raise AgentActivationError("cloud proof is invalid") from exc
    age = (observed_at - checked_at).total_seconds()
    if not -_CLOUD_PROOF_MAX_FUTURE_SKEW_SECONDS <= age \
            <= _CLOUD_PROOF_MAX_AGE_SECONDS:
        raise AgentActivationError("cloud proof is stale")
    return CloudDrainProof(True, 0, checked_at)


def probe_local_service_loaded(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Return whether the one fixed system LaunchDaemon is currently loaded."""
    if not _LAUNCHCTL.is_file() or not os.access(_LAUNCHCTL, os.X_OK):
        raise AgentActivationError("service status probe is unavailable")
    service_target = f"system/{LOCAL_SERVICE_LABEL}"
    try:
        completed = runner(
            (str(_LAUNCHCTL), "print", service_target),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentActivationError("service status probe failed") from exc
    if completed.returncode == 0:
        return True
    if completed.returncode == 113 and (
        f'Could not find service "{LOCAL_SERVICE_LABEL}" in domain for system'
        in completed.stderr
    ):
        return False
    raise AgentActivationError("service status probe failed")


def audit_external_effects_readiness(
    *,
    internal_root: Path,
    repository_root: Path,
    execution_root: Path,
    state_path: Path,
    cloud_proof_path: Path,
    service_loaded: bool,
    expected_service_loaded: bool,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
) -> ActivationReadiness:
    """Prove bounded local and cloud prerequisites without changing state."""
    if not isinstance(service_loaded, bool) \
            or not isinstance(expected_service_loaded, bool):
        raise AgentActivationError("local service state is invalid")
    if service_loaded is not expected_service_loaded:
        raise AgentActivationError("local service state does not match expectation")
    repository = Path(repository_root).resolve()
    execution = Path(execution_root).resolve()
    if repository == execution or repository.is_relative_to(execution) \
            or execution.is_relative_to(repository):
        raise AgentActivationError("agent execution root is not isolated")
    try:
        configuration, secrets = validate_agent_production_root(
            Path(internal_root), repository
        )
    except ProductionControlError as exc:
        raise AgentActivationError("agent production configuration is not ready") from exc
    if configuration.external_effects_enabled:
        raise AgentActivationError("agent production is already enabled")
    if secrets is None:
        raise AgentActivationError("Resend configuration is missing")
    if recipient_fingerprints(secrets.email_to) \
            != configuration.agent.resend_recipient_sha256s:
        raise AgentActivationError("Resend recipient allowlist is invalid")
    try:
        credentials = validate_agent_credential_context(
            Path(internal_root),
            require_codex_auth=True,
            require_google_adc=True,
        )
    except AgentCredentialError as exc:
        raise AgentActivationError("agent credentials are not ready") from exc
    try:
        state = audit_control_state(Path(state_path))
    except ControlStateMigrationError as exc:
        raise AgentActivationError("control state is not ready") from exc
    if not (
        state.schema_version == CONTROL_SCHEMA_VERSION
        and state.current_schema_version == CONTROL_SCHEMA_VERSION
        and state.quick_check_ok
        and state.owner_kind == Writer.LOCAL_CONTROL_PLANE.value
        and state.journal_mode != "wal"
        and state.active_event_date_attempts == 0
        and state.active_agent_runs == 0
        and state.in_flight_reports == 0
        and state.migration_ready
    ):
        raise AgentActivationError("control state is not ready")
    try:
        validate_agent_source(
            execution / "agent-source",
            configuration.agent_source_commit,
        )
    except ProductionControlError as exc:
        raise AgentActivationError("agent source is not ready") from exc
    try:
        free_bytes = int(getattr(disk_usage(execution), "free"))
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise AgentActivationError("execution-volume free space is unavailable") from exc
    if free_bytes < configuration.agent.minimum_free_bytes:
        raise AgentActivationError("execution-volume free space is insufficient")
    cloud = read_cloud_drain_proof(Path(cloud_proof_path), clock=clock)
    return ActivationReadiness(
        ready=True,
        external_effects_enabled=False,
        schema_version=state.schema_version,
        state_quick_check_ok=state.quick_check_ok,
        active_event_date_attempts=state.active_event_date_attempts,
        active_agent_runs=state.active_agent_runs,
        in_flight_reports=state.in_flight_reports,
        codex_auth_present=(credentials.codex_home / "auth.json").is_file(),
        google_adc_present=credentials.google_adc.is_file(),
        recipient_count=len(secrets.email_to),
        disk_ready=True,
        agent_source_ready=True,
        cloud_schedule_paused=cloud.schedule_paused,
        active_cloud_executions=cloud.active_executions,
        service_loaded=service_loaded,
    )


def _readiness_payload(result: ActivationReadiness) -> dict[str, object]:
    return {
        "ready": result.ready,
        "external_effects_enabled": result.external_effects_enabled,
        "schema_version": result.schema_version,
        "state_quick_check_ok": result.state_quick_check_ok,
        "active_event_date_attempts": result.active_event_date_attempts,
        "active_agent_runs": result.active_agent_runs,
        "in_flight_reports": result.in_flight_reports,
        "codex_auth_present": result.codex_auth_present,
        "google_adc_present": result.google_adc_present,
        "recipient_count": result.recipient_count,
        "disk_ready": result.disk_ready,
        "agent_source_ready": result.agent_source_ready,
        "cloud_schedule_paused": result.cloud_schedule_paused,
        "active_cloud_executions": result.active_cloud_executions,
        "service_loaded": result.service_loaded,
    }


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--internal-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--execution-root", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--cloud-proof", type=Path, required=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Run a safe audit or one separately authorized file transition."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    audit_parser = commands.add_parser("audit")
    _common_arguments(audit_parser)
    audit_parser.add_argument("--expect-service-stopped", action="store_true")
    rehearsal_parser = commands.add_parser("rehearse-disabled")
    _common_arguments(rehearsal_parser)
    rehearsal_parser.add_argument("--backup-root", type=Path, required=True)
    rehearsal_parser.add_argument("--confirm-service-stopped", action="store_true")
    rehearsal_parser.add_argument("--authorize-disabled-rehearsal", action="store_true")
    activation_parser = commands.add_parser("activate")
    _common_arguments(activation_parser)
    activation_parser.add_argument("--backup-root", type=Path, required=True)
    activation_parser.add_argument("--confirm-service-stopped", action="store_true")
    activation_parser.add_argument(
        "--authorize-external-effects-activation", action="store_true"
    )
    rollback_parser = commands.add_parser("rollback")
    rollback_parser.add_argument("--internal-root", type=Path, required=True)
    rollback_parser.add_argument("--repository-root", type=Path, required=True)
    rollback_parser.add_argument("--backup-root", type=Path, required=True)
    rollback_parser.add_argument("--confirm-service-stopped", action="store_true")
    rollback_parser.add_argument(
        "--authorize-external-effects-rollback", action="store_true"
    )
    args = parser.parse_args(argv)
    if args.command == "activate" and not (
        args.confirm_service_stopped
        and args.authorize_external_effects_activation
    ):
        print(json.dumps({"status": "blocked", "reason": "activation_unauthorized"}))
        return 2
    if args.command == "rehearse-disabled" and not (
        args.confirm_service_stopped and args.authorize_disabled_rehearsal
    ):
        print(json.dumps({"status": "blocked", "reason": "rehearsal_unauthorized"}))
        return 2
    if args.command == "rollback" and not (
        args.confirm_service_stopped and args.authorize_external_effects_rollback
    ):
        print(json.dumps({"status": "blocked", "reason": "rollback_unauthorized"}))
        return 2
    try:
        loaded = probe_local_service_loaded()
        if args.command == "rollback":
            if loaded:
                raise AgentActivationError("rollback requires stopped service")
            restore_disabled_agent_production_root(
                args.internal_root, args.repository_root, args.backup_root
            )
            payload = {
                "status": "completed",
                "operation": "rollback",
                "external_effects_enabled": False,
                "backup_retained": True,
            }
        else:
            expected_loaded = not (
                args.command in {"activate", "rehearse-disabled"}
                or args.expect_service_stopped
            )
            readiness = audit_external_effects_readiness(
                internal_root=args.internal_root,
                repository_root=args.repository_root,
                execution_root=args.execution_root,
                state_path=args.state,
                cloud_proof_path=args.cloud_proof,
                service_loaded=loaded,
                expected_service_loaded=expected_loaded,
            )
            if args.command == "audit":
                payload = {"status": "ok", "readiness": _readiness_payload(readiness)}
            elif args.command == "rehearse-disabled":
                rehearse_disabled_agent_activation(
                    args.internal_root, args.repository_root, args.backup_root
                )
                payload = {
                    "status": "completed",
                    "operation": "disabled_rehearsal",
                    "external_effects_enabled": False,
                    "backup_retained": True,
                }
            else:
                activate_agent_production_root(
                    args.internal_root, args.repository_root, args.backup_root
                )
                payload = {
                    "status": "completed",
                    "operation": "activation",
                    "external_effects_enabled": True,
                    "backup_retained": True,
                }
    except (AgentActivationError, ProductionControlError, PermissionError):
        print(json.dumps({"status": "blocked", "reason": "readiness_failed"}))
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
