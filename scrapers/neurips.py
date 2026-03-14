"""NeurIPS scraper implementation.

Current HTML structure of papers.nips.cc (verified against live page, 2025):

  Listing page: https://papers.nips.cc/paper_files/paper/{year}
    <a href="/paper_files/paper/{year}/hash/{hash}-Abstract[-Track].html">Title</a>

  Paper page: https://papers.nips.cc/.../hash/{hash}-Abstract[-Track].html
    <h1 class="paper-title">Paper Title</h1>
    <p class="paper-authors">Author1, Author2</p>
    <section class="paper-section">
        <h2 class="section-label">Abstract</h2>
        <p class="paper-abstract"></p>          ← empty anchor element
        <p>Actual abstract text here...</p>     ← real text is here
    </section>
    <a href="/paper_files/paper/{year}/file/{hash}-Paper[-Track].pdf">Paper</a>

  URL patterns:
    2022+ (with track):  {hash}-Abstract-Conference.html  → {hash}-Paper-Conference.pdf
    pre-2022 (no track): {hash}-Abstract.html             → {hash}-Paper.pdf
"""

import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

from .base import BaseScraper

logger = logging.getLogger(__name__)

# Matches abstract-page hrefs on the listing page.
# e.g. /paper_files/paper/2023/hash/{hex}-Abstract-Conference.html
_ABSTRACT_LINK_RE = re.compile(
    r'/paper_files/paper/\d+/hash/[a-f0-9]+-Abstract(?:-[\w]+(?:_[\w]+)*)?.html$'
)


class NeurIPSScraper(BaseScraper):
    """NeurIPS conference scraper.

    Scrapes https://papers.nips.cc/ for all years from 2000 onwards.
    Handles both the pre-2022 (no track suffix) and 2022+ (track suffix) URL formats.
    """

    NAME = "NeurIPS"
    BASE_URL = "https://papers.nips.cc/"
    REQUEST_DELAY = 0.1


    def __init__(self):
        super().__init__('neurips')

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_paper_urls(self, year: int) -> List[str]:
        """Return all abstract-page URLs for a given NeurIPS year."""
        listing_url = f"{self.base_url}paper_files/paper/{year}"
        logger.info(f"Fetching NeurIPS {year} listing: {listing_url}")

        response = self.session.get(listing_url)
        if not response:
            logger.error(f"Failed to fetch listing page: {listing_url}")
            return []

        paper_urls = self._extract_paper_links(response.content, year)
        logger.info(f"Found {len(paper_urls)} papers for NeurIPS {year}")
        return paper_urls

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse metadata for a single NeurIPS paper page."""
        response = self.session.get(url)
        if not response:
            logger.warning(f"No response for: {url}")
            return None

        soup = BeautifulSoup(response.content, 'html.parser')

        title = self._extract_title(soup)
        if not title:
            logger.warning(f"No title found: {url}")
            return None

        authors   = self._extract_authors(soup)
        abstract  = self._extract_abstract(soup)
        paper_id  = self._extract_paper_id(url)
        pdf_url   = self._extract_pdf_url(soup, url)

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

    def _extract_paper_links(self, html: bytes, year: int) -> List[str]:
        """Extract abstract-page URLs from the NeurIPS listing page."""
        soup = BeautifulSoup(html, 'html.parser')
        seen = set()
        urls = []

        for a in soup.find_all('a', href=True):
            href = a['href']
            if not _ABSTRACT_LINK_RE.search(href):
                continue
            full_url = urljoin(self.base_url, href)
            if full_url not in seen:
                seen.add(full_url)
                urls.append(full_url)

        return urls

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract paper title from <h1 class="paper-title">."""
        title_elem = soup.find('h1', class_='paper-title')
        if title_elem:
            return title_elem.get_text(strip=True)
        return ""

    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        """Extract authors from <p class="paper-authors">Author1, Author2</p>."""
        authors_p = soup.find('p', class_='paper-authors')
        if not authors_p:
            return []
        raw = authors_p.get_text(strip=True)
        return [a.strip() for a in raw.split(',') if a.strip()]

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        """Extract abstract from the paper-section.

        Structure:
            <section class="paper-section">
                <h2 class="section-label">Abstract</h2>
                <p class="paper-abstract"></p>   <- empty anchor element
                <p>Real abstract text...</p>     <- target
            </section>

        We find the empty anchor <p class="paper-abstract"> then take its
        next sibling <p>, which reliably contains the abstract text.
        """
        section = soup.find('section', class_='paper-section')
        if not section:
            return ""

        # Confirm this is the Abstract section (in case there are multiple sections)
        label = section.find('h2', class_='section-label')
        if not label or label.get_text(strip=True).lower() != 'abstract':
            return ""

        # The anchor <p> is empty; the real text is in the next sibling <p>
        anchor_p = section.find('p', class_='paper-abstract')
        if anchor_p:
            sibling = anchor_p.find_next_sibling('p')
            if sibling:
                return sibling.get_text(strip=True)

        # Fallback: first <p> with meaningful text in the section
        for p in section.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) > 20:
                return text

        return ""

    def _extract_paper_id(self, url: str) -> str:
        """Extract the hex hash from a NeurIPS abstract URL."""
        match = re.search(r'/hash/([a-f0-9]+)', url)
        if match:
            return match.group(1)
        return url.split('/')[-1].replace('.html', '').replace('-Abstract', '')

    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        """Extract PDF URL — prefer the direct link on the page.

        The paper page always has:
            <a href="/paper_files/paper/{year}/file/{hash}-Paper[-Track].pdf">Paper</a>

        Falls back to URL construction only if no direct link is found.
        Returns "" (not an exception) if nothing can be determined.
        """
        # Primary: find the "Paper" download link directly on the page
        for a in soup.find_all('a', href=True):
            href = a['href']
            if '/file/' in href and href.endswith('.pdf') and '-Paper' in href:
                return urljoin(self.base_url, href)

        # Fallback: construct from the abstract URL pattern
        return self._construct_pdf_url(page_url)

    def _construct_pdf_url(self, abstract_url: str) -> str:
        """Derive PDF URL from abstract-page URL as a fallback.

        Returns "" on unexpected input (no exception).
        """
        if '/hash/' not in abstract_url:
            logger.warning(f"Cannot construct PDF URL — unexpected format: {abstract_url}")
            return ""

        file_url = abstract_url.replace('/hash/', '/file/')

        # With track suffix (2022+): -Abstract-Track.html -> -Paper-Track.pdf
        match = re.search(r'-Abstract-([\w]+(?:_[\w]+)*)\.html$', file_url)
        if match:
            track = match.group(1)
            return re.sub(r'-Abstract-[\w]+(?:_[\w]+)*\.html$', f'-Paper-{track}.pdf', file_url)

        # Without track suffix (pre-2022): -Abstract.html -> -Paper.pdf
        if file_url.endswith('-Abstract.html'):
            return file_url.replace('-Abstract.html', '-Paper.pdf')

        logger.warning(f"Cannot construct PDF URL — unrecognised suffix: {abstract_url}")
        return ""