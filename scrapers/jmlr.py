"""JMLR scraper implementation.

HTML structure of jmlr.org (verified):

  Volume page: https://www.jmlr.org/papers/v{volume}/
    <dl>
      <dd>
        <a href="meila00a.html">abs</a>
      </dd>
    </dl>

  Volume mapping: volume = year - 1999 (v1 = 2000, v2 = 2001, ...)

  Abstract page: https://www.jmlr.org/papers/v1/meila00a.html
    <h2>Paper Title</h2>
    <i>Author1, Author2</i>
    <p class="abstract">Abstract text...</p>   ← volumes 6+
    <h3>Abstract</h3> ... (text nodes)          ← volumes 1-5 (older format)
    <a href="...pdf">pdf</a>
"""

import re
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional

from .base import BaseScraper

logger = logging.getLogger(__name__)


class JMLRScraper(BaseScraper):
    """JMLR scraper."""

    def __init__(self):
        super().__init__('jmlr')

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_paper_urls(self, year: int) -> List[str]:
        """Return all abstract-page URLs for a given JMLR year.

        JMLR volume number = year - 1999 (v1 = 2000, v2 = 2001, ...).
        """
        logger.info(f"Getting JMLR {year} paper URLs...")

        volume = year - 1999
        url = f"{self.base_url}/papers/v{volume}/"

        try:
            response = self.session.get(url)
            if not response:
                return []

            soup = BeautifulSoup(response.content, 'html.parser')
            paper_urls = []

            for dl in soup.find_all('dl'):
                dd_tag = dl.find('dd')
                if dd_tag:
                    a_tag = dd_tag.find('a', href=True, string=lambda s: s and 'abs' in s.lower())
                    if a_tag and a_tag['href']:
                        full_url = urljoin(f"{self.base_url}/papers/v{volume}/", a_tag['href'])
                        paper_urls.append(full_url)

            logger.info(f"Found {len(paper_urls)} papers for JMLR {year}")
            return paper_urls

        except Exception as e:
            logger.error(f"Failed to get paper URLs: {e}")
            return []

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single JMLR paper from its abstract page."""
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
        h2 = soup.find('h2')
        if h2:
            title = h2.get_text(strip=True)
            if len(title) > 3:
                return title
        return ""

    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        i_tag = soup.find('i')
        if i_tag:
            raw = i_tag.get_text(strip=True)
            if raw:
                return [a.strip() for a in raw.split(',') if a.strip()]
        return []

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        # Volumes 6+: <p class="abstract">
        p_tag = soup.find('p', class_='abstract')
        if p_tag:
            return p_tag.get_text(strip=True)

        # Volumes 1-5: <h3>Abstract</h3> followed by text nodes
        h3_tag = soup.find('h3', string=lambda s: s and s.strip().lower() == "abstract")
        if h3_tag:
            parts = []
            for sib in h3_tag.next_siblings:
                if getattr(sib, 'name', None) in ('font', 'p', 'h3', 'h2', 'h1', 'div'):
                    break
                if hasattr(sib, 'get_text'):
                    text = sib.get_text(separator=' ', strip=True).replace('\n', '').replace('\r', ' ').strip()
                elif hasattr(sib, 'strip'):
                    text = sib.strip().replace('\n', '').replace('\r', ' ').strip()
                else:
                    continue
                if text:
                    parts.append(text)
            return ' '.join(parts)

        return ""

    def _extract_paper_id(self, url: str) -> str:
        match = re.search(r'v\d+/([^/]+)\.html', url)
        return match.group(1) if match else ""

    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        a_tag = soup.find('a', string=lambda s: s and 'pdf' in s.lower())
        if a_tag and a_tag.get('href'):
            return urljoin(self.base_url, a_tag['href'])
        return ""