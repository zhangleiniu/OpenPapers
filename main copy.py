# main.py
"""Main script for multi-conference paper scraper."""

import argparse
import logging
from typing import Dict, Type

from scrapers.base import BaseScraper
from scrapers.neurips import NeurIPSScraper
from scrapers.icml import ICMLScraper
from scrapers.iclr import ICLRScraper
from scrapers.aaai import AAAIScraper

from config import CONFERENCES

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


class ScraperFactory:
    """Factory to create conference scrapers."""
    
    def __init__(self):
        self.scrapers: Dict[str, Type] = {
            'neurips': NeurIPSScraper,
            'icml': ICMLScraper,
            'iclr': ICLRScraper,
            'aaai': AAAIScraper,
        }
    
    def get_available_conferences(self) -> list:
        """Get list of available conference scrapers."""
        return list(self.scrapers.keys())
    
    def create_scraper(self, conference: str):
        """Create a scraper for the given conference."""
        conference = conference.lower()
        
        if conference not in self.scrapers:
            available = self.get_available_conferences()
            raise ValueError(f"Unknown conference: {conference}. Available: {available}")
        
        return self.scrapers[conference]()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Multi-conference academic paper scraper',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scrape NeurIPS 2022 with PDFs
  python main.py neurips 2022
  
  # Scrape multiple years, no PDFs
  python main.py neurips 2020 2021 2022 --no-pdfs
  
  # List available conferences
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
    
    # Set log level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Create factory
    factory = ScraperFactory()
    
    # Handle list conferences
    if args.list_conferences:
        print("Available conferences:")
        for conf in factory.get_available_conferences():
            conf_info = CONFERENCES.get(conf, {})
            name = conf_info.get('name', conf.upper())
            print(f"  {conf}: {name}")
        return
    
    # Validate arguments
    if not args.conference:
        parser.print_help()
        return
    
    if not args.years:
        print("Error: Please specify at least one year to scrape")
        return
    
    try:
        # Create scraper
        logger.info(f"Creating scraper for {args.conference}")
        scraper = factory.create_scraper(args.conference)
        
        # Scrape papers
        if len(args.years) == 1:
            # Single year
            papers = scraper.scrape_year(
                args.years[0],
                download_pdfs=not args.no_pdfs,
                resume=not args.no_resume
            )
            print(f"\n✅ Completed! Scraped {len(papers)} papers for {args.years[0]}")
        
        else:
            # Multiple years
            results = scraper.scrape_multiple_years(
                args.years,
                download_pdfs=not args.no_pdfs,
                resume=not args.no_resume
            )
            
            total_papers = sum(len(papers) for papers in results.values())
            print(f"\n✅ Completed! Scraped {total_papers} papers total:")
            for year, papers in results.items():
                print(f"  {year}: {len(papers)} papers")
    
    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        print(f"❌ Error: {e}")
        return 1
    
    return 0


if __name__ == '__main__':
    exit(main())