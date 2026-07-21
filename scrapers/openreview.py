"""Shared OpenReview API v2 support for provisional conference sources."""

import logging
import os
import threading
from typing import Dict, List, Optional
from urllib.parse import urljoin

from dotenv import load_dotenv


logger = logging.getLogger(__name__)

API_BASE = "https://api2.openreview.net"
SITE_BASE = "https://openreview.net"


def unwrap(value):
    """Return the payload of an OpenReview v2 ``{"value": ...}`` field."""
    return value.get("value", value) if isinstance(value, dict) else value


class OpenReviewClient:
    """Small authenticated API client using a scraper's rate-limited session."""

    def __init__(self, session):
        self.session = session
        self._token: Optional[str] = None
        self._login_attempted = False
        self._login_lock = threading.Lock()

    @property
    def headers(self) -> Dict[str, str]:
        self._login()
        headers = {"Accept": "application/json"}
        if not self._token:
            return headers
        headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _login(self) -> None:
        with self._login_lock:
            if self._login_attempted:
                return
            self._login_attempted = True
            load_dotenv()
            username = os.getenv("OPENREVIEW_USERNAME")
            password = os.getenv("OPENREVIEW_PASSWORD")
            if not username or not password:
                logger.warning(
                    "OPENREVIEW_USERNAME/PASSWORD not set; trying anonymous API access")
                return
            response = self.session.post(
                f"{API_BASE}/login",
                json={"id": username, "password": password},
                timeout=30,
            )
            if response is None:
                logger.warning("OpenReview login failed; trying anonymous API access")
                return
            try:
                self._token = response.json().get("token")
            except ValueError:
                self._token = None
            if self._token:
                logger.info("Logged in to OpenReview API v2")
            else:
                logger.warning("OpenReview login returned no token")

    def get_notes(self, invitation: str, venue_id: str,
                  limit: int = 1000) -> List[Dict]:
        """Fetch accepted public notes, stopping on a short or empty page."""
        notes: List[Dict] = []
        offset = 0
        while True:
            response = self.session.get(
                f"{API_BASE}/notes",
                params={
                    "invitation": invitation,
                    "limit": limit,
                    "offset": offset,
                },
                headers=self.headers,
            )
            if response is None:
                raise RuntimeError(
                    f"OpenReview request failed for invitation {invitation}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"OpenReview returned invalid JSON for {invitation}") from exc
            if payload.get("name") or payload.get("status", 200) >= 400:
                raise RuntimeError(
                    f"OpenReview API error: {payload.get('message', payload.get('name'))}")
            batch = payload.get("notes", [])
            if not batch:
                break
            notes.extend(
                note for note in batch
                if unwrap(note.get("content", {}).get("venueid")) == venue_id
            )
            logger.info("OpenReview: %d accepted notes fetched", len(notes))
            if len(batch) < limit:
                break
            offset += limit
        return notes

    def note_to_paper(self, note: Dict) -> Dict:
        """Convert one accepted note to the OpenPapers data contract."""
        content = note.get("content", {})
        paper_id = note.get("id", "")
        pdf_path = unwrap(content.get("pdf", ""))
        venue = str(unwrap(content.get("venue", "")) or "")
        status = ""
        for label in ("oral", "spotlight", "poster", "regular"):
            if label in venue.lower():
                status = label.title()
                break
        paper = {
            "id": paper_id,
            "title": unwrap(content.get("title", "")),
            "authors": unwrap(content.get("authors", [])),
            "abstract": unwrap(content.get("abstract", "")),
            "keywords": unwrap(content.get("keywords", [])),
            "pdf_url": urljoin(SITE_BASE, pdf_path) if pdf_path else "",
            "openreview_url": f"{SITE_BASE}/forum?id={paper_id}",
            "metadata_source": "openreview",
            "source_id": paper_id,
            "source_ids": {"openreview": paper_id},
            "publication_status": "provisional",
        }
        if status:
            paper["status"] = status
        return paper
