# scrapers/base.py
"""Base scraper class for all conference scrapers."""

import re
import time
import unicodedata
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional
import logging

from utils import RobustSession, save_papers, load_papers, get_paper_filename, assign_bibtex
from config import (DATA_ROOT, DEFAULT_REQUEST_DELAY, DEFAULT_RETRY_ATTEMPTS,
                    DEFAULT_TIMEOUT, PAPERS_DIR)

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Abstract base class for conference scrapers.

    Subclasses must declare:
        NAME          - display name, e.g. "NeurIPS"
        BASE_URL      - root URL for the conference site
        REQUEST_DELAY - seconds between requests (default: DEFAULT_REQUEST_DELAY)
        TIMEOUT       - HTTP timeout in seconds   (default: DEFAULT_TIMEOUT)
    """

    NAME: str = ""
    BASE_URL: str = ""
    REQUEST_DELAY: float = DEFAULT_REQUEST_DELAY
    TIMEOUT: int = DEFAULT_TIMEOUT
    CHECKPOINT_INTERVAL: int = 100
    PDF_DOWNLOAD_WORKERS: int = 1

    def __init__(self, conference: str):
        self.conference = conference.lower()
        self.base_url = self.BASE_URL
        self.session = RobustSession(
            delay=self.REQUEST_DELAY,
            retry_attempts=DEFAULT_RETRY_ATTEMPTS,
            timeout=self.TIMEOUT,
        )
        logger.info(f"Initialized {self.NAME or self.conference.upper()} scraper")

    @abstractmethod
    def get_paper_urls(self, year: int) -> List[str]:
        """Get list of paper URLs for a given year."""
        pass

    @abstractmethod
    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single paper from its URL."""
        pass

    def download_pdf(self, paper: Dict, year: int) -> bool:
        """Download PDF for a paper."""
        if not paper.get('pdf_url'):
            logger.warning(f"No PDF URL for paper {paper.get('id', 'unknown')}")
            paper['pdf_downloaded'] = False
            return False

        try:
            filename = get_paper_filename(paper)
            pdf_path = PAPERS_DIR / self.conference / str(year) / filename
            success = self.session.download_file(
                paper['pdf_url'], pdf_path,
                headers=self.pdf_request_headers(paper))
            if success:
                # Path relative to the data root (mustcite convention: papers/...)
                relative_path = f"papers/{self.conference}/{year}/{filename}"
                paper['pdf_path'] = relative_path
                paper['pdf_downloaded'] = True
            else:
                paper['pdf_downloaded'] = False
            return success

        except Exception as e:
            logger.error(f"Failed to download PDF for {paper.get('id', 'unknown')}: {e}")
            paper['pdf_downloaded'] = False
            return False

    def pdf_request_headers(self, paper: Dict) -> Dict[str, str]:
        """Return source-specific headers used only for the PDF request."""
        return {}

    @staticmethod
    def _identity_text(value: str) -> str:
        value = unicodedata.normalize("NFKD", str(value or ""))
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    @classmethod
    def _identity_keys(cls, paper: Dict) -> set:
        """Build conservative keys for reconciling provisional/archival records."""
        keys = set()
        for source, source_id in (paper.get("source_ids") or {}).items():
            if source and source_id:
                keys.add(("source", str(source), str(source_id)))
        if paper.get("metadata_source") and paper.get("source_id"):
            keys.add(("source", str(paper["metadata_source"]),
                      str(paper["source_id"])))
        title = cls._identity_text(paper.get("title", ""))
        authors = paper.get("authors") or []
        first_author = cls._identity_text(authors[0]) if authors else ""
        if title and first_author:
            keys.add(("bibliographic", title, first_author))
        return keys

    @staticmethod
    def _merge_record(existing: Dict, incoming: Dict) -> None:
        """Merge a newly discovered source without changing the stable paper ID."""
        old_id = existing.get("id")
        source_ids = dict(existing.get("source_ids") or {})
        source_ids.update(incoming.get("source_ids") or {})
        if incoming.get("metadata_source") and incoming.get("source_id"):
            source_ids[incoming["metadata_source"]] = incoming["source_id"]

        incoming_is_archival = incoming.get("publication_status") == "archival"
        same_source = bool(
            existing.get("metadata_source") == incoming.get("metadata_source") and
            existing.get("source_id") == incoming.get("source_id"))
        for key, value in incoming.items():
            if key in {"id", "source_ids"}:
                continue
            if value not in (None, "", [], {}):
                if incoming_is_archival or same_source or not existing.get(key):
                    existing[key] = value
        if old_id:
            existing["id"] = old_id
        if source_ids:
            existing["source_ids"] = source_ids

    def scrape_year(self, year: int, download_pdfs: bool = True,
                    resume: bool = True) -> List[Dict]:
        """Scrape all papers for a given year."""
        name = self.NAME or self.conference.upper()
        logger.info(f"Starting scrape of {name} {year}")

        try:
            existing_papers = load_papers(self.conference, year) if resume else []
            existing_urls = {p.get('url', '') for p in existing_papers}
            existing_by_url = {
                p.get('url'): p for p in existing_papers if p.get('url')
            }
            identity_index = {}
            for paper in existing_papers:
                for key in self._identity_keys(paper):
                    identity_index.setdefault(key, paper)

            # Retry downloading PDFs for papers with missing pdf_path
            if resume and download_pdfs:
                missing_pdf_count = self._retry_missing_pdfs(
                    existing_papers, year)
                if missing_pdf_count > 0:
                    logger.info(f"Retried {missing_pdf_count} papers with missing PDFs")

            paper_urls = self.get_paper_urls(year)
            if not paper_urls:
                logger.warning(f"No paper URLs found for {year}")
                return existing_papers

            logger.info(f"Found {len(paper_urls)} paper URLs")

            papers = existing_papers.copy()
            new_count = 0
            failed_count = 0

            for i, url in enumerate(paper_urls):
                try:
                    logger.debug(
                        "Processing %d/%d: %s",
                        i + 1, len(paper_urls), url.split('/')[-1])

                    previous = existing_by_url.get(url)
                    if (resume and previous is not None and
                            previous.get("publication_status") != "provisional"):
                        continue

                    paper = self.parse_paper(url)
                    if not paper:
                        failed_count += 1
                        logger.warning(f"Failed to parse paper: {url}")
                        continue

                    if not paper.get('title'):
                        failed_count += 1
                        logger.warning(f"No title found for paper: {url}")
                        continue

                    paper['year'] = year
                    paper['conference'] = self.conference
                    paper['url'] = url

                    matched = None
                    for key in self._identity_keys(paper):
                        if key in identity_index:
                            matched = identity_index[key]
                            break

                    if matched is not None:
                        was_archival = matched.get('publication_status') == 'archival'
                        self._merge_record(matched, paper)
                        existing_urls.add(url)
                        becoming_archival = (
                            not was_archival and
                            matched.get('publication_status') == 'archival')
                        if (download_pdfs and matched.get('pdf_url') and
                                (not matched.get('pdf_path') or becoming_archival)):
                            self.download_pdf(matched, year)
                        continue

                    if download_pdfs:
                        pdf_success = self.download_pdf(paper, year)
                        if not pdf_success:
                            logger.warning(f"PDF download failed for {paper.get('id', 'unknown')}")

                    papers.append(paper)
                    for key in self._identity_keys(paper):
                        identity_index.setdefault(key, paper)
                    new_count += 1

                    if new_count % self.CHECKPOINT_INTERVAL == 0:
                        assign_bibtex(papers)
                        save_papers(papers, self.conference, year)
                        logger.info(f"Saved progress: {len(papers)} papers")

                except Exception as e:
                    failed_count += 1
                    logger.error(f"Error processing {url}: {e}")
                    continue

            assign_bibtex(papers)
            save_papers(papers, self.conference, year)
            logger.info(f"Scraping completed for {name} {year}")
            logger.info(f"Total papers: {len(papers)} (new: {new_count}, failed: {failed_count})")
            return papers

        except Exception as e:
            logger.error(f"Scraping failed for {name} {year}: {e}")
            raise

    def _retry_missing_pdfs(self, papers: List[Dict], year: int) -> int:
        """Retry missing source PDFs, optionally overlapping file transfers."""
        pending = [
            paper for paper in papers
            if paper.get('pdf_url') and not self._has_valid_local_pdf(paper)
        ]
        if not pending:
            return 0

        def checkpoint(completed):
            if completed % self.CHECKPOINT_INTERVAL == 0:
                assign_bibtex(papers)
                save_papers(papers, self.conference, year)
                logger.info("Saved PDF retry progress: %d attempted", completed)

        if self.PDF_DOWNLOAD_WORKERS <= 1:
            for completed, paper in enumerate(pending, 1):
                logger.debug("Retrying PDF for: %s", paper.get('id', 'unknown'))
                self.download_pdf(paper, year)
                checkpoint(completed)
            return len(pending)

        logger.info(
            "Retrying %d PDFs with %d transfer workers",
            len(pending), self.PDF_DOWNLOAD_WORKERS)
        with ThreadPoolExecutor(max_workers=self.PDF_DOWNLOAD_WORKERS) as executor:
            completed = 0
            for start in range(0, len(pending), self.CHECKPOINT_INTERVAL):
                batch = pending[start:start + self.CHECKPOINT_INTERVAL]
                futures = {
                    executor.submit(self.download_pdf, paper, year): paper
                    for paper in batch
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        logger.error(
                            "PDF retry crashed for %s: %s",
                            futures[future].get('id', 'unknown'), exc)
                completed += len(batch)
                checkpoint(completed)
        return len(pending)

    @staticmethod
    def _has_valid_local_pdf(paper: Dict) -> bool:
        pdf_path = paper.get("pdf_path")
        if not pdf_path:
            return False
        relative = pdf_path[5:] if pdf_path.startswith("data/") else pdf_path
        path = DATA_ROOT / relative
        try:
            if path.stat().st_size < 1024:
                return False
            with path.open("rb") as handle:
                return handle.read(5) == b"%PDF-"
        except OSError:
            return False

    def scrape_multiple_years(self, years: List[int], **kwargs) -> Dict[int, List[Dict]]:
        """Scrape multiple years with error handling."""
        results = {}
        for year in years:
            try:
                papers = self.scrape_year(year, **kwargs)
                results[year] = papers
                time.sleep(2)
            except Exception as e:
                logger.error(f"Failed to scrape {year}: {e}")
                results[year] = []
        return results
