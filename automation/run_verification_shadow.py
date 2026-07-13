"""Explicit P2.S live-shadow command; never scheduled or deployed."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from automation.configuration import load_venue_catalog
from automation.live_fetch import LiveHttpFetcher
from automation.verification_shadow import (
    SHADOW_POLICY_PATH,
    ShadowVerificationError,
    run_shadow_review,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run isolated deterministic verification in P2.S shadow mode."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly permit bounded public HTTPS observations",
    )
    parser.add_argument("--discovery-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=SHADOW_POLICY_PATH)
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument(
        "--venue",
        action="append",
        dest="venues",
        help="catalog venue to sample; repeat, or omit for all catalog venues",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher_factory=LiveHttpFetcher,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("live shadow verification requires explicit --live")
    catalog = load_venue_catalog()
    venue_ids = args.venues or [
        item["venue_id"] for item in catalog["venues"]
    ]
    try:
        summary = run_shadow_review(
            discovery_root=args.discovery_root,
            output_root=args.output_root,
            venue_ids=venue_ids,
            year=args.year,
            fetcher=fetcher_factory(),
            observed_at=datetime.now(timezone.utc),
            policy_path=args.policy,
        )
    except ShadowVerificationError as exc:
        parser.exit(2, f"shadow verification refused: {exc}\n")
    compact = {
        "shadow_only": summary["shadow_only"],
        "observed_at": summary["observed_at"],
        "year": summary["year"],
        "venue_count": summary["venue_count"],
        "output_root": str(args.output_root.expanduser().resolve()),
        "effects": summary["effects"],
    }
    print(json.dumps(compact, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
