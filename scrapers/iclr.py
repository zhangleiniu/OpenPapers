import json
import os
import re
import logging
from typing import List, Dict, Optional

import requests

from .base import BaseScraper

logger = logging.getLogger(__name__)

API1 = "https://api.openreview.net"
API2 = "https://api2.openreview.net"

# Venue strings that indicate acceptance (compared in lowercase)
_ACCEPTED_VENUES = {"oral", "spotlight", "poster"}

# Cache path: {year: [forum_id, ...]}
_OWN_CACHE_PATH = "data/cache/iclr_papers.json"

_YEAR_CONFIG = {
    # ── Group A: venue field in submission note ───────────────────────────────
    2017: {
        "api":        API1,
        "strategy":   "venue",
        "invitation": "ICLR.cc/2017/conference/-/submission",
    },
    2022: {
        "api":        API1,
        "strategy":   "venue",
        "invitation": "ICLR.cc/2022/Conference/-/Blind_Submission",
    },
    2023: {
        "api":        API1,
        "strategy":   "venue",
        "invitation": "ICLR.cc/2023/Conference/-/Blind_Submission",
    },

    # ── Group B: need decision notes ─────────────────────────────────────────
    2018: {
        "api":               API1,
        "strategy":          "bulk_decision",
        "invitation":        "ICLR.cc/2018/Conference/-/Blind_Submission",
        "decision_inv":      "ICLR.cc/2018/Conference/-/Acceptance_Decision",
        "decision_field":    "decision",
    },
    2019: {
        "api":               API1,
        "strategy":          "per_paper_decision",
        "invitation":        "ICLR.cc/2019/Conference/-/Blind_Submission",
        "decision_inv_tmpl": "ICLR.cc/2019/Conference/-/Paper{num}/Meta_Review",
        "decision_field":    "recommendation",
    },
    2020: {
        "api":               API1,
        "strategy":          "per_paper_decision",
        "invitation":        "ICLR.cc/2020/Conference/-/Blind_Submission",
        "decision_inv_tmpl": "ICLR.cc/2020/Conference/Paper{num}/-/Decision",
        "decision_field":    "decision",
    },
    2021: {
        "api":               API1,
        "strategy":          "mixed",
        "invitation":        "ICLR.cc/2021/Conference/-/Blind_Submission",
        "decision_inv_tmpl": "ICLR.cc/2021/Conference/Paper{num}/-/Decision",
        "decision_field":    "decision",
    },

    # ── Group C: v2 API ───────────────────────────────────────────────────────
    2024: {
        "api":      API2,
        "strategy": "venueid",
        "venueid":  "ICLR.cc/2024/Conference",
    },
    2025: {
        "api":      API2,
        "strategy": "venueid",
        "venueid":  "ICLR.cc/2025/Conference",
    },
}


class ICLRScraper(BaseScraper):
    """ICLR conference scraper using OpenReview API (2017-2025).

    Cache lookup order for each year:
      1. data/cache/iclr_papers.json  — own cache, keyed by year string
      2. OpenReview API               — result saved to own cache
    """

    def __init__(self):
        super().__init__('iclr')
        # In-memory paper cache: paper_id → converted paper dict
        # Populated by _fetch_accepted_notes so parse_paper avoids redundant API calls
        self._paper_cache: Dict[str, Dict] = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────────────────

    def get_paper_urls(self, year: int) -> List[str]:
        """Return OpenReview forum URLs for all accepted papers in `year`."""
        if year not in _YEAR_CONFIG:
            logger.error(f"ICLR {year} not supported. Available: {sorted(_YEAR_CONFIG)}")
            return []

        logger.info(f"Getting ICLR {year} paper URLs...")
        papers = self._get_papers(year)
        if not papers:
            logger.error(f"No accepted papers found for ICLR {year}")
            return []

        for p in papers:
            self._paper_cache[p["id"]] = p

        urls = [p["openreview_url"] for p in papers]
        logger.info(f"ICLR {year}: {len(urls)} accepted papers")
        return urls

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single ICLR paper by its OpenReview forum URL."""
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

        # Final fallback: fetch from API
        for api_base in [API2, API1]:
            data = self._api_get(api_base, {"id": paper_id})
            if data and data.get("notes"):
                return self._note_to_paper(data["notes"][0], url)

        logger.error(f"Could not fetch note for {paper_id}")
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Two-level cache
    # ──────────────────────────────────────────────────────────────────────────

    def _get_papers(self, year: int) -> List[Dict]:
        """Return full paper dicts, using own cache when available."""

        # Level 1: own cache
        cache = self._load_cache()
        if str(year) in cache:
            papers = cache[str(year)]
            logger.info(f"ICLR {year}: loaded {len(papers)} papers from cache")
            return papers

        # Level 2: fetch from API
        logger.info(f"ICLR {year}: no cache found, fetching from OpenReview API...")
        notes  = self._fetch_accepted_notes(year)
        papers = [
            self._note_to_paper(n, f"https://openreview.net/forum?id={n['id']}")
            for n in notes
        ]

        cache[str(year)] = papers
        self._save_cache(cache)

        return papers

    def _load_cache(self) -> dict:
        if os.path.exists(_OWN_CACHE_PATH):
            with open(_OWN_CACHE_PATH) as f:
                return json.load(f)
        return {}

    def _save_cache(self, cache: dict) -> None:
        os.makedirs(os.path.dirname(_OWN_CACHE_PATH), exist_ok=True)
        with open(_OWN_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved cache → {_OWN_CACHE_PATH}")

    # ──────────────────────────────────────────────────────────────────────────
    # API fetching strategies
    # ──────────────────────────────────────────────────────────────────────────

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
        logger.info(f"  venue filter: {len(accepted)}/{len(notes)} accepted")
        return accepted

    def _strategy_bulk_decision(self, config: Dict) -> List[Dict]:
        decision_notes = self._paginate(config["api"], config["decision_inv"])
        field = config["decision_field"]
        decision_map = {
            n.get("forum", n.get("id", "")): n["content"].get(field, "")
            for n in decision_notes
        }
        logger.info(f"  loaded {len(decision_map)} decision notes")

        submissions = self._paginate(config["api"], config["invitation"])
        accepted = [
            n for n in submissions
            if "accept" in decision_map.get(
                n.get("forum", n.get("id", "")), ""
            ).lower()
        ]
        logger.info(f"  bulk decision: {len(accepted)}/{len(submissions)} accepted")
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
                logger.info(f"  [{i+1}/{len(submissions)}] {len(accepted)} accepted so far...")

            decision = self._fetch_forum_decision(
                config["api"], paper_id, tmpl.format(num=num), field
            )
            if decision and "accept" in decision.lower():
                accepted.append(note)

        logger.info(f"  per-paper decision: {len(accepted)}/{len(submissions)} accepted")
        return accepted

    def _strategy_mixed(self, config: Dict) -> List[Dict]:
        submissions = self._paginate(config["api"], config["invitation"])
        tmpl  = config["decision_inv_tmpl"]
        field = config["decision_field"]
        accepted  = []
        slow_path = 0

        for i, note in enumerate(submissions):
            venue = str(self._val(note["content"].get("venue", ""))).lower()

            # Fast path: venue field is populated
            if venue:
                if any(kw in venue for kw in _ACCEPTED_VENUES):
                    accepted.append(note)
                continue

            # Slow path: fetch forum decision
            num      = note.get("number")
            paper_id = note.get("id", "")
            if not num:
                continue

            slow_path += 1
            if slow_path % 100 == 0:
                logger.info(
                    f"  slow-path: {slow_path} checked, "
                    f"{len(accepted)} accepted so far..."
                )

            decision = self._fetch_forum_decision(
                config["api"], paper_id, tmpl.format(num=num), field
            )
            if decision and "accept" in decision.lower():
                accepted.append(note)

        logger.info(
            f"  mixed: {len(accepted)}/{len(submissions)} accepted "
            f"(slow-path: {slow_path})"
        )
        return accepted

    def _strategy_venueid(self, config: Dict) -> List[Dict]:
        notes = self._paginate_v2(config["api"], config["venueid"])
        logger.info(f"  venueid: {len(notes)} accepted")
        return notes

    # ──────────────────────────────────────────────────────────────────────────
    # Pagination
    # ──────────────────────────────────────────────────────────────────────────

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

    def _paginate_v2(self, api_base: str, venueid: str, limit: int = 1000) -> List[Dict]:
        """count field is unreliable in v2 — paginate until empty batch."""
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
            logger.info(f"    ... {len(all_notes)} notes fetched")
            if len(batch) < limit:
                break
            offset += limit
        return all_notes

    # ──────────────────────────────────────────────────────────────────────────
    # Decision fetching
    # ──────────────────────────────────────────────────────────────────────────

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

    # ──────────────────────────────────────────────────────────────────────────
    # HTTP
    # ──────────────────────────────────────────────────────────────────────────

    def _api_get(self, base: str, params: Dict) -> Optional[Dict]:
        """Make an API request via RobustSession (handles retries and 429 internally)."""
        url  = base + "/notes"
        resp = self.session.get(url, params=params)
        if resp is None:
            logger.error(f"Request failed: {url} params={params}")
            return None
        try:
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to parse JSON from {url}: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Paper parsing
    # ──────────────────────────────────────────────────────────────────────────

    def _note_to_paper(self, note: Dict, url: str) -> Dict:
        content  = note.get("content", {})
        paper_id = note.get("id", "")
        pdf = self._val(content.get("pdf", ""))
        if pdf and not str(pdf).startswith("http"):
            pdf = f"https://openreview.net{pdf}"
        return {
            "id":             paper_id,
            "title":          self._val(content.get("title",    "")),
            "authors":        self._val(content.get("authors",  [])),
            "abstract":       self._val(content.get("abstract", "")),
            "keywords":       self._val(content.get("keywords", [])),
            "pdf_url":        pdf,
            "openreview_url": url,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Utilities
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _val(x):
        """Unwrap v2 content values wrapped as {'value': ...}."""
        return x.get("value", x) if isinstance(x, dict) else x

    @staticmethod
    def _extract_paper_id(url: str) -> Optional[str]:
        m = re.search(r"[?&]id=([^&]+)", url)
        return m.group(1) if m else None