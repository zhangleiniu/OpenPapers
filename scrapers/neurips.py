"""NeurIPS scraper implementation.

Two strategies, selected automatically:

  papers.nips.cc   Scrapes the official proceedings site (all years it hosts).
  papercopilot     Falls back to the papercopilot GitHub JSON when the
                   proceedings site returns 404 (e.g. NeurIPS 2025+).
                   The "site" field (openreview.net/forum?id=X) is converted
                   to a PDF URL by replacing "forum" with "pdf".

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

import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

from .base import BaseScraper
from config import CACHE_DIR

logger = logging.getLogger(__name__)

# Matches abstract-page hrefs on the listing page.
# e.g. /paper_files/paper/2023/hash/{hex}-Abstract-Conference.html
_ABSTRACT_LINK_RE = re.compile(
    r'/paper_files/paper/\d+/hash/[a-f0-9]+-Abstract(?:-[\w]+(?:_[\w]+)*)?.html$'
)

# -- papercopilot fallback -----------------------------------------------------
_PAPERCOPILOT_URL_TMPL = (
    "https://raw.githubusercontent.com/papercopilot/paperlists/main/nips/nips{year}.json"
)
_PAPERCOPILOT_URL_ALT_TMPL = (
    "https://github.com/papercopilot/paperlists/raw/refs/heads/main/nips/nips{year}.json"
)
_PAPERCOPILOT_ACCEPTED = {"oral", "spotlight", "poster", "accept"}
_PAPERCOPILOT_EXCLUDED_TRACKS = {"tiny", "workshop", "demo"}

_CACHE_PATH = CACHE_DIR / "neurips_papers.json"


class NeurIPSScraper(BaseScraper):
    """NeurIPS conference scraper.

    Scrapes https://papers.nips.cc/ for all years from 2000 onwards.
    Handles both the pre-2022 (no track suffix) and 2022+ (track suffix) URL formats.

    When papers.nips.cc does not yet have a year (returns 404), falls back to
    the papercopilot GitHub JSON — same approach as the ICLR 2026+ strategy.
    """

    NAME = "NeurIPS"
    BASE_URL = "https://papers.nips.cc/"
    REQUEST_DELAY = 0.1


    def __init__(self):
        super().__init__('neurips')
        # In-memory cache for papercopilot papers: paper_id → paper dict
        self._paper_cache: Dict[str, Dict] = {}
        # Tracks whether we're in papercopilot mode for a given year
        self._papercopilot_year: Optional[int] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_paper_urls(self, year: int) -> List[str]:
        """Return all paper URLs for a given NeurIPS year.

        Tries papers.nips.cc first; if 404, falls back to papercopilot JSON.
        """
        # Try the official proceedings site first
        listing_url = f"{self.base_url}paper_files/paper/{year}"
        logger.info(f"Fetching NeurIPS {year} listing: {listing_url}")

        response = self.session.get(listing_url)
        if response:
            paper_urls = self._extract_paper_links(response.content, year)
            if paper_urls:
                logger.info(f"Found {len(paper_urls)} papers for NeurIPS {year}")
                return paper_urls

        # Fallback: papercopilot JSON
        logger.info(f"papers.nips.cc has no listing for {year}, trying papercopilot...")
        papers = self._get_papercopilot_papers(year)
        if not papers:
            logger.error(f"No papers found for NeurIPS {year} from any source")
            return []

        self._papercopilot_year = year
        for p in papers:
            self._paper_cache[p["id"]] = p

        urls = [p["openreview_url"] for p in papers]
        logger.info(f"Found {len(urls)} papers for NeurIPS {year} (via papercopilot)")
        return urls

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse metadata for a single NeurIPS paper.

        If in papercopilot mode, returns from the in-memory cache (no HTTP).
        Otherwise scrapes the papers.nips.cc page.
        """
        # Papercopilot fast path: all metadata already in memory
        if self._papercopilot_year is not None and "openreview.net" in url:
            paper_id = self._extract_openreview_id(url)
            if paper_id and paper_id in self._paper_cache:
                return self._paper_cache[paper_id]

        # Normal path: scrape papers.nips.cc
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

    # ------------------------------------------------------------------
    # Papercopilot fallback (NeurIPS 2025+)
    # ------------------------------------------------------------------

    def _get_papercopilot_papers(self, year: int) -> List[Dict]:
        """Load accepted papers from papercopilot, with disk cache."""
        cache = self._load_cache()
        if str(year) in cache:
            papers = cache[str(year)]
            logger.info(f"NeurIPS {year}: loaded {len(papers)} papers from cache")
            return papers

        raw_entries = self._fetch_papercopilot_json(year)
        if not raw_entries:
            return []

        logger.info(f"papercopilot: {len(raw_entries)} total entries for NeurIPS {year}")

        papers = []
        skipped = 0
        for entry in raw_entries:
            if not self._is_papercopilot_accepted(entry):
                continue
            paper = self._papercopilot_entry_to_paper(entry)
            if paper:
                papers.append(paper)
            else:
                skipped += 1

        logger.info(
            f"papercopilot: {len(papers)} accepted main-track papers "
            f"({skipped} skipped — no valid site URL)"
        )

        if papers:
            cache[str(year)] = papers
            self._save_cache(cache)

        return papers

    def _fetch_papercopilot_json(self, year: int) -> List[Dict]:
        """Download the papercopilot JSON from GitHub."""
        urls = [
            _PAPERCOPILOT_URL_TMPL.format(year=year),
            _PAPERCOPILOT_URL_ALT_TMPL.format(year=year),
        ]
        for url in urls:
            logger.info(f"Downloading papercopilot JSON: {url}")
            resp = self.session.get(url)
            if resp is None:
                continue
            try:
                data = resp.json()
                if isinstance(data, list):
                    logger.info(f"Downloaded {len(data)} entries from papercopilot")
                    return data
            except Exception as e:
                logger.warning(f"Failed to parse papercopilot JSON from {url}: {e}")
                continue

        logger.error(f"Could not download papercopilot JSON for NeurIPS {year}")
        return []

    @staticmethod
    def _is_papercopilot_accepted(entry: Dict) -> bool:
        """Check if a papercopilot entry is an accepted main-track paper."""
        status = str(entry.get("status", "")).lower()
        if not any(kw in status for kw in _PAPERCOPILOT_ACCEPTED):
            return False
        track = str(entry.get("track", "")).lower()
        if track and any(excl in track for excl in _PAPERCOPILOT_EXCLUDED_TRACKS):
            return False
        return True

    @staticmethod
    def _map_papercopilot_status(raw_status: str) -> str:
        """Normalise papercopilot status to Oral/Spotlight/Poster."""
        low = raw_status.lower()
        if "oral" in low:
            return "Oral"
        if "spotlight" in low:
            return "Spotlight"
        return "Poster"

    def _papercopilot_entry_to_paper(self, entry: Dict) -> Optional[Dict]:
        """Convert one papercopilot entry to our internal paper dict."""
        site_url = entry.get("site", "") or entry.get("url", "")
        if not site_url or "openreview.net" not in site_url:
            return None

        paper_id = self._extract_openreview_id(site_url)
        if not paper_id:
            return None

        # forum?id=X → pdf?id=X
        pdf_url = site_url.replace("/forum?", "/pdf?")

        authors_raw = entry.get("authors", [])
        if isinstance(authors_raw, str):
            authors = [a.strip() for a in authors_raw.split(",") if a.strip()]
        elif isinstance(authors_raw, list):
            authors = authors_raw
        else:
            authors = []

        return {
            "id":             paper_id,
            "title":          entry.get("title", ""),
            "authors":        authors,
            "abstract":       entry.get("abstract", ""),
            "keywords":       entry.get("keywords", []) if isinstance(entry.get("keywords"), list) else [],
            "pdf_url":        pdf_url,
            "openreview_url": site_url,
        }

    @staticmethod
    def _extract_openreview_id(url: str) -> Optional[str]:
        """Extract paper ID from an OpenReview URL."""
        m = re.search(r"[?&]id=([^&]+)", url)
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # Disk cache (shared across papercopilot runs)
    # ------------------------------------------------------------------

    def _load_cache(self) -> dict:
        if _CACHE_PATH.exists():
            with open(_CACHE_PATH) as f:
                return json.load(f)
        return {}

    def _save_cache(self, cache: dict) -> None:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved cache → {_CACHE_PATH}")