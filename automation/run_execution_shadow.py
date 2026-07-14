"""Explicit manual CLI for one P5.S existing-scraper shadow execution."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from automation.execution_pipeline import run_existing_scraper_pipeline
from automation.execution_shadow import (
    ExecutionShadowConfig,
    ExecutionShadowError,
    LocalImmutableResultStore,
    SandboxedSubprocessLauncher,
    build_pipeline_config,
    build_shadow_job,
    prepare_shadow_root,
    retain_sandbox_profile,
)


_SERVICE_LABEL = "system/org.openpapers.local-control"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one explicit canonical-isolated existing-scraper shadow."
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--canonical-data-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--python-executable", type=Path, required=True)
    parser.add_argument("--sandbox-executable", type=Path, default=Path("/usr/bin/sandbox-exec"))
    parser.add_argument("--venue", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--cancellation-grace-seconds", type=float, default=30.0)
    return parser


def _normalized(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _service_loaded() -> bool:
    try:
        completed = subprocess.run(
            ("/bin/launchctl", "print", _SERVICE_LABEL),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.live:
        print("refusing P5.S execution without explicit --live", file=sys.stderr)
        return 2
    if sys.platform != "darwin":
        print("P5.S sandbox execution requires macOS", file=sys.stderr)
        return 2
    if not _service_loaded():
        print("P5.S production coexistence gate is not healthy", file=sys.stderr)
        return 2
    if args.expected_count < 1:
        print("P5.S expected count must be positive", file=sys.stderr)
        return 2
    config = ExecutionShadowConfig(
        repository_root=_normalized(args.repository_root),
        python_executable=_normalized(args.python_executable),
        canonical_data_root=_normalized(args.canonical_data_root),
        shadow_root=_normalized(args.shadow_root),
        timeout_seconds=args.timeout_seconds,
        cancellation_grace_seconds=args.cancellation_grace_seconds,
    )
    try:
        prepare_shadow_root(
            config,
            venue_id=args.venue,
            year=args.year,
            expected_count=args.expected_count,
        )
        profile = retain_sandbox_profile(config)
        launcher = SandboxedSubprocessLauncher(
            profile,
            sandbox_executable=_normalized(args.sandbox_executable),
        )
        publisher = LocalImmutableResultStore(config.shadow_root / "results")
        job = build_shadow_job(
            venue_id=args.venue,
            year=args.year,
            expected_count=args.expected_count,
        )
        observation = run_existing_scraper_pipeline(
            job,
            build_pipeline_config(config),
            launcher,
            publisher,
        )
    except (ExecutionShadowError, ValueError, TypeError) as exc:
        print(f"P5.S refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(observation.as_dict(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
