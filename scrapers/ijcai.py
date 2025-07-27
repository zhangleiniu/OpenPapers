# scrapers/template.py
"""Template for implementing new conference scrapers."""

from typing import List, Dict, Optional
import logging
from bs4 import BeautifulSoup
import os
import json
from .base import BaseScraper

logger = logging.getLogger(__name__)


class IJCAIScraper(BaseScraper):
    """IJCAI scraper - copy this to create new conference scrapers."""
    
    def __init__(self):
        # Replace 'template' with actual conference name (must match config.py)
        super().__init__('ijcai')
    def labeled_json_exists(self) -> bool:
        return os.path.exists("data/ijcai/labeled.json")

    def load_labeled_tracks(self) -> dict:
        with open("data/ijcai/labeled.json") as f:
            return json.load(f)
        
    def get_track_names(self, url: str) -> list:
        response = self.session.get(url)
        if not response:
            logger.error(f"Failed to fetch {url}")
            return []
        soup = BeautifulSoup(response.content, 'html.parser')
        section_tags = soup.find_all('div', class_='section_title')
        track_names = set()
        for section in section_tags:
            h3_tag = section.find('h3')
            if h3_tag:
                track_name = h3_tag.get_text(strip=True).lower()
                track_names.add(track_name)
        return list(track_names)

    def get_relevant_tracks(self, year: int, labeled_tracks: dict) -> list:
        year_str = str(year)
        if year_str not in labeled_tracks:
            return []
        relevant_tracks = []
        for track in labeled_tracks[year_str]["tracks"]:
            if track.get("is_full_regular"):
                relevant_tracks.append(track["name"].lower())
        return relevant_tracks

    def get_paper_urls(self, year: int) -> list:
        logger.info(f"Getting {self.config['name']} {year} paper URLs...")
        year_str = str(year)
        if self.labeled_json_exists():
            labeled_tracks = self.load_labeled_tracks()
            if year_str in labeled_tracks:
                relevant_tracks = self.get_relevant_tracks(year, labeled_tracks)
                logger.info(f"Relevant tracks for {year}: {relevant_tracks}")
                paper_urls = []
                url = f"{self.base_url}/proceedings/{year}/"
                resposne = self.session.get(url)
                if not resposne:
                    logger.error(f"Failed to fetch {url}")
                    return []
                soup = BeautifulSoup(resposne.content, 'html.parser')
                section_tags = soup.find_all('div', class_='section')
                for section in section_tags:
                    track_name = section.find('h3').get_text(strip=True).lower()
                    if track_name in relevant_tracks:
                        div_tags = section.find_all('div', class_="details")
                        for div in div_tags:
                            a_tags = div.find_all('a', href=True)
                            a_tag = a_tags[1]
                            if a_tag:
                                final_url = self.base_url + a_tag['href']
                                paper_urls.append(final_url)
                return paper_urls                


        url = f"{self.base_url}/proceedings/{year}/"
        all_tracks = self.get_track_names(url)
        unlabeled_json = {
            year_str: {
                "tracks": [{"name": t} for t in all_tracks]
            }
        }
        os.makedirs("data/ijcai", exist_ok=True)
        with open(f"data/ijcai/unlabeled_{year}.json", "w") as f:
            json.dump(unlabeled_json, f, indent=2)
        logger.warning(
            f"Unlabeled track list for {year} written to data/ijcai/unlabeled_{year}.json. "
            "Please label this file using GPT and save as data/ijcai/labeled.json, then rerun."
        )
        exit(0)
    
    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single paper."""
        try:
            response = self.session.get(url)
            if not response:
                return None
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # TODO: Implement conference-specific parsing
            # Common elements to extract:
            
            # 1. Title
            title = self._extract_title(soup)
            if not title:
                logger.warning(f"No title found for {url}")
                return None
            
            # 2. Authors
            authors = self._extract_authors(soup)
            
            # 3. Abstract
            abstract = self._extract_abstract(soup)
            
            # 4. Paper ID
            paper_id = self._extract_paper_id(url)
            
            # 5. PDF URL
            pdf_url = self._extract_pdf_url(soup, url)
            
            paper = {
                'id': paper_id,
                'title': title,
                'authors': authors,
                'abstract': abstract,
                'pdf_url': pdf_url
            }
            
            logger.debug(f"Parsed: {title}")
            return paper
            
        except Exception as e:
            logger.error(f"Failed to parse {url}: {e}")
            return None
    
    def _extract_title(self, soup: BeautifulSoup) -> str:
        h1_tags = soup.find_all('h1')
        if h1_tags:
            title = h1_tags[1].get_text(strip=True)
            if title:
                return title
        return ""
    
    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        h2_tag = soup.find('h2')
        if h2_tag:
            author_text = h2_tag.get_text(strip=True)
            authors = [author.strip() for author in author_text.split(',') if author.strip()]
            return authors
        return []
    
    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        div_tag = soup.find('div', class_='col-md-12')
        if div_tag:
            return div_tag.get_text(strip=True)
        return ""
    
    def _extract_paper_id(self, url: str) -> str:       
        import re

        pattern = r'/proceedings/(\d{4})/(\d+)'
        match = re.search(pattern, url)
        if match:
            return f"{match.group(1)}-{match.group(2)}"
        return ""
    
    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        a_tag = soup.find('a', href=True, class_="button btn-lg btn-download")
        if a_tag: 
            return a_tag['href']
        return ""
    
    def _make_absolute_url(self, url: str) -> str:
        """Convert relative URL to absolute."""
        from urllib.parse import urljoin
        return urljoin(self.base_url, url)


# Example scrapers for reference:
