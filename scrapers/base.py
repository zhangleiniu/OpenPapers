# scrapers/base.py
"""Base scraper class for all conference scrapers."""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional
import logging
from pathlib import Path

from utils import RobustSession, save_papers, load_papers, get_paper_filename
from config import CONFERENCES, PAPERS_DIR

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Abstract base class for conference scrapers."""
    
    def __init__(self, conference_name: str):
        """Initialize scraper with conference-specific settings."""
        self.conference = conference_name.lower()
        
        # Get conference config
        self.config = CONFERENCES.get(self.conference, {})
        if not self.config:
            raise ValueError(f"Unknown conference: {conference_name}")
        
        # Setup HTTP session with conference-specific settings
        self.session = RobustSession(
            delay=self.config.get('request_delay', 1.0),
            retry_attempts=self.config.get('retry_attempts', 3),
            timeout=self.config.get('timeout', 30)
        )
        
        self.base_url = self.config['base_url']
        logger.info(f"Initialized {self.config['name']} scraper")
    
    @abstractmethod
    def get_paper_urls(self, year: int) -> List[str]:
        """Get list of paper URLs for a given year."""
        pass
    
    @abstractmethod
    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single paper from its URL."""
        pass
    
    def download_pdf(self, paper: Dict, year: int) -> bool:
        """Download PDF for a paper."""
        if not paper.get('pdf_url'):
            logger.warning(f"No PDF URL for paper {paper.get('id', 'unknown')}")
            return False
        
        try:
            # Generate filename
            filename = get_paper_filename(paper)
            
            # Create path
            pdf_path = PAPERS_DIR / self.conference / str(year) / filename
            
            # Download
            success = self.session.download_file(paper['pdf_url'], pdf_path)
            
            if success:
                paper['pdf_path'] = str(pdf_path)
            
            return success
            
        except Exception as e:
            logger.error(f"Failed to download PDF for {paper.get('id', 'unknown')}: {e}")
            return False
    
    def scrape_year(self, year: int, download_pdfs: bool = True, 
                   resume: bool = True) -> List[Dict]:
        """Scrape all papers for a given year."""
        logger.info(f"Starting scrape of {self.config['name']} {year}")
        
        try:
            # Load existing papers if resuming
            existing_papers = []
            if resume:
                existing_papers = load_papers(self.conference, year)
            
            existing_urls = {p.get('url', '') for p in existing_papers}
            
            # Get paper URLs
            paper_urls = self.get_paper_urls(year)
            if not paper_urls:
                logger.warning(f"No paper URLs found for {year}")
                return existing_papers
            
            logger.info(f"Found {len(paper_urls)} paper URLs")
            
            # Process papers
            papers = existing_papers.copy()
            new_count = 0
            failed_count = 0
            
            for i, url in enumerate(paper_urls):
                try:
                    logger.info(f"Processing {i+1}/{len(paper_urls)}: {url.split('/')[-1]}")
                    
                    # Skip if already processed
                    if resume and url in existing_urls:
                        logger.debug(f"Skipping existing paper: {url}")
                        continue
                    
                    # Parse paper
                    paper = self.parse_paper(url)
                    if not paper:
                        failed_count += 1
                        logger.warning(f"Failed to parse paper: {url}")
                        continue
                    
                    if not paper.get('title'):
                        failed_count += 1
                        logger.warning(f"No title found for paper: {url}")
                        continue
                    
                    # Add metadata
                    paper['year'] = year
                    paper['conference'] = self.conference
                    paper['url'] = url
                    
                    # Download PDF if requested
                    if download_pdfs:
                        pdf_success = self.download_pdf(paper, year)
                        if not pdf_success:
                            logger.warning(f"PDF download failed for {paper.get('id', 'unknown')}")
                    
                    papers.append(paper)
                    new_count += 1
                    
                    # Save periodically to avoid losing progress
                    if new_count % 10 == 0:
                        save_papers(papers, self.conference, year)
                        logger.info(f"Saved progress: {len(papers)} papers")
                
                except Exception as e:
                    failed_count += 1
                    logger.error(f"Error processing {url}: {e}")
                    continue
            
            # Final save
            save_papers(papers, self.conference, year)
            
            # Log summary
            logger.info(f"Scraping completed for {self.config['name']} {year}")
            logger.info(f"Total papers: {len(papers)} (new: {new_count}, failed: {failed_count})")
            
            return papers
            
        except Exception as e:
            logger.error(f"Scraping failed for {self.config['name']} {year}: {e}")
            raise
    
    def scrape_multiple_years(self, years: List[int], **kwargs) -> Dict[int, List[Dict]]:
        """Scrape multiple years with error handling."""
        results = {}
        
        for year in years:
            try:
                papers = self.scrape_year(year, **kwargs)
                results[year] = papers
                
                # Brief pause between years
                import time
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"Failed to scrape {year}: {e}")
                results[year] = []
                continue
        
        return results