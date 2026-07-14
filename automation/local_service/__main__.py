"""Uninstalled P4.L3 service command; no concrete effect is configured."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from automation.local_service.service import (
    LocalMountProbe,
    LocalServiceConfig,
    LocalServiceRunStatus,
    LocalWakeupEffect,
    VolumeAvailabilityProbe,
    run_local_service_once,
)
from automation.local_service.shadow import IsolatedSchedulerShadowEffect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one bounded OpenPapers local-service preflight."
    )
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--python-executable", type=Path, required=True)
    parser.add_argument("--internal-root", type=Path, required=True)
    parser.add_argument("--external-volume-root", type=Path, required=True)
    parser.add_argument("--role-user", required=True)
    parser.add_argument("--schedule-minute", type=int, default=17)
    parser.add_argument("--record-limit", type=int, default=128)
    parser.add_argument(
        "--isolated-shadow",
        action="store_true",
        help="run only the marker-gated local due-work scheduler",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    effect: LocalWakeupEffect | None = None,
    volume_probe: VolumeAvailabilityProbe | None = None,
    clock: Callable[[], datetime] | None = None,
    platform_name: str | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    config = LocalServiceConfig(
        repository_root=args.repository_root,
        python_executable=args.python_executable,
        internal_root=args.internal_root,
        external_volume_root=args.external_volume_root,
        role_user=args.role_user,
        schedule_minute=args.schedule_minute,
        record_limit=args.record_limit,
    )
    if args.isolated_shadow and effect is not None:
        raise ValueError("an injected effect cannot replace isolated shadow mode")
    resolved_effect = (
        IsolatedSchedulerShadowEffect() if args.isolated_shadow else effect
    )
    report = run_local_service_once(
        config,
        effect=resolved_effect,
        volume_probe=volume_probe or LocalMountProbe(),
        clock=clock,
        platform_name=platform_name,
    )
    print(json.dumps(report.as_dict(), sort_keys=True))
    if report.status is LocalServiceRunStatus.COMPLETED:
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
