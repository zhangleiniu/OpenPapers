"""Installed, marker-gated composition for the agent production path."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from automation.agent_production import (
    AgentProductionConfiguration,
    AgentProductionConfigurationError,
    AgentProductionEffect,
    AgentProductionSecrets,
    build_live_agent_production_effect,
    load_agent_production_configuration,
)
from automation.agent_credentials import validate_agent_credential_context
from automation.resend_notifications import recipient_fingerprints
from automation.local_service.production import (
    PRODUCTION_CONFIG,
    PRODUCTION_MARKER,
    PRODUCTION_SECRETS,
    ProductionControlError,
    ProductionMonitorEffect,
    _canonical,
    _fingerprint,
    _private_directory,
    _private_file,
    validate_production_root,
)
from automation.local_service.service import (
    LOCAL_SERVICE_LABEL,
    LocalEffectOutcome,
    LocalEffectStatus,
)
from automation.source_change_hints import (
    SourceChangeHintApplyOutcome,
    apply_pending_source_change_hints,
)


AGENT_PRODUCTION_MARKER = ".agent-production-control.v2.json"
AGENT_PRODUCTION_CONFIG = ".agent-production-config.v2.json"
AGENT_PRODUCTION_SECRETS = ".agent-production-secrets.v2.json"
_AGENT_PRODUCTION_FILES = (
    AGENT_PRODUCTION_CONFIG,
    AGENT_PRODUCTION_SECRETS,
    AGENT_PRODUCTION_MARKER,
)


@dataclass(frozen=True)
class InstalledAgentConfiguration:
    external_effects_enabled: bool
    agent_source_commit: str
    agent: AgentProductionConfiguration


def _agent_marker(config_bytes: bytes, secret_bytes: bytes, root: Path) -> bytes:
    return _canonical({
        "schema_version": 2,
        "label": LOCAL_SERVICE_LABEL,
        "mode": "agent_production_control",
        "configuration_sha256": _fingerprint(config_bytes),
        "secrets_sha256": _fingerprint(secret_bytes),
        "baseline_marker_sha256": _fingerprint(_private_file(root / PRODUCTION_MARKER)),
        "baseline_configuration_sha256": _fingerprint(
            _private_file(root / PRODUCTION_CONFIG)
        ),
        "baseline_secrets_sha256": _fingerprint(
            _private_file(root / PRODUCTION_SECRETS)
        ),
    })


def _agent_configuration(
    payload: Mapping[str, Any], *, targets_path: Path
) -> InstalledAgentConfiguration:
    if set(payload) != {
        "schema_version", "mode", "external_effects_enabled",
        "agent_source_commit", "agent_configuration",
    } or payload.get("schema_version") != 2 \
            or payload.get("mode") != "agent_production_control" \
            or not isinstance(payload.get("external_effects_enabled"), bool) \
            or not isinstance(payload.get("agent_source_commit"), str) \
            or not re.fullmatch(r"[0-9a-f]{40}", payload["agent_source_commit"]) \
            or not isinstance(payload.get("agent_configuration"), Mapping):
        raise ProductionControlError("agent production configuration is invalid")
    try:
        agent = load_agent_production_configuration(
            payload["agent_configuration"], targets_path=targets_path
        )
    except AgentProductionConfigurationError as exc:
        raise ProductionControlError(
            "agent production configuration is invalid"
        ) from exc
    return InstalledAgentConfiguration(
        payload["external_effects_enabled"], payload["agent_source_commit"], agent
    )


def _agent_secrets(payload: Mapping[str, Any]) -> AgentProductionSecrets | None:
    if set(payload) != {"schema_version", "resend"} \
            or payload.get("schema_version") not in {2, 3}:
        raise ProductionControlError("agent production secrets are invalid")
    resend = payload["resend"]
    if resend is None:
        return None
    if not isinstance(resend, Mapping) or set(resend) != {
        "api_key", "email_from", "email_to"
    }:
        raise ProductionControlError("agent production secrets are invalid")
    email_to = resend["email_to"]
    if payload["schema_version"] == 2 and not isinstance(email_to, str):
        raise ProductionControlError("agent production secrets are invalid")
    if payload["schema_version"] == 3 and not isinstance(email_to, list):
        raise ProductionControlError("agent production secrets are invalid")
    try:
        return AgentProductionSecrets(
            resend_api_key=resend["api_key"],
            email_from=resend["email_from"],
            email_to=tuple(email_to) if isinstance(email_to, list) else email_to,
        )
    except (AgentProductionConfigurationError, KeyError) as exc:
        raise ProductionControlError("agent production secrets are invalid") from exc


def _payload(path: Path, *, field: str) -> tuple[bytes, dict[str, Any]]:
    encoded = _private_file(path)
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionControlError(f"{field} is invalid") from exc
    if not isinstance(payload, dict) or encoded != _canonical(payload):
        raise ProductionControlError(f"{field} is invalid")
    return encoded, payload


def _validated_agent_file_set(
    source_root: Path,
    baseline_root: Path,
    repository_root: Path,
) -> tuple[
    InstalledAgentConfiguration,
    AgentProductionSecrets | None,
    tuple[bytes, bytes, bytes],
]:
    """Validate one exact v2 set against the current v1 baseline."""
    source = Path(source_root)
    baseline = Path(baseline_root)
    repository = Path(repository_root)
    _private_directory(source)
    config_bytes, config_payload = _payload(
        source / AGENT_PRODUCTION_CONFIG,
        field="agent production configuration",
    )
    secret_bytes, secret_payload = _payload(
        source / AGENT_PRODUCTION_SECRETS,
        field="agent production secrets",
    )
    marker_bytes, _ = _payload(
        source / AGENT_PRODUCTION_MARKER,
        field="agent production marker",
    )
    configuration = _agent_configuration(
        config_payload,
        targets_path=(repository / "automation" / "config"
                      / "agent_targets.v1.json"),
    )
    secrets = _agent_secrets(secret_payload)
    if configuration.external_effects_enabled and secrets is None:
        raise ProductionControlError("enabled agent production secrets are missing")
    if marker_bytes != _agent_marker(config_bytes, secret_bytes, baseline):
        raise ProductionControlError("agent production marker is invalid")
    return configuration, secrets, (config_bytes, secret_bytes, marker_bytes)


def validate_agent_production_root(
    internal_root: Path,
    repository_root: Path,
) -> tuple[InstalledAgentConfiguration, AgentProductionSecrets | None]:
    """Validate v1 baseline plus exact v2 agent files before mutable work."""
    root = Path(internal_root)
    repository = Path(repository_root)
    validate_production_root(root)
    configuration, secrets, _ = _validated_agent_file_set(
        root, root, repository
    )
    return configuration, secrets


def initialize_agent_production_root(
    internal_root: Path,
    repository_root: Path,
    configuration: Mapping[str, Any],
    secrets: Mapping[str, Any],
) -> tuple[Path, Path, Path]:
    """Create exact v2 files, accepting only byte-equivalent replay."""
    root = Path(internal_root)
    repository = Path(repository_root)
    _private_directory(root)
    validate_production_root(root)
    installed = _agent_configuration(
        configuration,
        targets_path=repository / "automation" / "config" / "agent_targets.v1.json",
    )
    resolved_secrets = _agent_secrets(secrets)
    if installed.external_effects_enabled and resolved_secrets is None:
        raise ProductionControlError("enabled agent production secrets are missing")
    config_bytes = _canonical(configuration)
    secret_bytes = _canonical(secrets)
    files = (
        (root / AGENT_PRODUCTION_CONFIG, config_bytes),
        (root / AGENT_PRODUCTION_SECRETS, secret_bytes),
        (root / AGENT_PRODUCTION_MARKER, _agent_marker(
            config_bytes, secret_bytes, root
        )),
    )
    for path, encoded in files:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            if _private_file(path) != encoded:
                raise ProductionControlError(
                    "agent production file conflicts with replay"
                )
            continue
        except OSError as exc:
            raise ProductionControlError(
                "agent production file creation failed"
            ) from exc
        with os.fdopen(descriptor, "wb") as file_obj:
            file_obj.write(encoded)
            file_obj.flush()
            os.fsync(file_obj.fileno())
    validate_agent_production_root(root, repository)
    return files[0][0], files[1][0], files[2][0]


def replace_disabled_agent_production_root(
    internal_root: Path,
    current_repository_root: Path,
    candidate_repository_root: Path,
    configuration: Mapping[str, Any],
    secrets: Mapping[str, Any],
    *,
    replace_file: Callable[[Path, Path], None] = os.replace,
) -> tuple[Path, Path, Path]:
    """Replace v2 files marker-last while both endpoints remain disabled.

    The caller must stop the service and retain byte-exact rollback copies.
    Any interruption before the marker replacement leaves validation closed.
    """
    root = Path(internal_root)
    current, _ = validate_agent_production_root(root, current_repository_root)
    if current.external_effects_enabled:
        raise ProductionControlError("enabled agent production cannot be refreshed")
    installed = _agent_configuration(
        configuration,
        targets_path=(Path(candidate_repository_root) / "automation" / "config"
                      / "agent_targets.v1.json"),
    )
    _agent_secrets(secrets)
    if installed.external_effects_enabled:
        raise ProductionControlError("disabled refresh cannot enable external effects")
    config_bytes = _canonical(configuration)
    secret_bytes = _canonical(secrets)
    encoded_files = (
        config_bytes,
        secret_bytes,
        _agent_marker(config_bytes, secret_bytes, root),
    )
    paths = _replace_agent_file_set(
        root, encoded_files, replace_file=replace_file
    )
    validate_agent_production_root(root, candidate_repository_root)
    return paths


def replace_enabled_agent_production_root(
    internal_root: Path,
    current_repository_root: Path,
    candidate_repository_root: Path,
    configuration: Mapping[str, Any],
    secrets: Mapping[str, Any],
    *,
    replace_file: Callable[[Path, Path], None] = os.replace,
) -> tuple[Path, Path, Path]:
    """Replace one enabled binding marker-last with exact in-process recovery.

    The host caller must stop the service and retain byte-exact filesystem and
    SQLite backups for crash recovery. This primitive never changes the
    external-effects bit and cannot activate a disabled installation.
    """
    root = Path(internal_root)
    current_repository = Path(current_repository_root)
    candidate_repository = Path(candidate_repository_root)
    current, current_secrets, before = _validated_agent_file_set(
        root, root, current_repository
    )
    if not current.external_effects_enabled or current_secrets is None:
        raise ProductionControlError(
            "enabled agent production upgrade requires enabled current state"
        )
    installed = _agent_configuration(
        configuration,
        targets_path=(candidate_repository / "automation" / "config"
                      / "agent_targets.v1.json"),
    )
    resolved_secrets = _agent_secrets(secrets)
    if not installed.external_effects_enabled or resolved_secrets is None:
        raise ProductionControlError(
            "enabled agent production upgrade requires enabled candidate state"
        )
    config_bytes = _canonical(configuration)
    secret_bytes = _canonical(secrets)
    encoded_files = (
        config_bytes,
        secret_bytes,
        _agent_marker(config_bytes, secret_bytes, root),
    )
    try:
        paths = _replace_agent_file_set(
            root, encoded_files, replace_file=replace_file
        )
        validated, validated_secrets = validate_agent_production_root(
            root, candidate_repository
        )
        if not validated.external_effects_enabled or validated_secrets is None:
            raise ProductionControlError(
                "enabled agent production upgrade did not remain enabled"
            )
    except (OSError, ProductionControlError) as exc:
        try:
            _replace_agent_file_set(root, before)
            restored, restored_secrets = validate_agent_production_root(
                root, current_repository
            )
            if not restored.external_effects_enabled or restored_secrets is None:
                raise ProductionControlError(
                    "enabled agent production upgrade recovery is disabled"
                )
        except ProductionControlError as rollback_exc:
            raise ProductionControlError(
                "enabled agent production upgrade recovery failed"
            ) from rollback_exc
        raise ProductionControlError(
            "enabled agent production upgrade failed"
        ) from exc
    return paths


def _replace_agent_file_set(
    internal_root: Path,
    encoded_files: tuple[bytes, bytes, bytes],
    *,
    replace_file: Callable[[Path, Path], None] = os.replace,
) -> tuple[Path, Path, Path]:
    """Stage an already-validated v2 set and replace its marker last."""
    root = Path(internal_root)
    targets = tuple(root / name for name in _AGENT_PRODUCTION_FILES)
    candidates: list[Path] = []
    try:
        for index, (target, encoded) in enumerate(zip(targets, encoded_files)):
            candidate = root / f".{target.name}.candidate-{os.getpid()}-{index}"
            descriptor = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            candidates.append(candidate)
            with os.fdopen(descriptor, "wb") as file_obj:
                file_obj.write(encoded)
                file_obj.flush()
                os.fsync(file_obj.fileno())
        for candidate, target in zip(candidates, targets):
            replace_file(candidate, target)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise ProductionControlError("agent production replacement failed") from exc
    finally:
        for candidate in candidates:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
    return targets


def create_agent_activation_backup(
    internal_root: Path,
    repository_root: Path,
    backup_root: Path,
) -> tuple[Path, Path, Path]:
    """Create one exact private disabled v2 backup, refusing overwrite."""
    root = Path(internal_root)
    backup = Path(backup_root)
    configuration, _, encoded_files = _validated_agent_file_set(
        root, root, Path(repository_root)
    )
    if configuration.external_effects_enabled:
        raise ProductionControlError("activation backup requires disabled state")
    _private_directory(backup.parent)
    try:
        backup.mkdir(mode=0o700)
    except OSError as exc:
        raise ProductionControlError("activation backup creation failed") from exc
    _private_directory(backup)
    paths: list[Path] = []
    try:
        for name, encoded in zip(_AGENT_PRODUCTION_FILES, encoded_files):
            path = backup / name
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            paths.append(path)
            with os.fdopen(descriptor, "wb") as file_obj:
                file_obj.write(encoded)
                file_obj.flush()
                os.fsync(file_obj.fileno())
        directory = os.open(backup, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise ProductionControlError("activation backup creation failed") from exc
    restored, _, _ = _validated_agent_file_set(
        backup, root, Path(repository_root)
    )
    if restored.external_effects_enabled:
        raise ProductionControlError("activation backup is not disabled")
    return paths[0], paths[1], paths[2]


def restore_disabled_agent_production_root(
    internal_root: Path,
    repository_root: Path,
    backup_root: Path,
    *,
    replace_file: Callable[[Path, Path], None] = os.replace,
) -> tuple[Path, Path, Path]:
    """Restore an exact disabled backup even if the current v2 set is invalid."""
    root = Path(internal_root)
    validate_production_root(root)
    configuration, _, encoded_files = _validated_agent_file_set(
        Path(backup_root), root, Path(repository_root)
    )
    if configuration.external_effects_enabled:
        raise ProductionControlError("activation rollback backup is not disabled")
    try:
        paths = _replace_agent_file_set(
            root, encoded_files, replace_file=replace_file
        )
        restored, _ = validate_agent_production_root(root, repository_root)
    except ProductionControlError as exc:
        raise ProductionControlError("agent production rollback failed") from exc
    if restored.external_effects_enabled:
        raise ProductionControlError("agent production rollback did not disable effects")
    return paths


def activate_agent_production_root(
    internal_root: Path,
    repository_root: Path,
    backup_root: Path,
    *,
    replace_file: Callable[[Path, Path], None] = os.replace,
) -> tuple[Path, Path, Path]:
    """Change only the external-effects bit, marker-last, with exact recovery."""
    root = Path(internal_root)
    repository = Path(repository_root)
    current, secrets = validate_agent_production_root(root, repository)
    if current.external_effects_enabled:
        raise ProductionControlError("agent production is already enabled")
    if secrets is None:
        raise ProductionControlError("activation requires configured secrets")
    create_agent_activation_backup(root, repository, backup_root)
    _, configuration = _payload(
        root / AGENT_PRODUCTION_CONFIG,
        field="agent production configuration",
    )
    configuration["external_effects_enabled"] = True
    config_bytes = _canonical(configuration)
    secret_bytes = _private_file(root / AGENT_PRODUCTION_SECRETS)
    encoded_files = (
        config_bytes,
        secret_bytes,
        _agent_marker(config_bytes, secret_bytes, root),
    )
    try:
        paths = _replace_agent_file_set(
            root, encoded_files, replace_file=replace_file
        )
        installed, _ = validate_agent_production_root(root, repository)
        if not installed.external_effects_enabled:
            raise ProductionControlError("activation did not enable effects")
    except (OSError, ProductionControlError) as exc:
        try:
            restore_disabled_agent_production_root(
                root, repository, backup_root
            )
        except ProductionControlError as rollback_exc:
            raise ProductionControlError(
                "agent production activation rollback failed"
            ) from rollback_exc
        raise ProductionControlError("agent production activation failed") from exc
    return paths


def rehearse_disabled_agent_activation(
    internal_root: Path,
    repository_root: Path,
    backup_root: Path,
) -> tuple[Path, Path, Path]:
    """Exercise backup/replacement/restore without ever enabling effects."""
    root = Path(internal_root)
    repository = Path(repository_root)
    current, _, before = _validated_agent_file_set(root, root, repository)
    if current.external_effects_enabled:
        raise ProductionControlError("disabled rehearsal requires disabled state")
    create_agent_activation_backup(root, repository, backup_root)
    try:
        _replace_agent_file_set(root, before)
        replayed, _ = validate_agent_production_root(root, repository)
        if replayed.external_effects_enabled:
            raise ProductionControlError("disabled rehearsal enabled effects")
    except ProductionControlError as exc:
        try:
            restore_disabled_agent_production_root(
                root, repository, backup_root
            )
        except ProductionControlError as rollback_exc:
            raise ProductionControlError(
                "disabled activation rehearsal rollback failed"
            ) from rollback_exc
        raise ProductionControlError("disabled activation rehearsal failed") from exc
    paths = restore_disabled_agent_production_root(
        root, repository, backup_root
    )
    final, _, after = _validated_agent_file_set(root, root, repository)
    if final.external_effects_enabled or after != before:
        raise ProductionControlError("disabled activation rehearsal changed state")
    return paths


def replace_disabled_agent_secrets(
    internal_root: Path,
    repository_root: Path,
    secrets: Mapping[str, Any],
) -> tuple[Path, Path, Path]:
    """Replace only disabled secrets while rebinding the marker last."""
    _, configuration = _payload(
        Path(internal_root) / AGENT_PRODUCTION_CONFIG,
        field="agent production configuration",
    )
    return replace_disabled_agent_production_root(
        internal_root, repository_root, repository_root, configuration, secrets
    )


def replace_disabled_agent_resend(
    internal_root: Path,
    repository_root: Path,
    *,
    api_key: str,
    email_from: str,
    email_to: tuple[str, ...],
) -> tuple[Path, Path, Path]:
    """Install one approved recipient allowlist without enabling effects."""
    root = Path(internal_root)
    validate_agent_production_root(root, repository_root)
    _, configuration = _payload(
        root / AGENT_PRODUCTION_CONFIG,
        field="agent production configuration",
    )
    agent = dict(configuration["agent_configuration"])
    agent.pop("resend_recipient_sha256", None)
    agent["schema_version"] = 3
    agent["resend_recipient_sha256s"] = list(recipient_fingerprints(email_to))
    configuration["agent_configuration"] = agent
    secrets = {"schema_version": 3, "resend": {
        "api_key": api_key,
        "email_from": email_from,
        "email_to": list(email_to),
    }}
    return replace_disabled_agent_production_root(
        root, repository_root, repository_root, configuration, secrets
    )


def validate_agent_source(path: Path, expected_commit: str) -> Path:
    source = Path(path).resolve()
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise ProductionControlError("agent source is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or source.is_symlink() \
            or metadata.st_uid != os.geteuid() \
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ProductionControlError("agent source is unsafe")

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ("git", *arguments), cwd=source, text=True, capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ProductionControlError("agent source Git state is invalid")
        return completed.stdout.strip()

    if Path(git("rev-parse", "--show-toplevel")).resolve() != source \
            or git("rev-parse", "HEAD") != expected_commit \
            or git("status", "--porcelain") \
            or git("remote"):
        raise ProductionControlError("agent source Git state is invalid")
    return source


class InstalledAgentProductionEffect:
    """Preserve baseline monitoring and gate all new external effects."""

    def __init__(
        self,
        *,
        repository_root: Path,
        baseline: ProductionMonitorEffect | None = None,
        live_builder: Callable[..., AgentProductionEffect] = (
            build_live_agent_production_effect
        ),
        hint_applier: Callable[..., SourceChangeHintApplyOutcome] | None = None,
    ) -> None:
        self._repository_root = Path(repository_root)
        default_baseline = baseline is None
        self._baseline = baseline or ProductionMonitorEffect(
            repository_root=self._repository_root
        )
        self._live_builder = live_builder
        self._hint_applier = (
            apply_pending_source_change_hints
            if default_baseline and hint_applier is None
            else hint_applier
        )

    def run(self, *, state_path: Path, execution_root: Path,
            scheduled_for, observed_at) -> LocalEffectOutcome:
        state = Path(state_path)
        internal_root = state.parent.parent
        configuration, secrets = validate_agent_production_root(
            internal_root, self._repository_root
        )
        baseline = self._baseline.run(
            state_path=state,
            execution_root=execution_root,
            scheduled_for=scheduled_for,
            observed_at=observed_at,
        )
        agent_source = validate_agent_source(
            Path(execution_root) / "agent-source",
            configuration.agent_source_commit,
        )
        if not configuration.external_effects_enabled:
            return baseline
        if secrets is None:  # Defensive; validation rejects this state.
            raise ProductionControlError("enabled agent production secrets are missing")
        credentials = validate_agent_credential_context(
            internal_root, require_codex_auth=True, require_google_adc=True,
        )
        agent = self._live_builder(
            repository_root=agent_source,
            configuration=configuration.agent,
            secrets=secrets,
            credentials=credentials,
        )
        outcome = agent.run(
            state_path=state,
            execution_root=execution_root,
            scheduled_for=scheduled_for,
            observed_at=observed_at,
        )
        hint_count = 0
        if self._hint_applier is not None:
            hint = self._hint_applier(
                internal_root / "monitor" / "production-wakeups.sqlite3",
                state,
                configuration.agent.targets,
                observed_at=observed_at,
                minimum_delay=configuration.agent.due_policy.minimum_retry_delay,
            )
            hint_count = hint.applied_count
        selection_count = (
            baseline.selection_count + outcome.selection_count + hint_count
        )
        return LocalEffectOutcome(
            LocalEffectStatus.COMPLETED
            if selection_count else LocalEffectStatus.NO_DUE_WORK,
            selection_count,
        )
