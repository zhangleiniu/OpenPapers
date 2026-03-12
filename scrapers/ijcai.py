"""IJCAI scraper implementation.

HTML structure of ijcai.org (verified):

  Proceedings page: https://www.ijcai.org/proceedings/{year}/
    <div class="section_title">
      <h3>Track Name</h3>
    </div>
    <div class="section">
      <h3>Track Name</h3>
      <div class="details">
        <a href="...">...</a>
        <a href="/proceedings/{year}/{id}">Paper Title</a>
      </div>
    </div>

  Paper page: https://www.ijcai.org/proceedings/{year}/{id}
    <h1>...</h1>                    ← first h1 is site header
    <h1>Paper Title</h1>            ← second h1 is the title
    <h2>Author1, Author2</h2>
    <div class="col-md-12">Abstract text...</div>
    <a class="button btn-lg btn-download" href="...pdf">Download PDF</a>

Track filtering:
  IJCAI proceedings contain many tracks (main track, workshops, special
  tracks, etc.). Only main-conference proceedings are scraped, as
  determined by Gemini (via Vertex AI) on first run, cached in
  data/cache/ijcai_tracks.json. To correct a mislabeled year, edit
  the cache file directly and rerun.

  If the API call fails, a skeleton entry with all tracks set to
  is_full_regular: false is written to the cache file, and the run is
  aborted with instructions to label manually.
"""

import re
import os
import json
import logging
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from dotenv import load_dotenv

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

from .base import BaseScraper

load_dotenv()

logger = logging.getLogger(__name__)

_LABELED_PATH = "data/cache/ijcai_tracks.json"

_SYSTEM_PROMPT = """\
You are a helper that classifies academic conference proceedings tracks.
Given a conference name, year, and a list of track titles, decide which
tracks are the main conference proceedings — the primary full-paper track(s)
of the named conference itself.

Mark is_full_regular as false for: workshops, tutorials, special tracks,
demonstrations, doctoral consortium, surveys, and co-located events not
part of the main conference.

Respond with a JSON object only, no explanation, no markdown fences.
Schema:
{
  "tracks": [
    {"name": "<exact name as given>", "is_full_regular": true | false},
    ...
  ]
}
"""


class IJCAIScraper(BaseScraper):
    """IJCAI conference scraper."""

    def __init__(self):
        super().__init__('ijcai')

        project_id = os.getenv("GCP_PROJECT_ID")
        location = os.getenv("GCP_LOCATION", "us-central1")
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

        if project_id:
            try:
                vertexai.init(project=project_id, location=location)
                self.model = GenerativeModel(
                    model_name=self.model_name,
                    system_instruction=_SYSTEM_PROMPT
                )
                logger.info(f"Vertex AI initialized with model {self.model_name}")
            except Exception as e:
                logger.error(f"Failed to initialize Vertex AI: {e}")
                self.model = None
        else:
            logger.warning("GCP_PROJECT_ID not found in environment. LLM features will be disabled.")
            self.model = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_paper_urls(self, year: int) -> List[str]:
        """Return paper URLs for a given IJCAI year.

        On first run for a given year, calls the Gemini API to identify
        main-conference tracks and caches the result in data/cache/ijcai_tracks.json.
        """
        logger.info(f"Getting IJCAI {year} paper URLs...")

        all_tracks = self.get_track_names(year)
        if not all_tracks:
            logger.error(f"No tracks found for IJCAI {year}")
            return []

        relevant_tracks = self._get_relevant_tracks(year, all_tracks)
        if not relevant_tracks:
            return []

        logger.info(f"Relevant tracks for {year}: {relevant_tracks}")

        url = f"{self.base_url}/proceedings/{year}/"
        response = self.session.get(url)
        if not response:
            logger.error(f"Failed to fetch {url}")
            return []

        soup = BeautifulSoup(response.content, 'html.parser')
        paper_urls = []

        for section in soup.find_all('div', class_='section'):
            h3 = section.find('h3')
            if not h3:
                continue
            track_name = h3.get_text(strip=True).lower()
            if track_name not in relevant_tracks:
                continue
            for div in section.find_all('div', class_='details'):
                a_tags = div.find_all('a', href=True)
                if len(a_tags) >= 2:
                    paper_urls.append(self.base_url + a_tags[1]['href'])

        logger.info(f"Found {len(paper_urls)} papers for IJCAI {year}")
        return paper_urls

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single IJCAI paper from its proceedings page."""
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
    # Track helpers
    # ------------------------------------------------------------------

    def get_track_names(self, year: int) -> list:
        """Return all track names from the IJCAI proceedings page for a given year."""
        url = f"{self.base_url}/proceedings/{year}/"
        response = self.session.get(url)
        if not response:
            logger.error(f"Failed to fetch {url}")
            return []
        soup = BeautifulSoup(response.content, 'html.parser')
        track_names = set()
        for section in soup.find_all('div', class_='section_title'):
            h3 = section.find('h3')
            if h3:
                track_names.add(h3.get_text(strip=True).lower())
        return list(track_names)

    # ------------------------------------------------------------------
    # Track labeling
    # ------------------------------------------------------------------

    def _get_relevant_tracks(self, year: int, all_track_names: list) -> list:
        """Return track names identified as main-conference proceedings.

        Loads from data/cache/ijcai_tracks.json if the year is already cached.
        Otherwise calls Gemini API to label. If API fails, writes a skeleton
        for manual labeling and returns [].
        """
        year_str = str(year)
        labeled = self._load_labeled()

        if year_str not in labeled:
            logger.info(f"No labeled data for IJCAI {year}. Attempting auto-labeling...")

            year_data = self._auto_label(year, all_track_names)

            if not year_data:
                logger.warning(f"Auto-labeling could not be completed for IJCAI {year}.")
                year_data = {
                    "tracks": [{"name": name, "is_full_regular": False} for name in all_track_names]
                }
                labeled[year_str] = year_data
                self._save_labeled(labeled)
                logger.error(f"Auto-labeling failed for IJCAI {year}. Please manually edit {_LABELED_PATH} and set 'is_full_regular': true for main tracks, then rerun.")
                return []

            labeled[year_str] = year_data
            self._save_labeled(labeled)

        return [
            t["name"]
            for t in labeled[year_str]["tracks"]
            if t.get("is_full_regular")
        ]

    def _auto_label(self, year: int, track_names: list) -> Optional[dict]:
        """Call Gemini API to label tracks. Returns dict or None on failure."""
        if not self.model:
            logger.error("Gemini model not initialized. Skipping API call.")
            return None

        user_message = (
            f"Conference: IJCAI\n"
            f"Year: {year}\n\n"
            f"Tracks:\n" +
            "\n".join(f"- {name}" for name in track_names)
        )

        try:
            config = GenerationConfig(
                response_mime_type="application/json",
                temperature=0.1
            )
            response = self.model.generate_content(
                user_message,
                generation_config=config
            )
            if not response.text:
                return None

            result = json.loads(response.text.strip())
            if "tracks" not in result or not isinstance(result["tracks"], list):
                logger.error(f"Unexpected JSON structure from Gemini for IJCAI {year}")
                return None

            main = [t["name"] for t in result["tracks"] if t.get("is_full_regular")]
            logger.info(f"Auto-labeled IJCAI {year} via {self.model_name}: main tracks = {main}")
            return result

        except Exception as e:
            logger.error(f"Gemini API call or parsing failed: {e}")
            return None

    def _load_labeled(self) -> dict:
        if os.path.exists(_LABELED_PATH):
            with open(_LABELED_PATH) as f:
                return json.load(f)
        return {}

    def _save_labeled(self, labeled: dict) -> None:
        os.makedirs(os.path.dirname(_LABELED_PATH), exist_ok=True)
        with open(_LABELED_PATH, "w") as f:
            json.dump(labeled, f, indent=2)
        logger.info(f"Updated labeled tracks in {_LABELED_PATH}")

    # ------------------------------------------------------------------
    # Private extraction helpers
    # ------------------------------------------------------------------

    def _extract_title(self, soup: BeautifulSoup) -> str:
        h1_tags = soup.find_all('h1')
        if len(h1_tags) >= 2:
            return h1_tags[1].get_text(strip=True)
        return ""

    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        h2 = soup.find('h2')
        if h2:
            raw = h2.get_text(strip=True)
            return [a.strip() for a in raw.split(',') if a.strip()]
        return []

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        div = soup.find('div', class_='col-md-12')
        return div.get_text(strip=True) if div else ""

    def _extract_paper_id(self, url: str) -> str:
        match = re.search(r'/proceedings/(\d{4})/(\d+)', url)
        return f"{match.group(1)}-{match.group(2)}" if match else ""

    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        a_tag = soup.find('a', href=True, class_="button btn-lg btn-download")
        return a_tag['href'] if a_tag else ""