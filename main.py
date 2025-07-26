# main.py
"""Main script for multi-conference paper scraper."""

import argparse
import logging
from typing import Dict, Type, List

from scrapers.neurips import NeurIPSScraper
from scrapers.icml import ICMLScraper
from scrapers.iclr import ICLRScraper
from scrapers.iclr_1516 import ICLRScraper1516 
from scrapers.aaai import AAAIScraper
from scrapers.cvpr import CVPRScraper  
from scrapers.colt import COLTScraper
from scrapers.uai import UAIScraper 
from scrapers.uai_1518 import UAIScraper1518
from scrapers.aistats import AISTATSScraper
from scrapers.jmlr import JMLRScraper  
from scrapers.acl import ACLScraper
from scrapers.ijcai import IJCAIScraper
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
    """Factory to create conference scrapers with year-specific handling."""
    
    def __init__(self):
        self.scrapers: Dict[str, Type] = {
            'neurips': NeurIPSScraper,
            'icml': ICMLScraper,
            'iclr': ICLRScraper,
            'aaai': AAAIScraper,
            'cvpr': CVPRScraper, 
            'colt': COLTScraper,
            'uai': UAIScraper, 
            'aistats': AISTATSScraper, 
            'jmlr': JMLRScraper, 
            'acl': ACLScraper, 
            'ijcai': IJCAIScraper 
        }
        
        # Year-specific scraper mappings
        self.year_specific_scrapers = {
            'iclr': {
                (2015, 2016): ICLRScraper1516,  # Use new scraper for 2015-2016
                # Default ICLRScraper will be used for other years
            },
            'uai': {
                (2015, 2018): UAIScraper1518, 
            }
        
        }
    
    def get_available_conferences(self) -> list:
        """Get list of available conference scrapers."""
        return list(self.scrapers.keys())
    
    def create_scraper(self, conference: str, years: List[int] = None):
        """Create a scraper for the given conference and years."""
        conference = conference.lower()
        
        if conference not in self.scrapers:
            available = self.get_available_conferences()
            raise ValueError(f"Unknown conference: {conference}. Available: {available}")
        
        # Check for year-specific scrapers
        if years and conference in self.year_specific_scrapers:
            year_mappings = self.year_specific_scrapers[conference]
            
            # Find the appropriate scraper based on years
            for year_range, scraper_class in year_mappings.items():
                if isinstance(year_range, tuple):
                    start, end = year_range
                    if all(start <= year <= end for year in years):
                        logger.info(f"Using year-specific scraper for {conference} {years}: {scraper_class.__name__}")
                        return scraper_class()
                elif isinstance(year_range, list):
                    if all(year in year_range for year in years):
                        logger.info(f"Using year-specific scraper for {conference} {years}: {scraper_class.__name__}")
                        return scraper_class()
                elif isinstance(year_range, int):
                    if all(year == year_range for year in years):
                        logger.info(f"Using year-specific scraper for {conference} {years}: {scraper_class.__name__}")
                        return scraper_class()
        
        # Use default scraper
        logger.info(f"Using default scraper for {conference}: {self.scrapers[conference].__name__}")
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
  
  # Scrape ICLR 2015-2016 (uses specialized scraper)
  python main.py iclr 2015 2016
  
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
        # Create scraper with year awareness
        logger.info(f"Creating scraper for {args.conference} {args.years}")
        scraper = factory.create_scraper(args.conference, args.years)
        
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