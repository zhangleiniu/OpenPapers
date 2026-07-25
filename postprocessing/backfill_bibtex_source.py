"""Backfill `bibtex_extra` (and therefore a richer `bibtex`) into metadata
scraped before scrapers learned to fetch the publisher's own citation record.

New scrapes already populate `bibtex_extra` at parse time (see
docs/data-schema.md#bibtex-generation). This script re-visits already-scraped
papers over the network and fills in the same field, so old and new records
converge on the same standard. It is *not* a replacement for
`rebuild_bibtex.py`: that script only re-derives `bibtex` from fields already
in the JSON, and `bibtex_extra` was never in the JSON for old records, so it
has nothing to enrich from without a network round-trip per paper.

Reuses each scraper's already-implemented extraction method rather than
duplicating parsing logic:

  - ACL/EMNLP/NAACL, ICML/AISTATS/COLT/UAI:
      one request per paper (fetch the paper page, parse its embedded
      citation block)
  - CVPR/ICCV:
      one request per *year* (1-4 for years split across daily listing
      URLs), not per paper — the CVF year-listing page embeds every paper's
      full BibTeX inline already, so scraper.fetch_bibtex_extra_by_id(year)
      gets the whole year in one shot instead of one request per paper
  - JMLR, IJCAI, AAAI:
      two requests (fetch the paper page to find the BibTeX/bib link, then
      fetch that link)
  - NeurIPS:
      one request (the Bibtex.bib URL is derivable from id+year directly)
  - ECCV:
      two requests (fetch the paper page for its DOI, then CrossRef)
  - ICLR:
      skipped entirely — OpenReview has no richer citation record to fetch
      (see docs/iclr.md)

Resumable: papers that already carry `bibtex_extra` are skipped, so a
half-finished or interrupted run can just be rerun. Dry run unless --write.
On the first write to a given file in a run, the pre-backfill version is
copied to `<file>.bak` (matching the backup convention `utils.save_papers`
already uses for live scrapes).

Cite keys are never touched. `utils.merge_bibtex_extra()` re-renders a
paper's `bibtex` using the key it already has, not `assign_bibtex()`'s
whole-file collision recomputation — pre-existing suffix drift (e.g. an "a"
suffix left over from a since-removed duplicate) is real and was confirmed
to affect ~7.4% of the corpus on a plain rerun, but fixing it is a separate,
deliberately out-of-scope decision from adding bibtex_extra. A paper not
touched by this script keeps its bibtex byte-for-byte identical.
"""

import argparse
import json
import logging
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import METADATA_DIR  # noqa: E402
from utils import merge_bibtex_extra  # noqa: E402

from scrapers.acl_anthology import ACLAnthologyScraper  # noqa: E402
from scrapers.aistats import AISTATSScraper  # noqa: E402
from scrapers.colt import COLTScraper  # noqa: E402
from scrapers.cvpr import CVPRScraper  # noqa: E402
from scrapers.eccv import ECCVScraper  # noqa: E402
from scrapers.iccv import ICCVScraper  # noqa: E402
from scrapers.icml import ICMLScraper  # noqa: E402
from scrapers.ijcai import IJCAIScraper  # noqa: E402
from scrapers.jmlr import JMLRScraper  # noqa: E402
from scrapers.neurips import NeurIPSScraper  # noqa: E402
from scrapers.uai import UAIScraper  # noqa: E402
from scrapers.aaai import AAAIScraper  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_INTERVAL = 25

# Conferences with no richer source record to backfill (see docs/iclr.md).
SKIPPED_CONFERENCES = {"iclr"}


_SCRAPER_FACTORIES = {
    "acl": lambda: ACLAnthologyScraper("acl"),
    "emnlp": lambda: ACLAnthologyScraper("emnlp"),
    "naacl": lambda: ACLAnthologyScraper("naacl"),
    "icml": ICMLScraper,
    "aistats": AISTATSScraper,
    "colt": COLTScraper,
    "uai": UAIScraper,
    "cvpr": CVPRScraper,
    "iccv": ICCVScraper,
    "jmlr": JMLRScraper,
    "ijcai": IJCAIScraper,
    "neurips": NeurIPSScraper,
    "aaai": AAAIScraper,
    "eccv": ECCVScraper,
}


def _make_scrapers(conferences, delay_multiplier=1.0):
    """One scraper instance per requested conference, reused across all its
    papers so each conference's own REQUEST_DELAY/session rate-limiting
    applies. Only instantiates what's actually needed — a few of these
    scrapers initialize a Vertex AI client in __init__, which is wasted
    work (and noisy logging) for conferences not being processed.

    delay_multiplier scales each scraper's configured REQUEST_DELAY after
    construction — a knob for resuming more politely against a host that
    was recently rate-limiting us, without changing the scraper classes'
    defaults used by live scraping."""
    scrapers = {conf: _SCRAPER_FACTORIES[conf]() for conf in conferences}
    if delay_multiplier != 1.0:
        for scraper in scrapers.values():
            scraper.session.delay *= delay_multiplier
    return scrapers


def _fetch_via_page(scraper, paper, extract):
    """Fetch paper['url'], parse it, and hand the soup to `extract`."""
    response = scraper.session.get(paper["url"], quiet_404=True)
    if not response:
        return None
    soup = BeautifulSoup(response.content, "html.parser")
    return extract(soup)


_PAGE_BASED_CONFERENCES = {
    "acl", "emnlp", "naacl", "icml", "aistats", "colt", "uai",
}

# These conferences' scrapers expose fetch_bibtex_extra_by_id(year) — one
# (or a handful of) request(s) covering an entire year's papers at once,
# rather than one request per paper. See cvpr.py/iccv.py docstrings.
_BULK_YEAR_CONFERENCES = {"cvpr", "iccv"}


def _build_fetchers(scrapers):
    """conference -> callable(paper) -> Optional[dict], one per scraper's
    existing extraction method (see each scraper's docstring for exactly
    which fields it contributes and why). Only builds fetchers for the
    conferences actually present in `scrapers`. Bulk-year conferences are
    handled separately by process_file and are not included here."""
    fetchers = {}
    for conf, s in scrapers.items():
        if conf in _BULK_YEAR_CONFERENCES:
            continue
        if conf in _PAGE_BASED_CONFERENCES:
            fetchers[conf] = (lambda p, s=s: _fetch_via_page(s, p, s._extract_bibtex_extra))
        elif conf in ("jmlr", "ijcai", "aaai"):
            fetchers[conf] = (lambda p, s=s:
                               _fetch_via_page(s, p, lambda soup: s._fetch_bibtex_extra(soup, p["url"])))
        elif conf == "eccv":
            fetchers[conf] = (lambda p, s=s:
                               _fetch_via_page(s, p, lambda soup: s._fetch_bibtex_extra(soup)))
        elif conf == "neurips":
            fetchers[conf] = (lambda p, s=s: s._fetch_bibtex_extra(p["id"], p["url"]))
        else:
            raise ValueError(f"No fetcher rule for conference: {conf}")
    return fetchers


def iter_metadata_files(metadata_root, conferences):
    for path in sorted(metadata_root.glob("*/*.json")):
        if path.name.endswith(".bak"):
            continue
        conf = path.parent.name
        if conf not in conferences:
            continue
        yield conf, path


def process_file(conf, path, fetch, write, years, limit_remaining, stats,
                  checkpoint_interval, bulk_scraper=None):
    """Returns the number of papers actually fetched from this file (for
    --limit bookkeeping).

    If `bulk_scraper` is given, `conf` is a bulk-year conference: the whole
    file's bibtex data is fetched in one call to
    bulk_scraper.fetch_bibtex_extra_by_id(year) instead of one `fetch(paper)`
    call per paper."""
    entries = json.loads(path.read_text(encoding="utf-8"))

    backed_up = False
    changed = False
    fetched_here = 0

    def checkpoint():
        nonlocal backed_up
        if not write:
            return
        if not backed_up:
            backup_path = path.with_suffix(".json.bak")
            shutil.copy2(path, backup_path)
            backed_up = True
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    bulk_by_id = None
    if bulk_scraper is not None:
        year = int(path.stem.rsplit("_", 1)[1])
        if years and year not in years:
            return 0
        try:
            bulk_by_id = bulk_scraper.fetch_bibtex_extra_by_id(year)
        except Exception as exc:
            logger.warning("[%s] bulk year fetch failed for %s: %s",
                            conf, path.name, exc)
            bulk_by_id = {}

    for paper in entries:
        if limit_remaining is not None and limit_remaining <= 0:
            break
        if paper.get("bibtex_extra"):
            stats["already_done"] += 1
            continue
        if years and int(paper.get("year", 0)) not in years:
            continue

        if bulk_by_id is not None:
            extra = bulk_by_id.get(paper.get("id"))
        else:
            if not paper.get("url"):
                stats["no_url"] += 1
                continue
            try:
                extra = fetch(paper)
            except Exception as exc:
                logger.warning("[%s] fetch failed for %s: %s",
                                conf, paper.get("id", "?"), exc)
                extra = None
                stats["errors"] += 1

        fetched_here += 1
        stats["fetched"] += 1
        if limit_remaining is not None:
            limit_remaining -= 1

        if extra and merge_bibtex_extra(paper, extra):
            changed = True
            stats["enriched"] += 1
        else:
            stats["empty"] += 1

        if changed and fetched_here % checkpoint_interval == 0:
            checkpoint()

    if changed:
        checkpoint()

    return fetched_here


def main(write, metadata_root, conferences, years, limit, checkpoint_interval,
         delay_multiplier=1.0):
    target_conferences = set(conferences) - SKIPPED_CONFERENCES
    unknown = target_conferences - set(_SCRAPER_FACTORIES)
    if unknown:
        raise SystemExit(f"No backfill handler for: {sorted(unknown)}")

    scrapers = _make_scrapers(target_conferences, delay_multiplier)
    fetchers = _build_fetchers(scrapers)

    stats = Counter()
    remaining = limit
    started = time.time()

    for conf, path in iter_metadata_files(metadata_root, target_conferences):
        if remaining is not None and remaining <= 0:
            break
        is_bulk = conf in _BULK_YEAR_CONFERENCES
        fetched = process_file(
            conf, path, None if is_bulk else fetchers[conf], write, years,
            remaining, stats, checkpoint_interval,
            bulk_scraper=scrapers[conf] if is_bulk else None)
        if remaining is not None:
            remaining -= fetched
        if fetched:
            logger.info("[%s] %s: fetched=%d enriched=%d empty=%d errors=%d "
                        "(total fetched so far: %d)",
                        conf, path.name, fetched, stats["enriched"],
                        stats["empty"], stats["errors"], stats["fetched"])

    elapsed = time.time() - started
    print(f"\nMode: {'WRITE' if write else 'DRY RUN'}")
    print(f"Conferences: {sorted(target_conferences)}")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Already had bibtex_extra (skipped): {stats['already_done']}")
    print(f"No url field (skipped): {stats['no_url']}")
    print(f"Fetched: {stats['fetched']}")
    print(f"  Enriched: {stats['enriched']}")
    print(f"  Empty result (no bibtex_extra found): {stats['empty']}")
    print(f"  Errors: {stats['errors']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill bibtex_extra into already-scraped metadata by "
                    "re-fetching each paper's source citation record. Dry "
                    "run unless --write.")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--metadata-root", type=Path, default=METADATA_DIR)
    parser.add_argument(
        "--conferences", type=str, default=None,
        help="Comma-separated conference keys (default: all except iclr)")
    parser.add_argument(
        "--years", type=str, default=None,
        help="Comma-separated years to restrict to (default: all years)")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after fetching this many papers total (for piloting)")
    parser.add_argument(
        "--checkpoint-interval", type=int, default=DEFAULT_CHECKPOINT_INTERVAL,
        help=f"Papers changed per file before an intermediate save "
             f"(default: {DEFAULT_CHECKPOINT_INTERVAL})")
    parser.add_argument(
        "--delay-multiplier", type=float, default=1.0,
        help="Scale each scraper's configured REQUEST_DELAY by this factor. "
             "Use >1 to resume more politely against a host that was "
             "recently rate-limiting us (default: 1.0, no change)")
    args = parser.parse_args()

    all_conferences = {
        "acl", "emnlp", "naacl", "icml", "aistats", "colt", "uai",
        "cvpr", "iccv", "jmlr", "ijcai", "neurips", "aaai", "eccv",
    }
    conferences = (set(c.strip() for c in args.conferences.split(","))
                   if args.conferences else all_conferences)
    years = (set(int(y.strip()) for y in args.years.split(","))
             if args.years else None)

    main(write=args.write, metadata_root=args.metadata_root,
         conferences=conferences, years=years, limit=args.limit,
         checkpoint_interval=args.checkpoint_interval,
         delay_multiplier=args.delay_multiplier)
