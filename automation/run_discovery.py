"""Explicit, shadow-only command for Phase 1 grounded discovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Callable, Sequence, TextIO

from automation.configuration import load_venue_catalog
from automation.discovery import (
    ArtifactStore,
    DiscoveryError,
    DiscoveryProvider,
    DiscoveryService,
    request_from_catalog,
    safe_error_summary,
)


REGISTRY_PATH = Path(__file__).with_name("conferences.json")


def _default_artifact_root() -> Path:
    data_root = os.getenv("SCRAPER_DATA_ROOT")
    if data_root:
        return Path(data_root) / "automation" / "discovery"
    return Path("data") / "automation" / "discovery"


def _shadow_cohort(path: Path = REGISTRY_PATH) -> dict[str, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryError(f"cannot load shadow registry: {exc}") from exc
    if payload.get("version") != 1 or not isinstance(
            payload.get("conference_years"), list):
        raise DiscoveryError("shadow registry must use version 1")
    cohort: dict[str, int] = {}
    for entry in payload["conference_years"]:
        if not isinstance(entry, dict):
            raise DiscoveryError("shadow registry entry must be an object")
        venue = entry.get("venue")
        year = entry.get("year")
        if not isinstance(venue, str) or not isinstance(year, int):
            raise DiscoveryError("shadow registry venue/year is invalid")
        if venue in cohort and cohort[venue] != year:
            raise DiscoveryError(f"shadow registry has duplicate venue: {venue}")
        cohort[venue] = year
    return cohort


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run citation-bearing LLM discovery in shadow mode. This command "
            "does not update lifecycle state, schedule work, or call a scraper."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly permit unmetered development Gemini calls",
    )
    parser.add_argument(
        "--venue",
        action="append",
        help=("catalog venue ID; repeat for multiple "
              "(default: current shadow cohort)"),
    )
    parser.add_argument(
        "--year",
        type=int,
        help="override the registry year for every selected shadow venue",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=None,
        help="local root for immutable development evidence and cache",
    )
    parser.add_argument(
        "--cache-hours",
        type=float,
        default=24.0,
        help="reuse an unchanged request for this many hours (default: 24)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore a fresh cache entry",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=0,
        help="non-negative transient retry count for this manual run",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    provider_factory: Callable[[], DiscoveryProvider] | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Run selected shadow observations and return an operator-friendly code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.live:
        stderr.write(
            "Refusing remote discovery without --live; no provider was called.\n")
        return 2
    if args.cache_hours < 0:
        stderr.write("--cache-hours cannot be negative.\n")
        return 2
    if args.max_retries < 0:
        stderr.write("--max-retries cannot be negative.\n")
        return 2
    if args.year is not None and not 1900 <= args.year <= 2200:
        stderr.write("--year must be between 1900 and 2200.\n")
        return 2

    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=Path(".env"))
        cohort = _shadow_cohort()
        venues = list(dict.fromkeys(args.venue or cohort.keys()))
        catalog = load_venue_catalog()
        catalog_venues = {
            venue["venue_id"] for venue in catalog["venues"]
        }
        unknown = sorted(set(venues) - catalog_venues)
        if unknown:
            stderr.write(
                "Venue is absent from the automation catalog: "
                f"{', '.join(unknown)}.\n")
            return 2
        missing_year = [
            venue for venue in venues
            if args.year is None and venue not in cohort
        ]
        if missing_year:
            stderr.write(
                "--year is required outside the default shadow cohort: "
                f"{', '.join(missing_year)}.\n")
            return 2
        artifact_root = args.artifact_root or _default_artifact_root()

        if provider_factory is None:
            from automation.providers.gemini import (
                GeminiSearchGroundingProvider,
            )

            provider = GeminiSearchGroundingProvider.from_environment()
        else:
            provider = provider_factory()

        service = DiscoveryService(
            provider,
            ArtifactStore(artifact_root),
            None,
            None,
            cache_max_age=timedelta(hours=args.cache_hours),
            max_retries=args.max_retries,
        )
        failures = 0
        stdout.write(
            "Unmetered manual development discovery; no canonical ledger, "
            "state, schedule, job, or scraper writes.\n")
        try:
            for venue_id in venues:
                year = (
                    args.year if args.year is not None else cohort[venue_id]
                )
                request = request_from_catalog(catalog, venue_id, year)
                try:
                    outcome = service.discover(request, force=args.force)
                except DiscoveryError as exc:
                    failures += 1
                    stderr.write(
                        f"{venue_id} {year}: discovery failed "
                        f"({safe_error_summary(exc)}).\n")
                    continue
                primary = outcome.primary
                result = primary.result
                source = "cache" if primary.cache_hit else "provider"
                stdout.write(
                    f"{venue_id} {year}: {source}; "
                    f"confidence={result['confidence']:.2f}; "
                    f"claims={len(result['claims'])}; "
                    f"candidate_milestones="
                    f"{len(result.get('candidate_milestones', []))}; "
                    f"artifact={primary.artifact_path}\n")
                if outcome.escalation_requested and outcome.secondary is None:
                    stdout.write(
                        f"{venue_id} {year}: independent observation not run; "
                        f"reason={outcome.escalation_skipped_reason}.\n")
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        return 1 if failures else 0
    except DiscoveryError as exc:
        stderr.write(f"Discovery setup failed ({type(exc).__name__}).\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
