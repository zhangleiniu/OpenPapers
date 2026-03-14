"""CVPR scraper implementation.

HTML structure of openaccess.thecvf.com (verified):

  Listing page: https://openaccess.thecvf.com/CVPR{year}?day=all
    <dt>
      <a href="/content/...html">Paper Title</a>
    </dt>

  2018-2020: papers are split across daily URLs, e.g.
    https://openaccess.thecvf.com/CVPR2018?day=2018-06-19

  Paper page: https://openaccess.thecvf.com/content/.../papers/...html
    <div id="papertitle">Paper Title</div>
    <div id="authors"><b><i>Author1, Author2</i></b></div>
    <div id="abstract">Abstract text...</div>
    PDF URL is derived by replacing /html/ with /papers/ and .html with .pdf.
"""

import re
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional

from .base import BaseScraper

logger = logging.getLogger(__name__)

# 2021+ use a single ?day=all URL; earlier years split papers across daily URLs
_YEAR_SPECIFIC_URLS = {
    2018: [
        "CVPR2018?day=2018-06-19",
        "CVPR2018?day=2018-06-20",
        "CVPR2018?day=2018-06-21",
    ],
    2019: [
        "CVPR2019?day=2019-06-18",
        "CVPR2019?day=2019-06-19",
        "CVPR2019?day=2019-06-20",
    ],
    2020: [
        "CVPR2020?day=2020-06-16",
        "CVPR2020?day=2020-06-17",
        "CVPR2020?day=2020-06-18",
    ],
}


class CVPRScraper(BaseScraper):
    """CVPR conference scraper using CVF Open Access."""

    NAME = "CVPR"
    BASE_URL = "https://openaccess.thecvf.com/"
    REQUEST_DELAY = 0.1


    def __init__(self):
        super().__init__('cvpr')

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_paper_urls(self, year: int) -> List[str]:
        """Return all paper-page URLs for a given CVPR year."""
        logger.info(f"Getting CVPR {year} paper URLs...")

        suffixes = _YEAR_SPECIFIC_URLS.get(year, [f"CVPR{year}?day=all"])
        urls_to_scrape = [self.base_url + s for s in suffixes]

        paper_urls = []
        try:
            for url in urls_to_scrape:
                response = self.session.get(url)
                if not response:
                    continue
                soup = BeautifulSoup(response.content, 'html.parser')
                for dt in soup.find_all('dt'):
                    a_tag = dt.find('a', href=True)
                    if a_tag and a_tag['href']:
                        paper_urls.append(urljoin(self.base_url, a_tag['href']))

            logger.info(f"Found {len(paper_urls)} papers for CVPR {year}")
            return paper_urls

        except Exception as e:
            logger.error(f"Failed to get paper URLs: {e}")
            return []

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single CVPR paper from its CVF Open Access page."""
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
        authors_div = soup.find(id='authors')
        if authors_div:
            b_tag = authors_div.find('b')
            if b_tag:
                i_tag = b_tag.find('i')
                if i_tag:
                    raw = i_tag.get_text(strip=True)
                    if len(raw) > 3:
                        return [a.strip() for a in raw.split(',') if a.strip()]
        return []

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        div = soup.find(id='abstract')
        if div:
            text = div.get_text(strip=True)
            if len(text) > 3:
                return text
        return ""

    def _extract_paper_id(self, url: str) -> str:
        match = re.search(r'/([^/]+)\.html$', url)
        if match:
            return match.group(1)
        return url.split('/')[-1].replace('.html', '')

    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        return page_url.replace('/html/', '/papers/').replace('.html', '.pdf')