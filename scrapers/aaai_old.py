"""AAAI scraper implementation."""

import re
import json
import hashlib
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

from .base import BaseScraper

logger = logging.getLogger(__name__)


class AAAIScraper(BaseScraper):
    """AAAI conference scraper."""
    
    def __init__(self, structure_file: str = "data/aaai_structure.json"):
        super().__init__('aaai')
        self.structure_file = structure_file
        self.structure = self._load_structure()
        # Store metadata for papers to use later in parse_paper
        self._paper_metadata = {}
    
    def _load_structure(self) -> Dict:
        """Load the pre-classified AAAI structure."""
        try:
            with open(self.structure_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"Structure file {self.structure_file} not found!")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in {self.structure_file}: {e}")
            raise
    
    def get_paper_urls(self, year: int) -> List[str]:
        """Get paper URLs for AAAI year."""
        logger.info(f"Getting AAAI {year} paper URLs...")
        
        if str(year) not in self.structure:
            logger.warning(f"No data found for AAAI {year}")
            return []
        
        year_data = self.structure[str(year)]
        paper_urls = []
        
        for volume in year_data.get('volumes', []):
            # Only process volumes with main AAAI tracks
            main_tracks = [track for track in volume.get('tracks', []) 
                          if track.get('is_main_aaai', False)]
            
            if not main_tracks:
                logger.debug(f"Skipping volume {volume['title']} - no main tracks")
                continue
            
            volume_url = volume['url']
            logger.info(f"Processing volume: {volume['title']}")
            
            # Extract paper URLs from this volume
            volume_paper_urls = self._extract_paper_urls_from_volume(
                volume_url, main_tracks, year, volume['title']
            )
            paper_urls.extend(volume_paper_urls)
        
        logger.info(f"Found {len(paper_urls)} total papers for AAAI {year}")
        return paper_urls
    
    def _extract_paper_urls_from_volume(self, volume_url: str, main_tracks: List[Dict], 
                                      year: int, volume_title: str) -> List[str]:
        """Extract paper URLs from a volume page."""
        try:
            response = self.session.get(volume_url)
            if not response:
                logger.error(f"Failed to fetch volume: {volume_url}")
                return []
            
            soup = BeautifulSoup(response.content, 'html.parser')
            paper_urls = []
            
            # Find all h2 elements (track headers)
            h2_elements = soup.find_all('h2')
            
            # Create a mapping of track names for quick lookup
            track_map = {track['name']: track for track in main_tracks}
            
            for h2 in h2_elements:
                h2_text = h2.get_text(strip=True)
                
                # Check if this h2 matches exactly one of our main tracks
                if h2_text not in track_map:
                    continue
                
                matching_track = track_map[h2_text]
                logger.debug(f"Processing track: {h2_text}")
                
                # Find the ul following this h2
                ul = self._find_following_ul(h2)
                if not ul:
                    logger.warning(f"No ul found after h2: {h2_text}")
                    continue
                
                # Extract paper URLs from this track
                track_paper_urls = self._extract_papers_from_ul(ul, matching_track, year, volume_title)
                paper_urls.extend(track_paper_urls)
            
            return paper_urls
            
        except Exception as e:
            logger.error(f"Error extracting papers from volume {volume_url}: {e}")
            return []
    
    def _find_following_ul(self, h2_element) -> Optional[BeautifulSoup]:
        """Find the ul element that follows an h2."""
        # First try direct next sibling
        ul = h2_element.find_next_sibling('ul')
        if ul:
            return ul
        
        # If not found, search through following siblings
        current = h2_element.next_sibling
        while current:
            if hasattr(current, 'name') and current.name == 'ul':
                return current
            current = current.next_sibling
        
        return None
    
    def _extract_papers_from_ul(self, ul_element, track: Dict, year: int, volume_title: str) -> List[str]:
        """Extract paper URLs from a ul element."""
        paper_urls = []
        
        # Find all li elements with class="paper-wrap"
        paper_items = ul_element.find_all('li', class_='paper-wrap')
        logger.debug(f"Found {len(paper_items)} papers in track: {track['name']}")
        
        for li in paper_items:
            try:
                # Extract paper URL from h5 > a
                h5 = li.find('h5')
                if not h5:
                    continue
                
                title_link = h5.find('a')
                if not title_link:
                    continue
                
                paper_url = title_link.get('href')
                if paper_url:
                    # Store metadata for later use in parse_paper
                    self._paper_metadata[paper_url] = {
                        'year': year,
                        'volume': volume_title,
                        'track': track['name']
                    }
                    paper_urls.append(paper_url)
                
            except Exception as e:
                logger.warning(f"Error extracting paper from li: {e}")
                continue
        
        return paper_urls
    
    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single AAAI paper."""
        try:
            response = self.session.get(url)
            if not response:
                logger.warning(f"Failed to fetch paper: {url}")
                return None
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Extract title
            title = self._extract_title(soup)
            if not title:
                logger.warning(f"No title found for {url}")
                return None
            
            # Extract authors
            authors = self._extract_authors(soup)
            
            # Extract abstract
            abstract = self._extract_abstract(soup)
            
            # Extract paper ID
            paper_id = self._extract_paper_id(url, title)
            
            # Construct PDF URL
            pdf_url = self._construct_pdf_url(soup, url)
            
            # Get stored metadata for this URL
            metadata = self._paper_metadata.get(url, {})
            
            paper = {
                'id': paper_id,
                'title': title,
                'authors': authors,
                'abstract': abstract,
                'pdf_url': pdf_url
            }
            
            # Add metadata from structure if available
            if metadata:
                paper.update(metadata)
            
            logger.debug(f"Parsed paper: {title} ({len(authors)} authors)")
            return paper
            
        except Exception as e:
            logger.error(f"Failed to parse {url}: {e}")
            return None
    
    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract paper title."""
        # AAAI papers usually have title in h1
        title_elem = soup.find('h1')
        if title_elem:
            title = title_elem.get_text().strip()
            if title and len(title) > 5:
                return title
        
        # Fallback: try other common selectors
        title_selectors = ['h2.entry-title', '.entry-title', '.paper-title', '.title']
        for selector in title_selectors:
            title_elem = soup.select_one(selector)
            if title_elem:
                title = title_elem.get_text().strip()
                if title and len(title) > 5:
                    return title
        
        return ""
    
    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        """Extract authors from AAAI paper page - handles multiple formats."""
        authors = []
        
        # AAAI specific pattern: <div class="author-wrap"> > <div class="author-output"> > <p class="bold">
        author_wrap = soup.find('div', class_='author-wrap')
        if author_wrap:
            author_output = author_wrap.find('div', class_='author-output')
            if author_output:
                # Get all p elements (both bold and non-bold)
                all_p_elements = author_output.find_all('p')
                bold_elements = author_output.find_all('p', class_='bold')
                non_bold_elements = [p for p in all_p_elements if 'bold' not in p.get('class', [])]
                
                # Rule: If there are non-bold p elements, they are institutions
                # and bold elements are definitely author names
                if non_bold_elements:
                    # Bold elements are pure author names
                    for elem in bold_elements:
                        text = elem.get_text().strip()
                        if text:
                            # Handle multiple authors in one element (split by 'and')
                            extracted = self._parse_author_text(text)
                            authors.extend(extracted)
                else:
                    # Only bold elements exist, need to filter out institutions
                    for elem in bold_elements:
                        text = elem.get_text().strip()
                        if not text:
                            continue
                        
                        # Skip if it looks like an institution
                        if self._is_institution(text):
                            continue
                        
                        # Handle different formats
                        extracted = self._parse_author_text(text)
                        authors.extend(extracted)
                
                logger.debug(f"Found {len(authors)} authors using AAAI structure")
        
        return authors
    
    def _is_institution(self, text: str) -> bool:
        """Check if text looks like an institution rather than author names."""
        institution_keywords = [
            'university', 'institute', 'laboratory', 'laboratories', 'lab',
            'college', 'school', 'department', 'center', 'centre',
            'corporation', 'company', 'inc', 'ltd', 'llc',
            'research', 'technology', 'science', 'engineering'
        ]
        
        text_lower = text.lower()
        
        # If text contains institution keywords, likely an institution
        for keyword in institution_keywords:
            if keyword in text_lower:
                return True
        
        # If text is very long (>50 chars), likely an institution
        if len(text) > 50:
            return True
        
        return False
    
    def _parse_author_text(self, text: str) -> List[str]:
        """Parse author text that may contain multiple authors in different formats."""
        authors = []
        
        # Format 1: "Bikramjit Banerjee and Jing Peng" - split by 'and'
        if ' and ' in text:
            parts = text.split(' and ')
            for part in parts:
                clean_part = part.strip()
                if clean_part:
                    authors.append(clean_part)
        
        # Format 2: Single author name or already clean
        else:
            authors.append(text)
        
        return authors
    
    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        """Extract paper abstract."""
        # AAAI abstract is usually in a div with class containing "abstract"
        abstract_selectors = [
            '.abstract',
            '.paper-abstract', 
            '#abstract',
            'div[class*="abstract"]'
        ]
        
        for selector in abstract_selectors:
            elem = soup.select_one(selector)
            if elem:
                abstract = elem.get_text().strip()
                if abstract and len(abstract) > 50:
                    return abstract
        
        # Fallback: look for meta tag
        meta_tag = soup.select_one('meta[name="citation_abstract"]')
        if meta_tag:
            abstract = meta_tag.get('content', '').strip()
            if abstract and len(abstract) > 50:
                return abstract
        
        return ""
    
    def _extract_paper_id(self, url: str, title: str) -> str:
        """Extract or generate paper ID."""
        # Try to extract ID from URL
        id_patterns = [
            r'/papers/(\d+)-',  # /papers/14003-title
            r'/(\d+)-',         # /14003-title
            r'paper[_-](\d+)',  # paper_14003 or paper-14003
        ]
        
        for pattern in id_patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        # Fallback: generate ID from title hash
        return hashlib.md5(title.encode()).hexdigest()[:8]
    
    def _construct_pdf_url(self, soup: BeautifulSoup, paper_url: str) -> str:
        """Find PDF URL from AAAI paper page."""
        # AAAI specific pattern: <div class="pdf-button"> > <a href="...pdf">
        pdf_button = soup.find('div', class_='pdf-button')
        if pdf_button:
            pdf_link = pdf_button.find('a', href=True)
            if pdf_link:
                href = pdf_link.get('href')
                if href and href.endswith('.pdf'):
                    return href
        
        logger.warning(f"No PDF link found for {paper_url}")
        return ""