"""UAI scraper implementation.

Routing:
  year <= 2018  ->  _scrape_year_legacy()  (auai.org single-page format)
  year >= 2019  ->  super().scrape_year()  (proceedings.mlr.press)

MLR Press volumes follow the same structure as the ICML scraper.
Each paper requires exactly one HTTP request in parse_paper.
"""

import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

from .base import BaseScraper
from utils import save_papers

logger = logging.getLogger(__name__)

_VOLUME_HREF_RE = re.compile(r'^v(\d+)/?$')

# auai.org URLs differ by year
_LEGACY_URLS = {
    2018: "https://www.auai.org/uai2018/accepted.php",
    2017: "https://www.auai.org/uai2017/accepted.php",
    2016: "https://www.auai.org/uai2016/proceedings.php",
    2015: "https://www.auai.org/uai2015/acceptedPapers.shtml",
}

_LEGACY_PRE2017_YEARS = {2015, 2016}


class UAIScraper(BaseScraper):
    """UAI conference scraper.

    Covers all years by routing internally:
    - 2015-2018: auai.org single-page format (all papers in one HTML table)
    - 2019+:     proceedings.mlr.press (one abstract page per paper)
    """

    NAME = "UAI"
    BASE_URL = "https://proceedings.mlr.press/"
    REQUEST_DELAY = 0.15
    TIMEOUT = 45


    def __init__(self):
        super().__init__('uai')
        self._volume_cache: Dict[int, str] = {
            2025: 'v286',
        }

    # ==================================================================
    # Public interface
    # ==================================================================

    def scrape_year(self, year: int, download_pdfs: bool = True, resume: bool = True) -> List[Dict]:
        if year <= 2018:
            return self._scrape_year_legacy(year, download_pdfs, resume)
        return super().scrape_year(year, download_pdfs, resume)

    # Used by BaseScraper.scrape_year for years >= 2019
    def get_paper_urls(self, year: int) -> List[str]:
        """Return all abstract-page URLs for a given UAI year (MLR Press)."""
        volume = self._get_volume_for_year(year)
        if not volume:
            return []

        volume_url = f"{self.base_url}{volume}/"
        logger.info(f"Fetching UAI {year} volume page: {volume_url}")

        response = self.session.get(volume_url)
        if not response:
            logger.error(f"Failed to fetch volume page: {volume_url}")
            return []

        paper_urls = self._extract_paper_links(response.content)
        logger.info(f"Found {len(paper_urls)} papers for UAI {year} ({volume})")
        return paper_urls

    def parse_paper(self, abs_url: str) -> Optional[Dict]:
        """Parse a single UAI paper from its MLR Press abstract page.

        All fields are extracted in a single HTTP request.
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

    # ==================================================================
    # Legacy scraping (2015-2018, auai.org)
    # ==================================================================

    def _scrape_year_legacy(self, year: int, download_pdfs: bool, resume: bool) -> List[Dict]:
        """Scrape UAI from the auai.org single-page format."""
        url = _LEGACY_URLS.get(year)
        if not url:
            logger.error(f"No URL defined for UAI {year}")
            return []

        logger.info(f"Scraping UAI {year} (legacy) from {url}")

        response = self.session.get(url)
        if not response:
            logger.error(f"Failed to fetch {url}")
            return []

        soup = BeautifulSoup(response.content, 'html.parser')
        papers = self._legacy_extract_all_papers(soup, year, url)
        logger.info(f"Found {len(papers)} papers for UAI {year}")

        if download_pdfs:
            for paper in papers:
                if not self.download_pdf(paper, year):
                    logger.warning(f"Failed to download PDF for paper {paper.get('id', 'unknown')}")

        save_papers(papers, self.conference, year)
        return papers

    def _legacy_extract_all_papers(self, soup: BeautifulSoup, year: int, page_url: str) -> List[Dict]:
        """Extract all papers from a UAI legacy accepted-papers page."""
        papers = []
        seen_titles = set()

        tr_tags = soup.find_all('tr')
        logger.info(f"Found {len(tr_tags)} table rows for UAI {year}")

        for tr in tr_tags:
            paper = self._legacy_extract_paper_from_row(tr, year, page_url)
            if not paper or not paper.get('title'):
                continue
            title = paper['title']
            if title in seen_titles:
                logger.debug(f"Skipping duplicate: {title!r}")
                continue
            papers.append(paper)
            seen_titles.add(title)
            logger.debug(f"Parsed: {title!r} ({len(paper['authors'])} authors)")

        return papers

    def _legacy_extract_paper_from_row(self, tr, year: int, page_url: str) -> Optional[Dict]:
        """Extract paper data from a single table row."""
        try:
            title = self._legacy_extract_title(tr, year)
            if not title:
                return None

            return {
                'id':         self._legacy_extract_id(tr, year),
                'title':      title,
                'authors':    self._legacy_extract_authors(tr, year),
                'abstract':   self._legacy_extract_abstract(tr, year),
                'pdf_url':    self._legacy_extract_pdf_url(tr, year),
                'year':       year,
                'conference': 'UAI',
                'url':        page_url,
            }
        except Exception as e:
            logger.error(f"Error extracting paper from row: {e}")
            return None

    def _legacy_extract_title(self, tr, year: int) -> str:
        if year in _LEGACY_PRE2017_YEARS:
            td_tags = tr.find_all('td')
            if len(td_tags) > 1:
                div = td_tags[1].find('div')
                if div:
                    b = div.find('b')
                    if b:
                        return b.get_text(strip=True)
        else:
            for h4 in tr.find_all('h4'):
                if h4.find('p', class_='text-info'):
                    continue  # skip award banners
                title = h4.get_text(strip=True)
                if len(title) > 3:
                    return title
        return ""

    def _legacy_extract_authors(self, tr, year: int) -> List[str]:
        if year in _LEGACY_PRE2017_YEARS:
            i_tag = tr.find('i')
            if i_tag:
                raw = i_tag.get_text(strip=True)
                authors = []
                for entry in (a.strip() for a in raw.split(';') if a.strip()):
                    name = entry.split(',', 1)[0].strip()
                    if len(name) > 2:
                        authors.append(name)
                return authors
        else:
            h4 = next(
                (h for h in tr.find_all('h4') if not h.find('p', class_='text-info')),
                None
            )
            if h4 and h4.next_sibling:
                raw = h4.next_sibling.strip()
                return [a.strip() for a in raw.split(',') if len(a.strip()) > 2]
        return []

    def _legacy_extract_abstract(self, tr, year: int) -> str:
        collapse_div = tr.find('div', class_='collapse')
        if not collapse_div:
            return ""
        if year in _LEGACY_PRE2017_YEARS:
            nested = collapse_div.find('div')
            text = nested.get_text(strip=True) if nested else ""
        else:
            text = collapse_div.get_text(strip=True)
        return text if len(text) > 3 else ""

    def _legacy_extract_pdf_url(self, tr, year: int) -> str:
        td = tr.find('td')
        if not td:
            return ""
        a = td.find('a', href=True)
        if not a:
            return ""
        href = a['href']
        if year in _LEGACY_PRE2017_YEARS:
            return urljoin(f"https://www.auai.org/uai{year}/", href)
        return href

    def _legacy_extract_id(self, tr, year: int) -> str:
        if year in _LEGACY_PRE2017_YEARS:
            td = tr.find('td')
            if td:
                b = td.find('b')
                if b:
                    m = re.search(r'ID:\s*(\d+)', b.get_text(strip=True))
                    if m:
                        return m.group(1)
        else:
            h5 = tr.find('h5')
            if h5:
                m = re.search(r'ID:\s*(\d+)', h5.get_text(strip=True))
                if m:
                    return m.group(1)
        return ""

    # ==================================================================
    # MLR Press helpers (2019+)
    # ==================================================================

    def _get_volume_for_year(self, year: int) -> Optional[str]:
        """Return the MLR Press volume identifier (e.g. 'v286') for a given UAI year."""
        if year in self._volume_cache:
            return self._volume_cache[year]

        logger.info(f"Finding UAI volume for year {year}...")

        response = self.session.get(self.base_url)
        if not response:
            logger.error("Failed to fetch MLR Press main page")
            return None

        soup = BeautifulSoup(response.content, 'html.parser')

        uai_pattern = re.compile(
            rf'\b(?:Proceedings\s+of.*?UAI\s+{year}|UAI\s+{year}.*?Proceedings)\b',
            re.IGNORECASE
        )

        for li in soup.find_all('li'):
            if not uai_pattern.search(li.get_text()):
                continue
            link = li.find('a', href=True)
            if not link:
                continue
            href = link['href'].strip('/')
            if _VOLUME_HREF_RE.match(href):
                volume = href if href.startswith('v') else f"v{href}"
                self._volume_cache[year] = volume
                logger.info(f"Found UAI {year} -> {volume}")
                return volume

        logger.warning(f"No UAI volume found for year {year}")
        return None

    def _extract_paper_links(self, html: bytes) -> List[str]:
        """Extract abstract-page URLs from an MLR Press volume page."""
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
        h1 = soup.find('h1')
        return h1.get_text(strip=True) if h1 else ""

    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        authors_span = soup.find('span', class_='authors')
        if not authors_span:
            return []
        raw = authors_span.get_text(separator=' ', strip=True).replace('\xa0', ' ')
        return [a.strip() for a in raw.split(',') if a.strip()]

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        div = soup.find('div', id='abstract', class_='abstract') or soup.find('div', class_='abstract')
        return div.get_text(strip=True) if div else ""

    def _extract_paper_id(self, abs_url: str) -> str:
        m = re.search(r'/v\d+/([^/]+)\.html$', abs_url)
        return m.group(1) if m else abs_url.split('/')[-1].replace('.html', '')

    def _extract_pdf_url(self, soup: BeautifulSoup, abs_url: str) -> str:
        for a in soup.find_all('a', href=True):
            text = a.get_text(strip=True)
            href = a['href']
            if ('Download PDF' in text or text.lower() == 'pdf') and href.endswith('.pdf'):
                return urljoin(self.base_url, href)
        return ""