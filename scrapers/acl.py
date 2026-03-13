import re
import os
import json
import logging
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from dotenv import load_dotenv

# Google Cloud Vertex AI imports
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

from .base import BaseScraper
from config import CACHE_DIR

# Load environment variables from .env
load_dotenv()

logger = logging.getLogger(__name__)

_LABELED_PATH = CACHE_DIR / "acl_tracks.json"

_SYSTEM_PROMPT = """\
You are a helper that classifies academic conference proceedings volumes.
Given a conference name, year, and a list of volume titles, decide which
volumes are the main conference proceedings — the primary full-paper track(s)
of the named conference itself.

Mark is_full_regular as false for: workshops, tutorials, tutorial abstracts,
student research workshops, system demonstrations, industry tracks, shared
tasks, and co-located events not part of the named conference.

Respond with a JSON object only, no explanation, no markdown fences.
Schema:
{
  "tracks": [
    {"name": "<exact name as given>", "is_full_regular": true | false},
    ...
  ]
}
"""


class ACLScraper(BaseScraper):
    """ACL conference scraper using ACL Anthology."""

    def __init__(self):
        super().__init__('acl')
        
        # Initialize Vertex AI from environment variables
        project_id = os.getenv("GCP_PROJECT_ID")
        location = os.getenv("GCP_LOCATION", "us-central1")
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-3-flash")

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
        """Return paper URLs for a given ACL year.

        On first run for a given year, calls the Gemini API to identify
        main-conference tracks and caches the result in data/cache/acl_tracks.json.
        """
        logger.info(f"Getting ACL {year} paper URLs...")

        all_tracks = self.get_track_names(year)
        if not all_tracks:
            logger.error(f"No tracks found for ACL {year}")
            return []

        relevant_tracks = self._get_relevant_tracks(year, all_tracks)
        if not relevant_tracks:
            # Note: _get_relevant_tracks now handles logging the fallback instructions
            return []

        logger.info(f"Relevant tracks for {year}: {relevant_tracks}")

        paper_urls = []
        for url in self.get_conference_urls(year, relevant_tracks):
            logger.info(f"Fetching track: {url}")
            response = self.session.get(url)
            if not response:
                continue
            soup = BeautifulSoup(response.content, 'html.parser')
            for strong_tag in soup.find_all('strong')[1:]:
                a = strong_tag.find('a', href=True, class_='align-middle')
                if a:
                    paper_urls.append(self.base_url + a['href'])

        logger.info(f"Found {len(paper_urls)} papers for ACL {year}")
        return paper_urls

    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single ACL paper from its ACL Anthology page."""
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
        """Return all track names listed on the ACL event page for a given year."""
        url = f"{self.base_url}/events/acl-{year}/"
        response = self.session.get(url)
        if not response:
            logger.error(f"Failed to fetch {url}")
            return []
        soup = BeautifulSoup(response.content, 'html.parser')
        a_tags = [a for a in soup.find_all('a', href=True) if a.get('class') == ['align-middle']]
        return [
            a.get_text(strip=True).lower()
            for a in a_tags
            if a.find_parent('h4', class_="d-sm-flex pb-2 border-bottom")
        ]

    def get_conference_urls(self, year: int, relevant_tracks: list) -> list:
        """Return volume URLs for tracks that match relevant_tracks."""
        try:
            url = f"{self.base_url}/events/acl-{year}/"
            response = self.session.get(url)
            if not response:
                return []
            soup = BeautifulSoup(response.content, 'html.parser')
            a_tags = [a for a in soup.find_all('a', href=True) if a.get('class') == ['align-middle']]
            return [
                self.base_url + a['href']
                for a in a_tags
                if a.find_parent('h4', class_="d-sm-flex pb-2 border-bottom")
                and a.get_text(strip=True).lower() in relevant_tracks
            ]
        except Exception as e:
            logger.error(f"Failed to get conference URLs: {e}")
            return []

    # ------------------------------------------------------------------
    # Track labeling
    # ------------------------------------------------------------------

    def _get_relevant_tracks(self, year: int, all_track_names: list) -> list:
        """Return track names identified as main-conference proceedings.

        Loads from data/cache/acl_tracks.json if the year is already cached.
        Otherwise calls Gemini API to label. If API fails, generates a template
        for manual labeling.
        """
        year_str = str(year)
        labeled = self._load_labeled()

        if year_str not in labeled:
            logger.info(f"No labeled data for ACL {year}. Attempting auto-labeling...")
            
            year_data = self._auto_label(year, all_track_names)
            
            if not year_data:
                # Fallback: Generate a skeleton JSON for the user to edit manually
                logger.warning(f"Auto-labeling could not be completed for ACL {year}.")
                year_data = {
                    "tracks": [{"name": name, "is_full_regular": False} for name in all_track_names]
                }
                labeled[year_str] = year_data
                self._save_labeled(labeled)
                
                logger.error(f"Auto-labeling failed for ACL {year}. Please manually edit {_LABELED_PATH} and set 'is_full_regular': true for main tracks, then rerun.")
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
            f"Conference: ACL\n"
            f"Year: {year}\n\n"
            f"Volumes:\n" +
            "\n".join(f"- {name}" for name in track_names)
        )

        try:
            # Generation configuration to ensure strict JSON output
            config = GenerationConfig(
                response_mime_type="application/json",
                temperature=0.1  # Low temperature for more deterministic classification
            )
            
            response = self.model.generate_content(
                user_message,
                generation_config=config
            )
            
            if not response.text:
                return None
                
            result = json.loads(response.text.strip())
            
            if "tracks" not in result or not isinstance(result["tracks"], list):
                logger.error(f"Unexpected JSON structure from Gemini for ACL {year}")
                return None
                
            main = [t["name"] for t in result["tracks"] if t.get("is_full_regular")]
            logger.info(f"Auto-labeled ACL {year} via {self.model_name}: main tracks = {main}")
            return result

        except Exception as e:
            logger.error(f"Gemini API call or parsing failed: {e}")
            return None

    def _load_labeled(self) -> dict:
        if _LABELED_PATH.exists():
            with open(_LABELED_PATH) as f:
                return json.load(f)
        return {}

    def _save_labeled(self, labeled: dict) -> None:
        _LABELED_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LABELED_PATH, "w") as f:
            json.dump(labeled, f, indent=2)
        logger.info(f"Updated labeled tracks in {_LABELED_PATH}")

    # ------------------------------------------------------------------
    # Private extraction helpers
    # ------------------------------------------------------------------

    def _extract_title(self, soup: BeautifulSoup) -> str:
        h2 = soup.find('h2', id='title')
        return h2.get_text(strip=True) if h2 else ""

    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        p_tag = soup.find('p', class_='lead')
        if not p_tag:
            return []
        return [a.get_text(strip=True) for a in p_tag.find_all('a', href=True)]

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        h5_tag = soup.find('h5', class_='card-title')
        if h5_tag:
            sibling = h5_tag.find_next_sibling('span')
            if sibling:
                return sibling.get_text(strip=True)
        return ""

    def _extract_paper_id(self, url: str) -> str:
        match = re.search(r'https://aclanthology\.org/(.*)', url)
        return match.group(1).rstrip('/') if match else ""

    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        dt_tag = soup.find('dt', string='PDF:')
        if not dt_tag:
            return ""
        dd_tag = dt_tag.find_next_sibling('dd')
        if dd_tag and dd_tag.a:
            return dd_tag.a['href']
        return ""