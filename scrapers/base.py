# scrapers/base.py
"""Base scraper class for all conference scrapers."""

import time
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
import logging

from utils import RobustSession, save_papers, load_papers, get_paper_filename
from config import DEFAULT_REQUEST_DELAY, DEFAULT_RETRY_ATTEMPTS, DEFAULT_TIMEOUT, PAPERS_DIR

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Abstract base class for conference scrapers.

    Subclasses must declare:
        NAME          - display name, e.g. "NeurIPS"
        BASE_URL      - root URL for the conference site
        REQUEST_DELAY - seconds between requests (default: DEFAULT_REQUEST_DELAY)
        TIMEOUT       - HTTP timeout in seconds   (default: DEFAULT_TIMEOUT)
    """

    NAME: str = ""
    BASE_URL: str = ""
    REQUEST_DELAY: float = DEFAULT_REQUEST_DELAY
    TIMEOUT: int = DEFAULT_TIMEOUT

    def __init__(self, conference: str):
        self.conference = conference.lower()
        self.base_url = self.BASE_URL
        self.session = RobustSession(
            delay=self.REQUEST_DELAY,
            retry_attempts=DEFAULT_RETRY_ATTEMPTS,
            timeout=self.TIMEOUT,
        )
        logger.info(f"Initialized {self.NAME or self.conference.upper()} scraper")

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
            filename = get_paper_filename(paper)
            pdf_path = PAPERS_DIR / self.conference / str(year) / filename
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
        name = self.NAME or self.conference.upper()
        logger.info(f"Starting scrape of {name} {year}")

        try:
            existing_papers = load_papers(self.conference, year) if resume else []
            existing_urls = {p.get('url', '') for p in existing_papers}

            paper_urls = self.get_paper_urls(year)
            if not paper_urls:
                logger.warning(f"No paper URLs found for {year}")
                return existing_papers

            logger.info(f"Found {len(paper_urls)} paper URLs")

            papers = existing_papers.copy()
            new_count = 0
            failed_count = 0

            for i, url in enumerate(paper_urls):
                try:
                    logger.info(f"Processing {i+1}/{len(paper_urls)}: {url.split('/')[-1]}")

                    if resume and url in existing_urls:
                        logger.debug(f"Skipping existing paper: {url}")
                        continue

                    paper = self.parse_paper(url)
                    if not paper:
                        failed_count += 1
                        logger.warning(f"Failed to parse paper: {url}")
                        continue

                    if not paper.get('title'):
                        failed_count += 1
                        logger.warning(f"No title found for paper: {url}")
                        continue

                    paper['year'] = year
                    paper['conference'] = self.conference
                    paper['url'] = url

                    if download_pdfs:
                        pdf_success = self.download_pdf(paper, year)
                        if not pdf_success:
                            logger.warning(f"PDF download failed for {paper.get('id', 'unknown')}")

                    papers.append(paper)
                    new_count += 1

                    if new_count % 10 == 0:
                        save_papers(papers, self.conference, year)
                        logger.info(f"Saved progress: {len(papers)} papers")

                except Exception as e:
                    failed_count += 1
                    logger.error(f"Error processing {url}: {e}")
                    continue

            save_papers(papers, self.conference, year)
            logger.info(f"Scraping completed for {name} {year}")
            logger.info(f"Total papers: {len(papers)} (new: {new_count}, failed: {failed_count})")
            return papers

        except Exception as e:
            logger.error(f"Scraping failed for {name} {year}: {e}")
            raise

    def scrape_multiple_years(self, years: List[int], **kwargs) -> Dict[int, List[Dict]]:
        """Scrape multiple years with error handling."""
        results = {}
        for year in years:
            try:
                papers = self.scrape_year(year, **kwargs)
                results[year] = papers
                time.sleep(2)
            except Exception as e:
                logger.error(f"Failed to scrape {year}: {e}")
                results[year] = []
        return results
