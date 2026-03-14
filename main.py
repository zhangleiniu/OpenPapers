# main.py
"""Main script for multi-conference paper scraper."""

import argparse
import logging
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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('scraper.log')
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
        return

    try:
        logger.info(f"Creating scraper for {args.conference} {args.years}")
        scraper = create_scraper(args.conference)

        if len(args.years) == 1:
            papers = scraper.scrape_year(
                args.years[0],
                download_pdfs=not args.no_pdfs,
                resume=not args.no_resume,
            )
            print(f"\n✅ Completed! Scraped {len(papers)} papers for {args.years[0]}")
        else:
            results = scraper.scrape_multiple_years(
                args.years,
                download_pdfs=not args.no_pdfs,
                resume=not args.no_resume,
            )
            total = sum(len(p) for p in results.values())
            print(f"\n✅ Completed! Scraped {total} papers total:")
            for year, papers in results.items():
                print(f"  {year}: {len(papers)} papers")

    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        print(f"❌ Error: {e}")
        return 1

    return 0


if __name__ == '__main__':
    exit(main())