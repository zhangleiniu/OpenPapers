"""ICCV scraper implementation.

HTML structure of openaccess.thecvf.com (verified):

  Listing page: https://openaccess.thecvf.com/ICCV{year}?day=all
    <dt>
      <a href="/content/...html">Paper Title</a>
    </dt>

  2019: papers are split across daily URLs:
    https://openaccess.thecvf.com/ICCV2019?day=2019-10-29
    https://openaccess.thecvf.com/ICCV2019?day=2019-10-30
    https://openaccess.thecvf.com/ICCV2019?day=2019-10-31
    https://openaccess.thecvf.com/ICCV2019?day=2019-11-01

  Paper page: https://openaccess.thecvf.com/content/.../papers/...html
    <div id="papertitle">Paper Title</div>
    <div id="authors"><b><i>Author1, Author2</i></b></div>
    <div id="abstract">Abstract text...</div>
    PDF URL is derived by replacing /html/ with /papers/ and .html with .pdf.
    Exception: ICCV 2017 uses content_ICCV_2017 (uppercase) in the PDF path.
"""

import re
import logging
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from urllib.parse import urljoin
from .base import BaseScraper
from utils import parse_bibtex_fields

logger = logging.getLogger(__name__)

# 2021+ use a single ?day=all URL; earlier years split papers across daily URLs
_YEAR_SPECIFIC_URLS = {
    2019: [
        "ICCV2019?day=2019-10-29",
        "ICCV2019?day=2019-10-30",
        "ICCV2019?day=2019-10-31",
        "ICCV2019?day=2019-11-01",
    ],
}


class ICCVScraper(BaseScraper):
    """ICCV conference scraper using CVF Open Access."""

    NAME = "ICCV"
    BASE_URL = "https://openaccess.thecvf.com/"
    REQUEST_DELAY = 0.1


    def __init__(self):
        super().__init__('iccv')

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_paper_urls(self, year: int) -> List[str]:
        """Return all paper-page URLs for a given ICCV year."""
        logger.info(f"Getting ICCV {year} paper URLs...")

        suffixes = _YEAR_SPECIFIC_URLS.get(year, [f"ICCV{year}?day=all"])
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

            logger.info(f"Found {len(paper_urls)} papers for ICCV {year}")
            return paper_urls

        except Exception as e:
            logger.error(f"Failed to get paper URLs: {e}")
            return []

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single ICCV paper from its CVF Open Access page."""
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
            extra = self._extract_bibtex_extra(soup)
            if extra:
                paper['bibtex_extra'] = extra

            logger.debug(f"Parsed: {title!r} ({len(authors)} authors)")
            return paper

        except Exception as e:
            logger.error(f"Failed to parse {url}: {e}")
            return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_bibtex_extra(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Pull enrichment fields from CVF's own citation block, already
        present on the paper page (no extra request needed)."""
        div = soup.find('div', class_='bibref')
        return self._bibref_div_to_extra(div) if div else {}

    @staticmethod
    def _bibref_div_to_extra(div) -> Dict[str, str]:
        fields = parse_bibtex_fields(div.get_text())
        extra = {}
        if fields.get('booktitle'):
            extra['booktitle'] = fields['booktitle']
        if fields.get('month'):
            extra['month'] = fields['month']
        if fields.get('pages'):
            extra['pages'] = fields['pages']
        return extra

    def fetch_bibtex_extra_by_id(self, year: int) -> Dict[str, Dict[str, str]]:
        """Return {paper_id: extra} for every paper in `year`'s listing
        page(s) in one request per page — the CVF listing page embeds every
        paper's full BibTeX inline, so there's no need to hit each paper's
        own page individually. Used by the metadata backfill script; live
        scraping still uses _extract_bibtex_extra since it already fetches
        each paper's page for title/authors/abstract anyway.

        Each paper is a <dt> (title) followed by *two* sibling <dd>
        elements (authors, then links/bibtex) — not one, as a naive
        find_next_sibling('dd') would assume. Walk forward through all
        following siblings until the next <dt> and check each <dd> along
        the way for the <div class="bibref"> block."""
        suffixes = _YEAR_SPECIFIC_URLS.get(year, [f"ICCV{year}?day=all"])
        result = {}
        for suffix in suffixes:
            response = self.session.get(self.base_url + suffix, quiet_404=True)
            if not response:
                continue
            soup = BeautifulSoup(response.content, 'html.parser')
            for dt in soup.find_all('dt'):
                a_tag = dt.find('a', href=True)
                if not a_tag or not a_tag.get('href'):
                    continue
                paper_id = self._extract_paper_id(a_tag['href'])
                div = None
                for sib in dt.find_next_siblings():
                    if sib.name == 'dt':
                        break
                    if sib.name == 'dd':
                        div = sib.find('div', class_='bibref')
                        if div:
                            break
                if paper_id and div:
                    result[paper_id] = self._bibref_div_to_extra(div)
        return result

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
        pdf_url = page_url.replace('/html/', '/papers/').replace('.html', '.pdf')
        # ICCV 2017 uses uppercase in the directory name
        if '_iccv_2017' in pdf_url:
            pdf_url = pdf_url.replace('content_iccv_2017', 'content_ICCV_2017')
        return pdf_url