"""Local-only P4.2 worker health command; it starts no worker or subprocess."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from automation.mac_worker.health import WorkerHealthConfig, collect_worker_health


class _MissingPrefectProbe:
    def is_configured(self, *, work_pool_name: str) -> bool:
        return False


def _local_prefect_probe():
    try:
        from automation.mac_worker.prefect_support import LocalPrefectSettingsProbe
    except ModuleNotFoundError as exc:
        if exc.name != "prefect":
            raise
        return _MissingPrefectProbe()
    return LocalPrefectSettingsProbe()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check local OpenPapers Mac worker prerequisites."
    )
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--codex-auth-path", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = WorkerHealthConfig(
        repository_root=args.repository_root.expanduser().resolve(),
        data_root=args.data_root.expanduser().resolve(),
        # Keep the final path component unresolved so the metadata check can
        # reject a symlink instead of silently inspecting its target.
        codex_auth_path=args.codex_auth_path.expanduser().absolute(),
    )
    report = collect_worker_health(config, _local_prefect_probe())
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
