import re
import os
import json
import time
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from dotenv import load_dotenv

from .base import BaseScraper
from config import CACHE_DIR
from utils import create_gemini_model, llm_json_config

load_dotenv()

logger = logging.getLogger(__name__)

# ── Cache file paths ──────────────────────────────────────────────────────────
_PAGES_CACHE  = CACHE_DIR / "aaai_pages.json"   # all issues + is_main_aaai label
_TRACKS_CACHE = CACHE_DIR / "aaai_tracks.json"  # per-issue section labels

# ── Archive base URL ──────────────────────────────────────────────────────────
_ARCHIVE_BASE = "https://ojs.aaai.org/index.php/AAAI/issue/archive"

# ── System prompt: issue-level classification ─────────────────────────────────
_ISSUE_SYSTEM_PROMPT = """\
You are classifying AAAI journal issues.

For each issue, decide whether it is part of the main AAAI conference proceedings
for a given target year — i.e., it primarily contains full technical papers from
the main AAAI conference program.

Mark is_main_aaai as TRUE for:
  - Technical Tracks of AAAI (any number)
  - Special Tracks of AAAI (e.g. AI Alignment, AI for Social Impact, Senior Member
    Presentations, New Faculty Highlights, Journal Track, Safe/Robust/Responsible AI)
  - Mixed issues that bundle AAAI technical/special content with other content
    (e.g. "IAAI-25, EAAI-25, AAAI-25 Student Abstracts...") — include these so
    section-level filtering can exclude the non-AAAI parts later.

Mark is_main_aaai as FALSE for:
  - Issues from a different conference year than the target year
  - Issues that are exclusively IAAI (Innovative Applications of AI) with no
    AAAI main-track content
  - Exclusively student paper / undergraduate consortium / doctoral consortium issues
  - Exclusively demonstration or workshop issues

Respond with a JSON object only — no explanation, no markdown fences.
Schema:
{
  "issues": [
    {"title": "<exact title as given>", "is_main_aaai": true | false, "reason": "brief phrase"},
    ...
  ]
}
"""

# ── System prompt: section/track-level classification ─────────────────────────
_TRACK_SYSTEM_PROMPT = """\
You are classifying sections within an AAAI conference proceedings issue.

For each section name, decide whether it contains regular full papers from the
main AAAI conference program (is_regular_paper: true).

Mark is_regular_paper as TRUE for:
  - AAAI Technical Tracks (any topic area: machine learning, NLP, vision, etc.)
  - AAAI Special Tracks (AI Alignment, AI for Social Impact, Senior Member
    Presentations, New Faculty Highlights, Journal Track,
    Safe/Robust/Responsible AI, etc.)

Mark is_regular_paper as FALSE for:
  - IAAI (Innovative Applications of AI) tracks
  - EAAI (Educational Advances in AI) tracks
  - Student abstract or student paper tracks
  - Undergraduate or doctoral consortium tracks
  - System demonstration tracks
  - Workshop sections
  - Anything not clearly part of the main AAAI full-paper program

Respond with a JSON object only — no explanation, no markdown fences.
Schema:
{
  "tracks": [
    {"name": "<exact name as given>", "is_regular_paper": true | false, "reason": "brief phrase"},
    ...
  ]
}
"""


class AAAIScraper(BaseScraper):
    """AAAI conference scraper with dynamic issue discovery and LLM-based filtering."""

    def __init__(self):
        super().__init__('aaai')
        self.issue_model = create_gemini_model(_ISSUE_SYSTEM_PROMPT)
        self.track_model = create_gemini_model(_TRACK_SYSTEM_PROMPT)

    # ──────────────────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────────────────

    def get_paper_urls(self, year: int) -> List[str]:
        """Return all paper URLs for a given AAAI year.

        Steps:
          1. Ensure aaai_pages.json is up to date; get issues for `year`.
          2. For each relevant issue, ensure its sections are labelled in
             aaai_tracks.json.
          3. Collect paper URLs from sections marked is_regular_paper=true.
        """
        logger.info(f"Getting AAAI {year} paper URLs...")

        # ── Step 1: issue discovery ───────────────────────────────────────────
        relevant_issues = self._get_issues_for_year(year)
        if not relevant_issues:
            logger.error(f"No main-AAAI issues found for {year}.")
            return []

        logger.info(f"Relevant issues for {year}: {[i['title'] for i in relevant_issues]}")

        # ── Steps 2 & 3: section filtering + paper URL extraction ─────────────
        all_paper_urls: List[str] = []
        for issue in relevant_issues:
            issue_url = issue["url"]
            logger.info(f"Processing issue: {issue['title']}")

            regular_sections = self._get_regular_sections(issue_url)
            if regular_sections is None:
                logger.warning(f"  Could not determine sections for {issue_url}, skipping.")
                continue

            paper_urls = self._extract_paper_links_for_sections(issue_url, regular_sections)
            logger.info(f"  → {len(paper_urls)} papers from {len(regular_sections)} regular sections")
            all_paper_urls.extend(paper_urls)

        logger.info(f"Total papers found for AAAI {year}: {len(all_paper_urls)}")
        return all_paper_urls

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single AAAI paper page."""
        try:
            response = self.session.get(url)
            if not response:
                return None

            soup = BeautifulSoup(response.content, 'html.parser')

            title = self._extract_title(soup)
            if not title:
                logger.warning(f"No title found for {url}")
                return None

            authors   = self._extract_authors(soup)
            abstract  = self._extract_abstract(soup)
            issue_info = self._extract_issue_info(soup)
            section   = self._extract_section(soup)
            pdf_url   = self._extract_pdf_url(soup)
            paper_id  = self._extract_paper_id(url)

            paper = {
                'id':       paper_id,
                'title':    title,
                'authors':  authors,
                'abstract': abstract,
                'issue':    issue_info,
                'section':  section,
                'pdf_url':  pdf_url,
            }
            logger.debug(f"Parsed: {title!r} ({len(authors)} authors)")
            return paper

        except Exception as e:
            logger.error(f"Failed to parse {url}: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Level-1: issue cache  (aaai_pages.json)
    # ──────────────────────────────────────────────────────────────────────────

    def _get_issues_for_year(self, year: int) -> List[Dict]:
        """Return cached+labelled issues for `year`, updating cache if needed."""
        cache = self._load_pages_cache()

        # Check freshness: compare the first live issue URL against cache.
        live_first_url = self._fetch_first_issue_url()
        if live_first_url and cache.get("first_issue_url") == live_first_url:
            logger.info("aaai_pages.json is up to date.")
        else:
            logger.info("aaai_pages.json is stale or missing — refreshing...")
            cache = self._refresh_pages_cache(cache, live_first_url)

        return [
            i for i in cache.get("issues", [])
            if i.get("inferred_year") == year and i.get("is_main_aaai") is True
        ]

    def _fetch_first_issue_url(self) -> Optional[str]:
        """Fetch the very first issue URL on archive page 1 (freshness sentinel)."""
        try:
            resp = self.session.get(_ARCHIVE_BASE)
            if not resp:
                return None
            soup = BeautifulSoup(resp.content, "html.parser")
            first_a = soup.select_one(
                "div.page_issue_archive ul.issues_archive li div.obj_issue_summary h2 a.title"
            )
            return first_a["href"] if first_a else None
        except Exception as e:
            logger.error(f"Could not fetch archive page 1: {e}")
            return None

    def _refresh_pages_cache(self, old_cache: dict, live_first_url: Optional[str]) -> dict:
        """
        Fetch all archive pages, label any issues not yet in cache, and save.
        Already-labelled issues are preserved as-is to avoid wasting LLM calls.
        """
        known_urls = {i["url"] for i in old_cache.get("issues", [])}

        new_issues: List[Dict] = []   # issues not yet in cache (prepended = newest first)
        page = 1
        stop = False

        while not stop:
            page_url = _ARCHIVE_BASE if page == 1 else f"{_ARCHIVE_BASE}/{page}"
            logger.info(f"  Fetching archive page {page}: {page_url}")
            resp = self.session.get(page_url)
            if not resp:
                break

            soup = BeautifulSoup(resp.content, "html.parser")
            ul = soup.select_one("div.page_issue_archive ul.issues_archive")
            if not ul:
                break

            batch: List[Dict] = []
            for li in ul.find_all("li", recursive=False):
                obj = li.find("div", class_="obj_issue_summary")
                if not obj:
                    continue
                h2 = obj.find("h2")
                if not h2:
                    continue
                a = h2.find("a", class_="title")
                if not a:
                    continue

                url    = a["href"]
                title  = a.get_text(strip=True)
                series = h2.find("div", class_="series")
                series_text = series.get_text(strip=True) if series else None

                if url in known_urls:
                    # Hit the frontier — everything beyond is already cached
                    stop = True
                    break

                batch.append({
                    "title":          title,
                    "url":            url,
                    "series":         series_text,
                    "inferred_year":  self._extract_year(title, series_text),
                    "is_main_aaai":   None,   # to be filled by LLM
                })

            new_issues.extend(batch)
            if not stop:
                page += 1
                time.sleep(0.4)

        # LLM-label all new issues in one or more batched calls
        if new_issues:
            logger.info(f"  LLM-labelling {len(new_issues)} new issues...")
            self._label_issues_inplace(new_issues)

        # Rebuild cache: new issues first, then existing ones
        updated_issues = new_issues + old_cache.get("issues", [])
        new_cache = {
            "first_issue_url": live_first_url,
            "issues":          updated_issues,
        }
        self._save_pages_cache(new_cache)
        return new_cache

    def _label_issues_inplace(self, issues: List[Dict]) -> None:
        """
        Call LLM to set is_main_aaai on each issue dict.
        Batches by inferred_year so the LLM has clear year context.
        Mutates issues in-place.
        """
        if not self.issue_model:
            logger.error("Issue model not initialised — cannot label issues.")
            for i in issues:
                i["is_main_aaai"] = False
            return

        # Group by inferred_year so each LLM call targets one year
        from collections import defaultdict
        by_year: Dict[Optional[int], List[Dict]] = defaultdict(list)
        for issue in issues:
            by_year[issue["inferred_year"]].append(issue)

        for year, group in by_year.items():
            result = self._auto_label_issues(year, group)
            if result is None:
                logger.warning(f"  LLM labelling failed for year={year}; defaulting to False")
                for i in group:
                    i["is_main_aaai"] = False
                continue

            # Map label back by title
            label_map = {item["title"]: item["is_main_aaai"] for item in result}
            for issue in group:
                issue["is_main_aaai"] = label_map.get(issue["title"], False)
                logger.debug(f"  {'✓' if issue['is_main_aaai'] else '✗'} {issue['title']}")

    def _auto_label_issues(self, year: Optional[int], issues: List[Dict]) -> Optional[List[Dict]]:
        """Call Gemini to classify a batch of issues. Returns list of {title, is_main_aaai} or None."""
        user_message = (
            f"Target year: {year}\n\n"
            "Issues:\n" +
            "\n".join(
                f"- title: {i['title']!r}  series: {i.get('series') or 'n/a'}"
                for i in issues
            )
        )
        try:
            response = self.issue_model.generate_content(user_message, generation_config=llm_json_config())
            if not response.text:
                return None
            parsed = json.loads(response.text.strip())
            return parsed.get("issues")
        except Exception as e:
            logger.error(f"Issue LLM call failed for year={year}: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Level-2: section/track cache  (aaai_tracks.json)
    # ──────────────────────────────────────────────────────────────────────────

    def _get_regular_sections(self, issue_url: str) -> Optional[List[str]]:
        """
        Return the list of section names that are regular AAAI papers for this issue.
        Loads from aaai_tracks.json if already labelled; otherwise fetches + labels + saves.
        Returns None on hard failure.
        """
        tracks_cache = self._load_tracks_cache()

        if issue_url not in tracks_cache:
            logger.info(f"  Labelling sections for {issue_url}...")
            section_names = self._fetch_section_names(issue_url)
            if section_names is None:
                return None

            labelled = self._auto_label_tracks(issue_url, section_names)
            if labelled is None:
                logger.warning(f"  Track labelling failed for {issue_url}; skipping.")
                return None

            tracks_cache[issue_url] = {"tracks": labelled}
            self._save_tracks_cache(tracks_cache)

        return [
            t["name"]
            for t in tracks_cache[issue_url]["tracks"]
            if t.get("is_regular_paper") is True
        ]

    def _fetch_section_names(self, issue_url: str) -> Optional[List[str]]:
        """Fetch an issue page and return the list of section (track) names."""
        try:
            resp = self.session.get(issue_url)
            if not resp:
                return None
            soup = BeautifulSoup(resp.content, "html.parser")
            sections_div = soup.find("div", class_="sections")
            if not sections_div:
                return []
            names = []
            for section_div in sections_div.find_all("div", class_="section"):
                h2 = section_div.find("h2")
                if h2:
                    names.append(h2.get_text(strip=True))
            return names
        except Exception as e:
            logger.error(f"Failed to fetch sections from {issue_url}: {e}")
            return None

    def _auto_label_tracks(self, issue_url: str, section_names: List[str]) -> Optional[List[Dict]]:
        """Call Gemini to classify sections. Returns list of {name, is_regular_paper} or None."""
        if not self.track_model:
            logger.error("Track model not initialised — cannot label sections.")
            return None

        user_message = (
            f"Issue URL: {issue_url}\n\n"
            "Sections:\n" +
            "\n".join(f"- {name}" for name in section_names)
        )
        try:
            response = self.track_model.generate_content(user_message, generation_config=llm_json_config())
            if not response.text:
                return None
            parsed = json.loads(response.text.strip())
            tracks = parsed.get("tracks", [])
            for t in tracks:
                logger.debug(f"  {'✓' if t.get('is_regular_paper') else '✗'} {t['name']}")
            return tracks
        except Exception as e:
            logger.error(f"Track LLM call failed for {issue_url}: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Paper URL extraction (section-aware)
    # ──────────────────────────────────────────────────────────────────────────

    def _extract_paper_links_for_sections(
        self, issue_url: str, regular_sections: List[str]
    ) -> List[str]:
        """
        Fetch issue page and collect paper URLs only from sections in regular_sections.
        """
        try:
            resp = self.session.get(issue_url)
            if not resp:
                return []
            soup = BeautifulSoup(resp.content, "html.parser")
            paper_urls: List[str] = []

            sections_div = soup.find("div", class_="sections")
            if not sections_div:
                return []

            regular_set = set(regular_sections)

            for section_div in sections_div.find_all("div", class_="section"):
                h2 = section_div.find("h2")
                if not h2:
                    continue
                track_title = h2.get_text(strip=True)
                if track_title not in regular_set:
                    logger.debug(f"  Skipping section: {track_title}")
                    continue

                papers_ul = section_div.find("ul")
                if not papers_ul:
                    continue
                for li in papers_ul.find_all("li"):
                    title_h3 = li.find("h3", class_="title")
                    if title_h3:
                        link = title_h3.find("a")
                        if link and link.get("href"):
                            paper_urls.append(urljoin(self.base_url, link["href"]))

            return paper_urls

        except Exception as e:
            logger.error(f"Failed to extract papers from {issue_url}: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────────────
    # Year inference  (pure regex, no LLM)
    # ──────────────────────────────────────────────────────────────────────────

    _SHORT_YEAR_RE  = re.compile(r'\b(?:AAAI|IAAI|EAAI)-(\d{2})\b', re.IGNORECASE)
    _SERIES_YEAR_RE = re.compile(r'\((\d{4})\)')

    @classmethod
    def _extract_year(cls, title: str, series: Optional[str]) -> Optional[int]:
        matches = cls._SHORT_YEAR_RE.findall(title)
        if matches:
            def to_full(yy: int) -> int:
                return 2000 + yy if yy < 80 else 1900 + yy
            return min(to_full(int(m)) for m in matches)
        if series:
            m = cls._SERIES_YEAR_RE.search(series)
            if m:
                return int(m.group(1))
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Cache I/O
    # ──────────────────────────────────────────────────────────────────────────

    def _load_pages_cache(self) -> dict:
        if _PAGES_CACHE.exists():
            with open(_PAGES_CACHE) as f:
                return json.load(f)
        return {}

    def _save_pages_cache(self, data: dict) -> None:
        _PAGES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(_PAGES_CACHE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {_PAGES_CACHE} ({len(data.get('issues', []))} issues)")

    def _load_tracks_cache(self) -> dict:
        if _TRACKS_CACHE.exists():
            with open(_TRACKS_CACHE) as f:
                return json.load(f)
        return {}

    def _save_tracks_cache(self, data: dict) -> None:
        _TRACKS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(_TRACKS_CACHE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {_TRACKS_CACHE} ({len(data)} issues labelled)")

    # ──────────────────────────────────────────────────────────────────────────
    # Paper field extractors  (unchanged from original)
    # ──────────────────────────────────────────────────────────────────────────

    def _extract_title(self, soup: BeautifulSoup) -> str:
        h1 = soup.find("h1", class_="page_title")
        return h1.get_text(strip=True) if h1 else ""

    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        authors = []
        ul = soup.find("ul", class_="authors")
        if ul:
            for li in ul.find_all("li"):
                name_span = li.find("span", class_="name")
                if name_span:
                    authors.append(name_span.get_text(strip=True))
        return authors

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        section = soup.find("section", class_="item abstract")
        if section:
            p = section.find("p")
            if p:
                return p.get_text(strip=True)
        return ""

    def _extract_issue_info(self, soup: BeautifulSoup) -> str:
        issue_div = soup.find("div", class_="item issue")
        if issue_div:
            for sub in issue_div.find_all("section", class_="sub_item"):
                label = sub.find("h2", class_="label")
                if label and label.get_text(strip=True) == "Issue":
                    value = sub.find("div", class_="value")
                    if value:
                        a = value.find("a", class_="title")
                        if a:
                            return a.get_text(strip=True)
        return ""

    def _extract_section(self, soup: BeautifulSoup) -> str:
        issue_div = soup.find("div", class_="item issue")
        if issue_div:
            for sub in issue_div.find_all("section", class_="sub_item"):
                label = sub.find("h2", class_="label")
                if label and label.get_text(strip=True) == "Section":
                    value = sub.find("div", class_="value")
                    if value:
                        return value.get_text(strip=True)
        return ""

    def _extract_pdf_url(self, soup: BeautifulSoup) -> str:
        ul = soup.find("ul", class_="value galleys_links")
        if ul:
            a = ul.find("a", class_="obj_galley_link pdf")
            if a and a.get("href"):
                return urljoin(self.base_url, a["href"])
        return ""

    def _extract_paper_id(self, url: str) -> str:
        m = re.search(r'/article/view/(\d+)', url)
        return m.group(1) if m else url.split('/')[-1]