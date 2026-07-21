# main.py
"""Main script for multi-conference paper scraper."""

import argparse
import logging
import sys
from typing import List

from scrapers.neurips import NeurIPSScraper
from scrapers.icml import ICMLScraper
from scrapers.iclr import ICLRScraper
from scrapers.aaai import AAAIScraper
from scrapers.cvpr import CVPRScraper
from scrapers.colt import COLTScraper
from scrapers.uai import UAIScraper
from scrapers.aistats import AISTATSScraper
from scrapers.jmlr import JMLRScraper
from scrapers.acl import ACLScraper
from scrapers.ijcai import IJCAIScraper
from scrapers.emnlp import EMNLPScraper
from scrapers.naacl import NAACLScraper
from scrapers.iccv import ICCVScraper
from scrapers.eccv import ECCVScraper
from config import DATA_ROOT, LOG_FILE
from utils import assign_bibtex, save_papers

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ]
)

logger = logging.getLogger(__name__)

SCRAPERS = {
    'neurips': NeurIPSScraper,
    'icml':    ICMLScraper,
    'iclr':    ICLRScraper,
    'aaai':    AAAIScraper,
    'cvpr':    CVPRScraper,
    'iccv':    ICCVScraper,
    'colt':    COLTScraper,
    'uai':     UAIScraper,
    'aistats': AISTATSScraper,
    'jmlr':    JMLRScraper,
    'eccv':    ECCVScraper,
    'acl':     ACLScraper,
    'emnlp':   EMNLPScraper,
    'naacl':   NAACLScraper,
    'ijcai':   IJCAIScraper,
}


def create_scraper(conference: str):
    """Instantiate and return a scraper for the given conference key."""
    conference = conference.lower()
    if conference not in SCRAPERS:
        raise ValueError(f"Unknown conference: {conference}. Available: {list(SCRAPERS)}")
    return SCRAPERS[conference]()


def enrich_and_save(papers, conference: str, year: int):
    """Apply optional PDF-derived fallbacks and persist any changes."""
    from postprocessing.backfill_missing_metadata_fields import enrich_papers

    report = enrich_papers(papers)
    filled_total = sum(
        sum(report["filled"][field].values())
        for field in ("abstract", "authors"))
    if filled_total:
        assign_bibtex(papers)  # authors may have been filled by enrichment
        save_papers(papers, conference, year)

    for field in ("abstract", "authors"):
        missing = report["missing"][field]
        filled = sum(report["filled"][field].values())
        if missing:
            logger.info(
                "Enrichment %s: missing=%d, filled=%d, unfilled=%d",
                field, missing, filled, missing - filled)
    return report


def completeness_issues(papers, require_pdfs: bool = True,
                        level: str = "archival"):
    """Return issue counts for announced, metadata, or archival readiness."""
    if level not in {"announced", "metadata", "archival"}:
        raise ValueError(f"Unknown completeness level: {level}")
    required = ["id", "title", "authors", "year", "conference", "url", "bibtex"]
    if level in {"metadata", "archival"}:
        required.append("abstract")
    if level == "archival":
        required.append("pdf_url")
    issues = {
        field: sum(1 for paper in papers if not paper.get(field))
        for field in required
    }
    if level == "archival":
        provisional = sum(
            1 for paper in papers
            if paper.get("publication_status") == "provisional")
        if provisional:
            issues["provisional"] = provisional
    if require_pdfs and level == "archival":
        missing_path = 0
        missing_file = 0
        invalid_pdf = 0
        for paper in papers:
            pdf_path = paper.get("pdf_path")
            if not pdf_path:
                missing_path += 1
                continue
            relative = pdf_path[5:] if pdf_path.startswith("data/") else pdf_path
            path = DATA_ROOT / relative
            if not path.is_file():
                missing_file += 1
                continue
            try:
                with path.open("rb") as handle:
                    if handle.read(5) != b"%PDF-":
                        invalid_pdf += 1
            except OSError:
                missing_file += 1
        issues.update(pdf_path=missing_path, pdf_file=missing_file,
                      invalid_pdf=invalid_pdf)
    return {key: value for key, value in issues.items() if value}


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Multi-conference academic paper scraper',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py neurips 2022
  python main.py iclr 2017 2018 2019
  python main.py neurips 2020 2021 2022 --no-pdfs
  python main.py --list-conferences
        """
    )

    parser.add_argument('conference', nargs='?', help='Conference to scrape')
    parser.add_argument('years', nargs='*', type=int, help='Years to scrape')
    parser.add_argument('--no-pdfs', action='store_true', help='Skip PDF downloads')
    parser.add_argument('--no-resume', action='store_true', help='Start fresh (ignore existing data)')
    parser.add_argument('--list-conferences', action='store_true', help='List available conferences')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable debug logging')
    parser.add_argument(
        '--enrich-missing', action='store_true',
        help='Fill missing abstracts/authors from existing GROBID or Nougat output')
    parser.add_argument(
        '--require-complete', action='store_true',
        help='Exit non-zero if metadata or downloaded PDFs remain incomplete')
    parser.add_argument(
        '--completeness-level', choices=('announced', 'metadata', 'archival'),
        default='archival',
        help='Validation target used with --require-complete (default: archival)')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_conferences:
        print("Available conferences:")
        for key, cls in SCRAPERS.items():
            print(f"  {key}: {cls.NAME or key.upper()}")
        return

    if not args.conference:
        parser.print_help()
        return

    if not args.years:
        print("Error: Please specify at least one year to scrape")
        return 2

    try:
        logger.info(f"Creating scraper for {args.conference} {args.years}")
        scraper = create_scraper(args.conference)

        if len(args.years) == 1:
            papers = scraper.scrape_year(
                args.years[0],
                download_pdfs=not args.no_pdfs,
                resume=not args.no_resume,
            )
            if args.enrich_missing:
                enrich_and_save(papers, args.conference.lower(), args.years[0])
            if args.require_complete:
                issues = completeness_issues(
                    papers, require_pdfs=not args.no_pdfs,
                    level=args.completeness_level)
                if issues:
                    print(f"❌ Incomplete dataset: {issues}")
                    return 2
            print(f"\n✅ Completed! Scraped {len(papers)} papers for {args.years[0]}")
        else:
            results = scraper.scrape_multiple_years(
                args.years,
                download_pdfs=not args.no_pdfs,
                resume=not args.no_resume,
            )
            total = sum(len(p) for p in results.values())
            all_issues = {}
            for year, papers in results.items():
                if args.enrich_missing:
                    enrich_and_save(papers, args.conference.lower(), year)
                if args.require_complete:
                    issues = completeness_issues(
                        papers, require_pdfs=not args.no_pdfs,
                        level=args.completeness_level)
                    if issues:
                        all_issues[year] = issues
            if all_issues:
                print(f"❌ Incomplete dataset: {all_issues}")
                return 2
            print(f"\n✅ Completed! Scraped {total} papers total:")
            for year, papers in results.items():
                print(f"  {year}: {len(papers)} papers")

    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        print(f"❌ Error: {e}")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
