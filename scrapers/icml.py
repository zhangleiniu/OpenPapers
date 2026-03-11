"""ICML scraper implementation.

HTML structure of proceedings.mlr.press (verified):

  Main page: https://proceedings.mlr.press/
    <li>
      <a href="v235/">Proceedings of ICML 2024</a>   ← volume href + year in text
    </li>

  Volume page: https://proceedings.mlr.press/v235/
    <div class="paper">
      <p class="title"><b>Paper Title</b></p>
      <p class="details">
        <span class="authors">Author1, Author2</span>
      </p>
      <p class="links">
        <a href="v235/paper123.html">abs</a>
        <a href="/v235/paper123/paper123.pdf">Download PDF</a>
      </p>
    </div>

  Abstract page: https://proceedings.mlr.press/v235/paper123.html
    <h1>Paper Title</h1>
    <span class="authors">Author1, Author2</span>
    <div id="abstract" class="abstract">Abstract text...</div>
    <a href="/v235/paper123/paper123.pdf">Download PDF</a>
"""

import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

from .base import BaseScraper

logger = logging.getLogger(__name__)

# Matches volume hrefs like "v235/" or "v119/" on the main proceedings page
_VOLUME_HREF_RE = re.compile(r'^v(\d+)/?$')


class ICMLScraper(BaseScraper):
    """ICML conference scraper.

    Scrapes https://proceedings.mlr.press/ for ICML proceedings.
    Discovers the volume number dynamically from the main page.
    Each paper requires exactly one HTTP request in parse_paper.
    """

    def __init__(self):
        super().__init__('icml')
        self._volume_cache: Dict[int, str] = {}  # year -> volume id, e.g. 2024 -> "v235"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_paper_urls(self, year: int) -> List[str]:
        """Return all abstract-page URLs for a given ICML year."""
        volume = self._get_volume_for_year(year)
        if not volume:
            return []

        volume_url = f"{self.base_url}{volume}/"
        logger.info(f"Fetching ICML {year} volume page: {volume_url}")

        response = self.session.get(volume_url)
        if not response:
            logger.error(f"Failed to fetch volume page: {volume_url}")
            return []

        paper_urls = self._extract_paper_links(response.content, volume)
        logger.info(f"Found {len(paper_urls)} papers for ICML {year} ({volume})")
        return paper_urls

    def parse_paper(self, abs_url: str) -> Optional[Dict]:
        """Parse metadata for a single ICML paper from its abstract page.

        All fields (title, authors, abstract, pdf_url) are extracted from the
        abstract page in a single HTTP request.
        """
        response = self.session.get(abs_url)
        if not response:
            logger.warning(f"No response for: {abs_url}")
            return None

        soup = BeautifulSoup(response.content, 'html.parser')

        title = self._extract_title(soup)
        if not title:
            logger.warning(f"No title found: {abs_url}")
            return None

        authors  = self._extract_authors(soup)
        abstract = self._extract_abstract(soup)
        paper_id = self._extract_paper_id(abs_url)
        pdf_url  = self._extract_pdf_url(soup, abs_url)

        paper = {
            'id':       paper_id,
            'title':    title,
            'authors':  authors,
            'abstract': abstract,
            'pdf_url':  pdf_url,
        }

        logger.debug(f"Parsed: {title!r} ({len(authors)} authors)")
        return paper

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_volume_for_year(self, year: int) -> Optional[str]:
        """Return the MLR Press volume identifier (e.g. 'v235') for a given ICML year."""
        if year in self._volume_cache:
            return self._volume_cache[year]

        logger.info(f"Finding ICML volume for year {year}...")

        response = self.session.get(self.base_url)
        if not response:
            logger.error("Failed to fetch MLR Press main page")
            return None

        soup = BeautifulSoup(response.content, 'html.parser')

        # Pattern matches: "Proceedings of ICML 2024" or "ICML 2024 Proceedings"
        icml_pattern = re.compile(
            rf'\b(?:Proceedings\s+of\s+ICML\s+{year}|ICML\s+{year}\s+Proceedings)\b',
            re.IGNORECASE
        )

        for li in soup.find_all('li'):
            if not icml_pattern.search(li.get_text()):
                continue
            link = li.find('a', href=True)
            if not link:
                continue
            href = link['href'].strip('/')
            if _VOLUME_HREF_RE.match(href):
                volume = href if href.startswith('v') else f"v{href}"
                self._volume_cache[year] = volume
                logger.info(f"Found ICML {year} -> {volume}")
                return volume

        logger.warning(f"No ICML volume found for year {year}")
        return None

    def _extract_paper_links(self, html: bytes, volume: str) -> List[str]:
        """Extract abstract-page URLs from a volume page."""
        soup = BeautifulSoup(html, 'html.parser')
        seen = set()
        urls = []

        for paper_div in soup.find_all('div', class_='paper'):
            links_p = paper_div.find('p', class_='links')
            if not links_p:
                continue
            abs_link = links_p.find('a', string='abs')
            if not abs_link or not abs_link.get('href'):
                continue
            full_url = urljoin(self.base_url, abs_link['href'])
            if full_url not in seen:
                seen.add(full_url)
                urls.append(full_url)

        return urls

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract paper title from <h1> on the abstract page."""
        h1 = soup.find('h1')
        if h1:
            return h1.get_text(strip=True)
        return ""

    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        """Extract authors from <span class="authors"> on the abstract page."""
        authors_span = soup.find('span', class_='authors')
        if not authors_span:
            return []
        raw = authors_span.get_text(separator=' ', strip=True)
        raw = raw.replace('\xa0', ' ')  # non-breaking spaces
        return [a.strip() for a in raw.split(',') if a.strip()]

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        """Extract abstract from <div id="abstract" class="abstract">."""
        abstract_div = soup.find('div', id='abstract', class_='abstract')
        if abstract_div:
            return abstract_div.get_text(strip=True)
        # Fallback: any div with class abstract
        abstract_div = soup.find('div', class_='abstract')
        if abstract_div:
            return abstract_div.get_text(strip=True)
        return ""

    def _extract_paper_id(self, abs_url: str) -> str:
        """Extract paper ID from abstract URL.

        e.g. https://proceedings.mlr.press/v235/aamand24a.html -> aamand24a
        """
        match = re.search(r'/v\d+/([^/]+)\.html$', abs_url)
        if match:
            return match.group(1)
        return abs_url.split('/')[-1].replace('.html', '')

    def _extract_pdf_url(self, soup: BeautifulSoup, abs_url: str) -> str:
        """Extract PDF URL from the 'Download PDF' link on the abstract page.

        Returns "" if no PDF link is found.
        """
        for a in soup.find_all('a', href=True):
            text = a.get_text(strip=True)
            href = a['href']
            if ('Download PDF' in text or text.lower() == 'pdf') and href.endswith('.pdf'):
                return urljoin(self.base_url, href)
        return ""