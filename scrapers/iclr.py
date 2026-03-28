"""ICLR scraper implementation.

Covers all years from 2015 onwards via three internal strategies,
selected automatically by year:

  2015–2016  iclr.cc static archive pages + arXiv for abstracts
  2017–2025  OpenReview API (api.openreview.net / api2.openreview.net)
  2019       iclr.cc/Downloads JSON + virtualsite pages
             (faster than per-paper API for 2019: ~1 500 requests vs ~5 000)

Cache: data/cache/iclr_papers.json  (keyed by year string)
All strategies populate the same cache format so results are interchangeable.
"""

import json
import os
import re
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional

from .base import BaseScraper
from config import CACHE_DIR

logger = logging.getLogger(__name__)

# -- OpenReview API bases ------------------------------------------------------
_API1 = "https://api.openreview.net"
_API2 = "https://api2.openreview.net"

# -- iclr.cc endpoints ---------------------------------------------------------
_DOWNLOADS_URL = "https://iclr.cc/Downloads/{year}"
_ARCHIVE_2015  = "https://iclr.cc/archive/www/doku.php%3Fid=iclr2015:accepted-main.html"
_ARCHIVE_2016  = "https://iclr.cc/archive/www/doku.php%3Fid=iclr2016:accepted-main.html"

# -- Shared cache --------------------------------------------------------------
_CACHE_PATH = CACHE_DIR / "iclr_papers.json"

# -- Regex ---------------------------------------------------------------------
_FORUM_RE    = re.compile(r'openreview\.net/forum\?id=([^"&\s]+)')
_ARXIV_ID_RE = re.compile(r'arxiv\.org/abs/(\d+\.\d+)')

# -- Accepted venue keywords ---------------------------------------------------
_ACCEPTED_VENUES = {"oral", "spotlight", "poster"}

# -- Per-year API config (2017-2025) ------------------------------------------
_YEAR_CONFIG = {
    # Group A: venue field in submission note
    2017: {
        "strategy":   "venue",
        "api":        _API1,
        "invitation": "ICLR.cc/2017/conference/-/submission",
    },
    2022: {
        "strategy":   "venue",
        "api":        _API1,
        "invitation": "ICLR.cc/2022/Conference/-/Blind_Submission",
    },
    2023: {
        "strategy":   "venue",
        "api":        _API1,
        "invitation": "ICLR.cc/2023/Conference/-/Blind_Submission",
    },

    # Group B: bulk decision notes
    2018: {
        "strategy":       "bulk_decision",
        "api":            _API1,
        "invitation":     "ICLR.cc/2018/Conference/-/Blind_Submission",
        "decision_inv":   "ICLR.cc/2018/Conference/-/Acceptance_Decision",
        "decision_field": "decision",
    },

    # Group C: per-paper decision notes
    2020: {
        "strategy":          "per_paper_decision",
        "api":               _API1,
        "invitation":        "ICLR.cc/2020/Conference/-/Blind_Submission",
        "decision_inv_tmpl": "ICLR.cc/2020/Conference/Paper{num}/-/Decision",
        "decision_field":    "decision",
    },

    # Group D: mixed (venue field for some, per-paper for others)
    2021: {
        "strategy":          "mixed",
        "api":               _API1,
        "invitation":        "ICLR.cc/2021/Conference/-/Blind_Submission",
        "decision_inv_tmpl": "ICLR.cc/2021/Conference/Paper{num}/-/Decision",
        "decision_field":    "decision",
    },

    # Group E: v2 API, filter by venueid directly
    2024: {
        "strategy": "venueid",
        "api":      _API2,
        "venueid":  "ICLR.cc/2024/Conference",
    },
    2025: {
        "strategy": "venueid",
        "api":      _API2,
        "venueid":  "ICLR.cc/2025/Conference",
    },
}

# 2019 uses iclr.cc/Downloads (faster than per-paper API)
_DOWNLOADS_YEARS = {2019}

# 2015-2016 use static iclr.cc archive pages
_ARCHIVE_YEARS = {2015, 2016}
_ARCHIVE_URLS  = {
    2015: _ARCHIVE_2015,
    2016: _ARCHIVE_2016,
}


class ICLRScraper(BaseScraper):
    """ICLR conference scraper (2015-2025).

    Routing:
      2015-2016 → _strategy_archive (iclr.cc static pages + arXiv abstracts)
      2019      → _strategy_downloads (iclr.cc Downloads JSON + virtualsite)
      others    → OpenReview API strategies (see _YEAR_CONFIG)
    """

    NAME = "ICLR"
    BASE_URL = "https://iclr.cc/"
    REQUEST_DELAY = 0.15
    TIMEOUT = 5

    def __init__(self):
        super().__init__('iclr')
        self._paper_cache: Dict[str, Dict] = {}
        self._archive_cache: Dict[str, Dict] = {}
        self._openreview_token: Optional[str] = self._login_openreview()

    def _login_openreview(self) -> Optional[str]:
        """Login to OpenReview and return a bearer token.

        Reads OPENREVIEW_USERNAME and OPENREVIEW_PASSWORD from the environment.
        Required for accessing older conference data (pre-2024).
        """
        from dotenv import load_dotenv
        load_dotenv()

        username = os.getenv("OPENREVIEW_USERNAME")
        password = os.getenv("OPENREVIEW_PASSWORD")
        if not username or not password:
            logger.warning("OPENREVIEW_USERNAME/PASSWORD not set — API access may be limited.")
            return None

        for api_base in [_API2, _API1]:
            try:
                resp = self.session.session.post(
                    f"{api_base}/login",
                    json={"id": username, "password": password},
                    timeout=30,
                )
                if resp.status_code == 200:
                    token = resp.json().get("token")
                    if token:
                        logger.info(f"Logged in to OpenReview via {api_base}")
                        return token
            except Exception as e:
                logger.warning(f"Login failed via {api_base}: {e}")

        logger.error("Could not log in to OpenReview — API requests may be forbidden.")
        return None

    # --------------------------------------------------------------------------
    # Public interface
    # --------------------------------------------------------------------------

    def get_paper_urls(self, year: int) -> List[str]:
        """Return paper URLs for all accepted ICLR papers in `year`."""
        supported = _ARCHIVE_YEARS | _DOWNLOADS_YEARS | set(_YEAR_CONFIG)
        if year not in supported:
            logger.error(f"ICLR {year} not supported. Available: {sorted(supported)}")
            return []

        logger.info(f"Getting ICLR {year} paper URLs...")
        papers = self._get_papers(year)
        if not papers:
            logger.error(f"No accepted papers found for ICLR {year}")
            return []

        for p in papers:
            if year in _ARCHIVE_YEARS:
                self._archive_cache[p["url"]] = p
            else:
                self._paper_cache[p["id"]] = p

        urls = [p["url"] if year in _ARCHIVE_YEARS else p["openreview_url"] for p in papers]
        logger.info(f"Found {len(urls)} papers for ICLR {year}")
        return urls

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single ICLR paper by URL."""
        # 2015-2016: arXiv URL
        if "arxiv.org" in url:
            return self._parse_paper_archive(url)

        # 2017-2025: OpenReview forum URL
        paper_id = self._extract_paper_id(url)
        if not paper_id:
            logger.warning(f"Could not extract paper ID from {url}")
            return None

        # Fast path: in memory
        if paper_id in self._paper_cache:
            return self._paper_cache[paper_id]

        # Slow path: disk cache
        cache = self._load_cache()
        for year_papers in cache.values():
            for p in year_papers:
                if p.get("id") == paper_id:
                    return p

        # Final fallback: fetch from API
        for api_base in [_API2, _API1]:
            data = self._api_get(api_base, {"id": paper_id})
            if data and data.get("notes"):
                return self._note_to_paper(data["notes"][0], url)

        logger.error(f"Could not fetch note for {paper_id}")
        return None

    # --------------------------------------------------------------------------
    # Cache
    # --------------------------------------------------------------------------

    def _get_papers(self, year: int) -> List[Dict]:
        """Return full paper dicts, using disk cache when available."""
        cache = self._load_cache()
        if str(year) in cache:
            papers = cache[str(year)]
            logger.info(f"ICLR {year}: loaded {len(papers)} papers from cache")
            return papers

        if year in _ARCHIVE_YEARS:
            papers = self._strategy_archive(year)
        elif year in _DOWNLOADS_YEARS:
            papers = self._strategy_downloads(year)
        else:
            notes  = self._fetch_accepted_notes(year)
            papers = [
                self._note_to_paper(n, f"https://openreview.net/forum?id={n['id']}")
                for n in notes
            ]

        if papers:
            cache[str(year)] = papers
            self._save_cache(cache)

        return papers

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

    # --------------------------------------------------------------------------
    # Strategy: 2015-2016 (iclr.cc static archive + arXiv)
    # --------------------------------------------------------------------------

    def _strategy_archive(self, year: int) -> List[Dict]:
        """Scrape iclr.cc static archive page; fetch abstracts from arXiv."""
        url = _ARCHIVE_URLS[year]
        response = self.session.get(url)
        if not response:
            logger.error(f"Failed to fetch {url}")
            return []

        soup = BeautifulSoup(response.content, 'html.parser')
        papers = []
        seen_titles: set = set()

        page_div = soup.find('div', class_='page')
        if not page_div:
            logger.error("Could not find div.page in archive page")
            return []

        for header in page_div.find_all('h3'):
            track_name = header.get_text(strip=True)
            level3_div = None
            current = header
            while current:
                current = current.find_next_sibling()
                if current and current.name == 'div' and 'level3' in (current.get('class') or []):
                    level3_div = current
                    break
                elif current and current.name == 'h3':
                    break

            if not level3_div:
                continue

            for item in level3_div.find_all('li', class_='level1'):
                paper = self._parse_archive_item(item, track_name, year)
                if not paper:
                    continue
                if paper['title'] in seen_titles:
                    logger.debug(f"Duplicate skipped: {paper['title']}")
                    continue
                seen_titles.add(paper['title'])
                papers.append(paper)

        logger.info(f"Found {len(papers)} papers for ICLR {year}")
        return papers

    def _parse_archive_item(self, item, track_name: str, year: int) -> Optional[Dict]:
        try:
            li_div = item.find('div', class_='li')
            if not li_div:
                return None

            arxiv_link = None
            for link in li_div.find_all('a', href=True):
                href = link.get('href', '')
                if 'arxiv.org/abs/' in href:
                    link_text = link.get_text(strip=True)
                    if not (link_text.startswith('[') and link_text.endswith(']')):
                        arxiv_link = link
                        break

            if not arxiv_link:
                logger.warning(f"No arXiv link in: {li_div.get_text()[:100]}")
                return None

            title     = arxiv_link.get_text(strip=True)
            arxiv_url = arxiv_link.get('href')
            arxiv_id  = self._extract_arxiv_id(arxiv_url)
            if not arxiv_id:
                logger.warning(f"Could not extract arXiv ID from: {arxiv_url}")
                return None

            text_after = ""
            current = arxiv_link
            while current:
                if current.next_sibling:
                    current = current.next_sibling
                    if hasattr(current, 'get_text'):
                        text_after += current.get_text()
                    elif isinstance(current, str):
                        text_after += current
                else:
                    break

            authors = self._parse_archive_authors(text_after)

            return {
                'id':       arxiv_id,
                'url':      arxiv_url,
                'title':    title,
                'authors':  authors,
                'abstract': "",   # filled in parse_paper via arXiv
                'track':    track_name,
                'pdf_url':  f"https://arxiv.org/pdf/{arxiv_id}.pdf",
                'year':     year,
            }
        except Exception as e:
            logger.error(f"Error parsing archive item: {e}")
            return None

    def _parse_archive_authors(self, text: str) -> List[str]:
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'^[,\s]+', '', text)
        if not text:
            return []
        if ' and ' in text:
            if ',' in text:
                parts = text.rsplit(' and ', 1)
                text = parts[0] + ', ' + parts[1]
            else:
                return [a.strip() for a in text.split(' and ') if a.strip()]
        return [a.strip() for a in text.split(',') if a.strip()]

    def _parse_paper_archive(self, url: str) -> Optional[Dict]:
        """parse_paper for 2015-2016: return cached paper + fetch abstract from arXiv."""
        if url in self._archive_cache:
            paper = self._archive_cache[url].copy()
        else:
            # Check disk cache
            cache = self._load_cache()
            paper = None
            for year_papers in cache.values():
                for p in year_papers:
                    if p.get('url') == url:
                        paper = p.copy()
                        break
                if paper:
                    break
            if not paper:
                logger.warning(f"Paper not found in cache: {url}")
                return None

        if not paper.get('abstract'):
            paper['abstract'] = self._fetch_arxiv_abstract(paper.get('id', ''))

        logger.debug(f"Parsed: {paper['title']!r} ({len(paper['authors'])} authors)")
        return paper

    def _fetch_arxiv_abstract(self, arxiv_id: str) -> str:
        try:
            resp = self.session.get(f"https://arxiv.org/abs/{arxiv_id}")
            if not resp:
                return ""
            soup = BeautifulSoup(resp.content, 'html.parser')
            block = soup.find('blockquote', class_='abstract')
            if block:
                descriptor = block.find('span', class_='descriptor')
                if descriptor:
                    descriptor.decompose()
                return block.get_text(strip=True)
        except Exception as e:
            logger.error(f"Error fetching arXiv abstract for {arxiv_id}: {e}")
        return ""

    # --------------------------------------------------------------------------
    # Strategy: 2019 (iclr.cc Downloads JSON + virtualsite pages)
    # --------------------------------------------------------------------------

    def _strategy_downloads(self, year: int) -> List[Dict]:
        """Fetch accepted paper list from iclr.cc/Downloads, resolve forum IDs."""
        url = _DOWNLOADS_URL.format(year=year)

        logger.info(f"GET {url}")
        resp = self.session.get(url)
        if not resp:
            logger.error(f"Failed to load Downloads page for {year}")
            return []

        token_match = re.search(
            r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', resp.text
        )
        if not token_match:
            logger.error("Could not find CSRF token")
            return []
        token = token_match.group(1)

        logger.info(f"POST {url} (Download Data)")
        raw_resp = self.session.post(
            url,
            data={
                "csrfmiddlewaretoken": token,
                "file_format":        "5",
                "posters":            "on",
                "submitaction":       "Download Data",
            },
            headers={"Referer": url},
            timeout=60,
        )
        if not raw_resp:
            logger.error("POST request failed")
            return []

        try:
            raw_papers = raw_resp.json()
        except Exception as e:
            logger.error(f"Failed to parse Downloads JSON: {e}")
            return []

        logger.info(f"Downloaded {len(raw_papers)} records from iclr.cc")

        papers = []
        failed = 0
        for i, raw in enumerate(raw_papers):
            if (i + 1) % 100 == 0:
                logger.info(f"[{i+1}/{len(raw_papers)}] {len(papers)} forum IDs found...")

            forum_id = self._extract_forum_id_from_virtualsite(raw.get("virtualsite_url", ""))
            if not forum_id:
                failed += 1
                logger.warning(f"No forum ID for: {raw.get('name', '?')[:60]}")
                continue

            authors_str = raw.get("speakers/authors", "")
            authors = [a.strip() for a in authors_str.split(",") if a.strip()]
            papers.append({
                "id":             forum_id,
                "title":          raw.get("name", ""),
                "authors":        authors,
                "abstract":       raw.get("abstract", ""),
                "keywords":       [],
                "pdf_url":        f"https://openreview.net/pdf?id={forum_id}",
                "openreview_url": f"https://openreview.net/forum?id={forum_id}",
            })

        logger.info(f"Done: {len(papers)} papers with forum ID, {failed} failed")
        return papers

    def _extract_forum_id_from_virtualsite(self, url: str) -> Optional[str]:
        if not url:
            return None
        resp = self.session.get(url)
        if not resp:
            return None
        m = _FORUM_RE.search(resp.text)
        return m.group(1) if m else None

    # --------------------------------------------------------------------------
    # Strategy: OpenReview API (2017-2025 except 2019)
    # --------------------------------------------------------------------------

    def _fetch_accepted_notes(self, year: int) -> List[Dict]:
        config   = _YEAR_CONFIG[year]
        strategy = config["strategy"]
        dispatch = {
            "venue":              self._strategy_venue,
            "bulk_decision":      self._strategy_bulk_decision,
            "per_paper_decision": self._strategy_per_paper_decision,
            "mixed":              self._strategy_mixed,
            "venueid":            self._strategy_venueid,
        }
        return dispatch[strategy](config)

    def _strategy_venue(self, config: Dict) -> List[Dict]:
        notes    = self._paginate(config["api"], config["invitation"])
        accepted = [
            n for n in notes
            if any(kw in str(self._val(n["content"].get("venue", ""))).lower()
                   for kw in _ACCEPTED_VENUES)
        ]
        logger.info(f"venue filter: {len(accepted)}/{len(notes)} accepted")
        return accepted

    def _strategy_bulk_decision(self, config: Dict) -> List[Dict]:
        decision_notes = self._paginate(config["api"], config["decision_inv"])
        field = config["decision_field"]
        decision_map = {
            n.get("forum", n.get("id", "")): n["content"].get(field, "")
            for n in decision_notes
        }
        logger.info(f"loaded {len(decision_map)} decision notes")
        submissions = self._paginate(config["api"], config["invitation"])
        accepted = [
            n for n in submissions
            if "accept" in decision_map.get(
                n.get("forum", n.get("id", "")), ""
            ).lower()
        ]
        logger.info(f"bulk decision: {len(accepted)}/{len(submissions)} accepted")
        return accepted

    def _strategy_per_paper_decision(self, config: Dict) -> List[Dict]:
        submissions = self._paginate(config["api"], config["invitation"])
        tmpl  = config["decision_inv_tmpl"]
        field = config["decision_field"]
        accepted = []
        for i, note in enumerate(submissions):
            num      = note.get("number")
            paper_id = note.get("id", "")
            if not num:
                continue
            if (i + 1) % 100 == 0:
                logger.info(f"[{i+1}/{len(submissions)}] {len(accepted)} accepted so far...")
            decision = self._fetch_forum_decision(
                config["api"], paper_id, tmpl.format(num=num), field
            )
            if decision and "accept" in decision.lower():
                accepted.append(note)
        logger.info(f"per-paper decision: {len(accepted)}/{len(submissions)} accepted")
        return accepted

    def _strategy_mixed(self, config: Dict) -> List[Dict]:
        submissions = self._paginate(config["api"], config["invitation"])
        tmpl  = config["decision_inv_tmpl"]
        field = config["decision_field"]
        accepted  = []
        slow_path = 0
        for i, note in enumerate(submissions):
            venue = str(self._val(note["content"].get("venue", ""))).lower()
            if venue:
                if any(kw in venue for kw in _ACCEPTED_VENUES):
                    accepted.append(note)
                continue
            num      = note.get("number")
            paper_id = note.get("id", "")
            if not num:
                continue
            slow_path += 1
            if slow_path % 100 == 0:
                logger.info(f"slow-path: {slow_path} checked, {len(accepted)} accepted so far...")
            decision = self._fetch_forum_decision(
                config["api"], paper_id, tmpl.format(num=num), field
            )
            if decision and "accept" in decision.lower():
                accepted.append(note)
        logger.info(
            f"mixed: {len(accepted)}/{len(submissions)} accepted (slow-path: {slow_path})"
        )
        return accepted

    def _strategy_venueid(self, config: Dict) -> List[Dict]:
        notes = self._paginate_v2(config["api"], config["venueid"])
        logger.info(f"venueid: {len(notes)} accepted")
        return notes

    # --------------------------------------------------------------------------
    # Pagination
    # --------------------------------------------------------------------------

    def _paginate(self, api_base: str, invitation: str, limit: int = 1000) -> List[Dict]:
        all_notes: List[Dict] = []
        offset = 0
        while True:
            data = self._api_get(api_base, {
                "invitation": invitation,
                "limit":      limit,
                "offset":     offset,
            })
            if not data:
                break
            batch = data.get("notes", [])
            if not batch:
                break
            all_notes.extend(batch)
            logger.info(f"... {len(all_notes)} notes fetched")
            if len(batch) < limit:
                break
            offset += limit
        return all_notes

    def _paginate_v2(self, api_base: str, venueid: str, limit: int = 1000) -> List[Dict]:
        """Paginate v2 API by venueid. count field is unreliable — stop on empty batch."""
        all_notes: List[Dict] = []
        offset = 0
        while True:
            data = self._api_get(api_base, {
                "content.venueid": venueid,
                "limit":           limit,
                "offset":          offset,
            })
            if not data:
                break
            batch = data.get("notes", [])
            if not batch:
                break
            all_notes.extend(batch)
            logger.info(f"... {len(all_notes)} notes fetched")
            if len(batch) < limit:
                break
            offset += limit
        return all_notes

    # --------------------------------------------------------------------------
    # Decision fetching
    # --------------------------------------------------------------------------

    def _fetch_forum_decision(
        self, api_base: str, paper_id: str, decision_invitation: str, field: str
    ) -> Optional[str]:
        data = self._api_get(api_base, {"forum": paper_id})
        if not data:
            return None
        for note in data.get("notes", []):
            if note.get("invitation") == decision_invitation:
                return note.get("content", {}).get(field)
        return None

    # --------------------------------------------------------------------------
    # HTTP
    # --------------------------------------------------------------------------

    def _api_get(self, base: str, params: Dict) -> Optional[Dict]:
        url     = base + "/notes"
        headers = {"Authorization": f"Bearer {self._openreview_token}"} if self._openreview_token else {}
        resp    = self.session.get(url, params=params, headers=headers)
        if resp is None:
            logger.error(f"Request failed: {url} params={params}")
            return None
        try:
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to parse JSON from {url}: {e}")
            return None

    # --------------------------------------------------------------------------
    # Note → paper dict
    # --------------------------------------------------------------------------

    def _note_to_paper(self, note: Dict, url: str) -> Dict:
        content  = note.get("content", {})
        paper_id = note.get("id", "")
        return {
            "id":             paper_id,
            "title":          self._val(content.get("title",    "")),
            "authors":        self._val(content.get("authors",  [])),
            "abstract":       self._val(content.get("abstract", "")),
            "keywords":       self._val(content.get("keywords", [])),
            "pdf_url":        f"https://openreview.net/pdf?id={paper_id}",
            "openreview_url": url,
        }

    # --------------------------------------------------------------------------
    # Utilities
    # --------------------------------------------------------------------------

    @staticmethod
    def _val(x):
        """Unwrap v2 content values wrapped as {'value': ...}."""
        return x.get("value", x) if isinstance(x, dict) else x

    @staticmethod
    def _extract_paper_id(url: str) -> Optional[str]:
        m = re.search(r"[?&]id=([^&]+)", url)
        return m.group(1) if m else None

    @staticmethod
    def _extract_arxiv_id(arxiv_url: str) -> Optional[str]:
        m = _ARXIV_ID_RE.search(arxiv_url)
        return m.group(1) if m else None