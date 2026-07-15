"""Explicit P2.8S live-canary command; never scheduled or deployed."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

from automation.production_wakeup_canary import CanaryRootError, run_canary
from automation.providers.gemini import DEFAULT_LOCATION, DEFAULT_MODEL


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the exact P2.8 automatic discovery/verification/action "
            "composition once, live, against one preselected archival "
            "venue/year inside a private marked root. This command is "
            "manual, uninstalled, and never dispatches a retained job."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly permit metered Gemini calls and live HTTPS fetches",
    )
    parser.add_argument(
        "--canary-root",
        type=Path,
        required=True,
        help="private, fresh-or-marked root disjoint from any production path",
    )
    parser.add_argument(
        "--gemini-project",
        default=None,
        help="GCP project (default: GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT env)",
    )
    parser.add_argument("--gemini-location", default=DEFAULT_LOCATION)
    parser.add_argument("--gemini-model", default=DEFAULT_MODEL)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("P2.8S live canary requires explicit --live")
    load_dotenv()
    project = (
        args.gemini_project
        or os.getenv("GCP_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
    )
    if not project:
        parser.error(
            "--gemini-project (or GCP_PROJECT_ID/GOOGLE_CLOUD_PROJECT) is required"
        )

    try:
        outcome = run_canary(
            args.canary_root,
            gemini_project=project,
            gemini_location=args.gemini_location,
            gemini_model=args.gemini_model,
        )
    except CanaryRootError as exc:
        parser.exit(2, f"P2.8S canary refused: {exc}\n")

    compact = {
        "replayed": outcome.replayed,
        "outcome": outcome.outcome,
        "refusal_category": outcome.refusal_category,
        "selection_count": outcome.selection_count,
        "verification_ids": list(outcome.verification_ids),
        "retained_jobs": list(outcome.retained_jobs),
    }
    print(json.dumps(compact, sort_keys=True))
    if outcome.outcome == "refused":
        return 2
    if outcome.outcome in ("no_action", "replayed"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
