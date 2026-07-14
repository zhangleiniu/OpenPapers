"""Pure system LaunchDaemon rendering and rollback scope for P4.L3."""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

from automation.local_service.service import LOCAL_SERVICE_LABEL, LocalServiceConfig


LAUNCHDAEMON_ROOT = Path("/Library/LaunchDaemons")


@dataclass(frozen=True)
class LaunchDaemonRollbackScope:
    label: str
    domain_target: str
    plist_path: Path
    removable_paths: tuple[Path, ...]
    preserved_paths: tuple[Path, ...]

    def matches_label(self, label: str) -> bool:
        return label == self.label

    def may_remove(self, path: Path) -> bool:
        return path in self.removable_paths


def _program_arguments(config: LocalServiceConfig) -> list[str]:
    return [
        str(config.python_executable),
        "-m",
        "automation.local_service",
        "--repository-root",
        str(config.repository_root),
        "--python-executable",
        str(config.python_executable),
        "--internal-root",
        str(config.internal_root),
        "--external-volume-root",
        str(config.external_volume_root),
        "--role-user",
        config.role_user,
        "--schedule-minute",
        str(config.schedule_minute),
        "--record-limit",
        str(config.record_limit),
    ]


def render_launchdaemon(config: LocalServiceConfig) -> bytes:
    """Return a deterministic credential-free plist without writing it."""
    if not isinstance(config, LocalServiceConfig):
        raise TypeError("config must be LocalServiceConfig")
    document = {
        "Label": LOCAL_SERVICE_LABEL,
        "ProgramArguments": _program_arguments(config),
        "WorkingDirectory": str(config.repository_root),
        "UserName": config.role_user,
        "RunAtLoad": True,
        "StartCalendarInterval": {"Minute": config.schedule_minute},
        "ThrottleInterval": 60,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "Umask": 0o77,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)


def render_isolated_shadow_launchdaemon(config: LocalServiceConfig) -> bytes:
    """Render the fixed P4.LS service with only the isolated scheduler effect."""
    document = plistlib.loads(render_launchdaemon(config))
    document["ProgramArguments"].append("--isolated-shadow")
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)


def render_production_launchdaemon(config: LocalServiceConfig) -> bytes:
    """Render the fixed P4.LC service without embedding configuration/secrets."""
    document = plistlib.loads(render_launchdaemon(config))
    document["ProgramArguments"].append("--production-control")
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)


def build_rollback_scope(config: LocalServiceConfig) -> LaunchDaemonRollbackScope:
    """Describe the only service/file targets a future rollback may remove."""
    if not isinstance(config, LocalServiceConfig):
        raise TypeError("config must be LocalServiceConfig")
    plist_path = LAUNCHDAEMON_ROOT / f"{LOCAL_SERVICE_LABEL}.plist"
    return LaunchDaemonRollbackScope(
        label=LOCAL_SERVICE_LABEL,
        domain_target=f"system/{LOCAL_SERVICE_LABEL}",
        plist_path=plist_path,
        removable_paths=(plist_path,),
        preserved_paths=(
            config.internal_root,
            config.repository_root,
            config.external_volume_root,
        ),
    )
