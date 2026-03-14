"""ECCV scraper implementation.

HTML structure of eccv.ecva.net (verified):

  Listing page: https://www.ecva.net/papers.php
    All years are listed on a single page. Each year is represented by
    an accordion button containing the year string, followed by a panel:

    <button class="accordion">... ECCV 2024 ...</button>
    <div class="accordion-content">
      <dt class="ptitle">
        <a href="/papers/eccv_2024/papers_ECCV/html/...php">Paper Title</a>
      </dt>
    </div>

  Paper page: https://www.ecva.net/papers/eccv_{year}/papers_ECCV/html/...php
    <div id="papertitle">Paper Title</div>
    <div id="authors"><i>Author1, Author2*</i></div>
    <div id="abstract">Abstract text...</div>
    <a href="../papers/...pdf">pdf</a>
"""

import re
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional

from .base import BaseScraper

logger = logging.getLogger(__name__)


class ECCVScraper(BaseScraper):
    """ECCV conference scraper using ecva.net."""

    NAME = "ECCV"
    BASE_URL = "https://www.ecva.net/"
    REQUEST_DELAY = 0.15
    TIMEOUT = 45


    def __init__(self):
        super().__init__('eccv')

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_paper_urls(self, year: int) -> List[str]:
        """Return all paper-page URLs for a given ECCV year.

        All years are listed on a single page at /papers.php.
        The target year's section is identified by matching the year
        string against each accordion button's text.
        """
        logger.info(f"Getting ECCV {year} paper URLs...")

        try:
            url = f"{self.base_url}/papers.php"
            response = self.session.get(url)
            if not response:
                return []

            soup = BeautifulSoup(response.content, 'html.parser')
            paper_urls = []

            for button in soup.find_all('button', class_='accordion'):
                if str(year) not in button.text:
                    continue
                panel = button.find_next_sibling('div', class_='accordion-content')
                if not panel:
                    continue
                for dt in panel.find_all('dt', class_='ptitle'):
                    a_tag = dt.find('a', href=True)
                    if a_tag:
                        paper_urls.append(self.base_url + a_tag['href'])

            logger.info(f"Found {len(paper_urls)} papers for ECCV {year}")
            return paper_urls

        except Exception as e:
            logger.error(f"Failed to get paper URLs: {e}")
            return []

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single ECCV paper from its ecva.net page."""
        try:
            response = self.session.get(url)
            if not response:
                return None

            soup = BeautifulSoup(response.content, 'html.parser')

            title = self._extract_title(soup)
            if not title:
                logger.warning(f"No title found: {url}")
                return None

            authors  = self._extract_authors(soup)
            abstract = self._extract_abstract(soup)
            paper_id = self._extract_paper_id(url)
            pdf_url  = self._extract_pdf_url(soup, url)

            paper = {
                'id':       paper_id,
                'title':    title,
                'authors':  authors,
                'abstract': abstract,
                'pdf_url':  pdf_url,
            }

            logger.debug(f"Parsed: {title!r} ({len(authors)} authors)")
            return paper

        except Exception as e:
            logger.error(f"Failed to parse {url}: {e}")
            return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_title(self, soup: BeautifulSoup) -> str:
        div = soup.find('div', id='papertitle')
        return div.get_text(strip=True) if div else ""

    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        div = soup.find('div', id='authors')
        if div:
            i_tag = div.find('i')
            if i_tag:
                raw = i_tag.get_text(strip=True).replace('*', '')
                return [a.strip() for a in raw.split(',') if a.strip()]
        return []

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        div = soup.find('div', id='abstract')
        if div:
            return div.get_text(strip=True).strip('"').strip("'")
        return ""

    def _extract_paper_id(self, url: str) -> str:
        match = re.search(r'/html/(.*?)(?:\.php|$)', url)
        return match.group(1) if match else ""

    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        a_tag = soup.find('a', href=True, string='pdf')
        if a_tag:
            return urljoin(page_url, a_tag['href'])
        return ""