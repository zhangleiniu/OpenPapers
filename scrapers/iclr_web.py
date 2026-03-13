import json
import os
import re
import logging
from typing import List, Dict, Optional

from .base import BaseScraper

logger = logging.getLogger(__name__)

# iclr.cc endpoints
_DOWNLOADS_URL  = "https://iclr.cc/Downloads/{year}"

# OpenReview APIs (fallback for 2020)
_API1 = "https://api.openreview.net"
_API2 = "https://api2.openreview.net"

# Regex to extract OpenReview forum ID from virtualsite HTML
_FORUM_RE = re.compile(r'openreview\.net/forum\?id=([^"&\s]+)')

# Shared cache with iclr.py: {year: [{id, title, authors, ...}, ...]}
_CACHE_PATH = "data/cache/iclr_papers.json"

# Years supported by iclr.cc/Downloads
_DOWNLOADS_YEARS = {2018, 2019, 2021, 2022, 2023, 2024, 2025}

# 2020 needs API fallback (virtualsite pages have no OpenReview link)
_API_FALLBACK_YEARS = {2020}

# 2020 API config
_2020_CONFIG = {
    "api":               _API1,
    "invitation":        "ICLR.cc/2020/Conference/-/Blind_Submission",
    "decision_inv_tmpl": "ICLR.cc/2020/Conference/Paper{num}/-/Decision",
    "decision_field":    "decision",
    "venueid":           None,
}

_ACCEPTED_KEYWORDS = {"oral", "spotlight", "poster"}


class ICLRWebScraper(BaseScraper):
    """ICLR scraper using iclr.cc/Downloads + virtualsite pages (2018-2025).

    Data flow:
      1. POST iclr.cc/Downloads/{year} → JSON with title/authors/abstract/virtualsite_url
      2. GET each virtualsite_url → extract OpenReview forum ID via regex
      3. Compose pdf_url as https://openreview.net/pdf?id={forum_id}

    Cache: data/cache/iclr_papers.json  (shared with iclr.py, keyed by year string)
    Exception: 2020 falls back to OpenReview API (no forum links on virtualsite pages).
    """

    def __init__(self):
        super().__init__('iclr')
        # In-memory paper cache: paper_id → full paper dict
        # Populated by get_paper_urls so parse_paper needs zero API calls
        self._paper_cache: Dict[str, Dict] = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────────────────

    def get_paper_urls(self, year: int) -> List[str]:
        if year not in _DOWNLOADS_YEARS | _API_FALLBACK_YEARS:
            logger.error(f"ICLRWebScraper: year {year} not supported")
            return []

        logger.info(f"Getting ICLR {year} paper URLs (web scraper)...")
        papers = self._get_papers(year)
        if not papers:
            logger.error(f"No papers found for ICLR {year}")
            return []

        # Populate in-memory cache for parse_paper
        for p in papers:
            self._paper_cache[p["id"]] = p

        urls = [p["openreview_url"] for p in papers]
        logger.info(f"ICLR {year}: {len(urls)} accepted papers")
        return urls

    def parse_paper(self, url: str) -> Optional[Dict]:
        paper_id = self._extract_paper_id(url)
        if not paper_id:
            logger.warning(f"Could not extract paper ID from {url}")
            return None

        # Fast path: already in memory from get_paper_urls
        if paper_id in self._paper_cache:
            return self._paper_cache[paper_id]

        # Slow path: check disk cache
        cache = self._load_cache()
        for year_papers in cache.values():
            for p in year_papers:
                if p.get("id") == paper_id:
                    return p

        logger.warning(f"Paper {paper_id} not in cache; run get_paper_urls first")
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Cache
    # ──────────────────────────────────────────────────────────────────────────

    def _get_papers(self, year: int) -> List[Dict]:
        """Return full paper dicts, using disk cache when available."""
        cache = self._load_cache()
        if str(year) in cache:
            papers = cache[str(year)]
            logger.info(f"ICLR {year}: loaded {len(papers)} papers from cache")
            return papers

        if year in _API_FALLBACK_YEARS:
            papers = self._fetch_via_api(year)
        else:
            papers = self._fetch_via_downloads(year)

        if papers:
            cache[str(year)] = papers
            self._save_cache(cache)

        return papers

    def _load_cache(self) -> dict:
        if os.path.exists(_CACHE_PATH):
            with open(_CACHE_PATH) as f:
                return json.load(f)
        return {}

    def _save_cache(self, cache: dict) -> None:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved cache → {_CACHE_PATH}")

    # ──────────────────────────────────────────────────────────────────────────
    # Main fetch: Downloads JSON + virtualsite pages
    # ──────────────────────────────────────────────────────────────────────────

    def _fetch_via_downloads(self, year: int) -> List[Dict]:
        url = _DOWNLOADS_URL.format(year=year)

        # Step 1: GET page, extract CSRF token
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

        # Step 2: POST to download JSON
        logger.info(f"POST {url} (Download Data)")
        # RobustSession only has .get(); use the underlying requests.Session for POST
        raw_resp = self.session.session.post(
            url,
            data={
                "csrfmiddlewaretoken": token,
                "file_format": "5",
                "posters": "on",
                "submitaction": "Download Data",
            },
            headers={"Referer": url},
            timeout=60,
        )
        if raw_resp.status_code != 200:
            logger.error(f"POST failed: {raw_resp.status_code}")
            return []

        try:
            raw_papers = raw_resp.json()
        except Exception as e:
            logger.error(f"Failed to parse Downloads JSON: {e}")
            return []

        logger.info(f"  Downloaded {len(raw_papers)} records")

        # Step 3: extract forum ID from each virtualsite page
        papers = []
        failed = 0
        for i, raw in enumerate(raw_papers):
            if (i + 1) % 100 == 0:
                logger.info(f"  [{i+1}/{len(raw_papers)}] {len(papers)} forum IDs found...")

            virtualsite_url = raw.get("virtualsite_url", "")
            forum_id = self._extract_forum_id_from_virtualsite(virtualsite_url)
            if not forum_id:
                failed += 1
                logger.warning(f"  No forum ID for: {raw.get('name', '?')[:60]}")
                continue

            papers.append(self._build_paper(raw, forum_id))

        logger.info(
            f"  Done: {len(papers)} papers with forum ID, {failed} failed"
        )
        return papers

    def _extract_forum_id_from_virtualsite(self, url: str) -> Optional[str]:
        if not url:
            return None
        resp = self.session.get(url)
        if not resp:
            return None
        m = _FORUM_RE.search(resp.text)
        return m.group(1) if m else None

    def _build_paper(self, raw: Dict, forum_id: str) -> Dict:
        authors_str = raw.get("speakers/authors", "")
        # Authors are comma-separated; split carefully
        authors = [a.strip() for a in authors_str.split(",") if a.strip()]
        return {
            "id":             forum_id,
            "title":          raw.get("name", ""),
            "authors":        authors,
            "abstract":       raw.get("abstract", ""),
            "keywords":       [],
            "pdf_url":        f"https://openreview.net/pdf?id={forum_id}",
            "openreview_url": f"https://openreview.net/forum?id={forum_id}",
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 2020 fallback: OpenReview API (per-paper decision)
    # ──────────────────────────────────────────────────────────────────────────

    def _fetch_via_api(self, year: int) -> List[Dict]:
        """2020 only: per-paper decision via OpenReview API."""
        logger.info(f"ICLR {year}: using OpenReview API fallback (virtualsite has no forum links)")
        cfg   = _2020_CONFIG
        notes = self._paginate(cfg["api"], cfg["invitation"])
        tmpl  = cfg["decision_inv_tmpl"]
        field = cfg["decision_field"]
        papers = []

        for i, note in enumerate(notes):
            num      = note.get("number")
            paper_id = note.get("id", "")
            if not num:
                continue
            if (i + 1) % 100 == 0:
                logger.info(f"  [{i+1}/{len(notes)}] {len(papers)} accepted so far...")

            decision = self._fetch_forum_decision(
                cfg["api"], paper_id, tmpl.format(num=num), field
            )
            if decision and "accept" in decision.lower():
                papers.append(self._note_to_paper(note))

        logger.info(f"  2020 API fallback: {len(papers)}/{len(notes)} accepted")
        return papers

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
            logger.info(f"    ... {len(all_notes)} notes fetched")
            if len(batch) < limit:
                break
            offset += limit
        return all_notes

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

    def _api_get(self, base: str, params: Dict) -> Optional[Dict]:
        resp = self.session.get(base + "/notes", params=params)
        if not resp:
            return None
        try:
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to parse API JSON: {e}")
            return None

    def _note_to_paper(self, note: Dict) -> Dict:
        content  = note.get("content", {})
        paper_id = note.get("id", "")
        pdf = content.get("pdf", "")
        if pdf and not str(pdf).startswith("http"):
            pdf = f"https://openreview.net{pdf}"
        return {
            "id":             paper_id,
            "title":          content.get("title",    ""),
            "authors":        content.get("authors",  []),
            "abstract":       content.get("abstract", ""),
            "keywords":       content.get("keywords", []),
            "pdf_url":        pdf,
            "openreview_url": f"https://openreview.net/forum?id={paper_id}",
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Utilities
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_paper_id(url: str) -> Optional[str]:
        m = re.search(r"[?&]id=([^&]+)", url)
        return m.group(1) if m else None