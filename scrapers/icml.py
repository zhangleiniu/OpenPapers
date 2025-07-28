"""ICML scraper implementation."""

import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

from .base import BaseScraper

logger = logging.getLogger(__name__)


class ICMLScraper(BaseScraper):
    """ICML conference scraper."""
    
    def __init__(self):
        super().__init__('icml')
        self._volume_cache = {}  # Cache year->volume mapping
    
    def _get_volume_for_year(self, year: int) -> Optional[str]:
        """Get ICML volume identifier (e.g., 'v119') for the given year."""
        if year in self._volume_cache:
            return self._volume_cache[year]

        logger.info(f"Finding ICML volume for year {year}...")

        try:
            response = self.session.get(self.base_url)
            if not response or response.status_code != 200:
                logger.error("Failed to fetch ICML proceedings main page")
                return None

            soup = BeautifulSoup(response.content, 'html.parser')

            # Matches: "Proceedings of ICML 2022" or "ICML 2022 Proceedings"
            icml_pattern = re.compile(
                #rf'(Proceedings\s+of\s+ICML\s+{year}|ICML\s+{year}\s+Proceedings)',
                rf'\b(?:Proceedings\s+of\s+ICML\s+{year}|ICML\s+{year}\s+Proceedings)\b\s*$',
                re.IGNORECASE
            )

            for li in soup.find_all('li'):
                li_text = li.get_text().strip()
                if icml_pattern.search(li_text):
                    link = li.find('a', href=True)
                    if link:
                        href = link['href']
                        match = re.match(r'v\d+', href)
                        if match:
                            volume_id = match.group(0)
                            self._volume_cache[year] = volume_id
                            logger.info(f"Found ICML {year} volume: {volume_id}")
                            return volume_id

            logger.warning(f"No ICML volume found for year {year}")
            return None

        except Exception as e:
            logger.error(f"Error while fetching ICML volume for year {year}: {e}")
            return None
    
    def get_paper_urls(self, year: int) -> List[str]:
        """Get paper URLs for ICML year."""
        logger.info(f"Getting ICML {year} paper URLs...")
        
        # Get volume number
        volume = self._get_volume_for_year(year)
        if not volume:
            return []
        
        # Construct volume URL
        volume_url = f"{self.base_url}{volume}/"
        
        try:
            response = self.session.get(volume_url)
            if not response:
                logger.error(f"Failed to fetch volume page: {volume_url}")
                return []
            
            # Extract abstract URLs from paper divs
            soup = BeautifulSoup(response.content, 'html.parser')
            paper_urls = []
            
            for paper_div in soup.find_all('div', class_='paper'):
                # Find the abs link
                links_p = paper_div.find('p', class_='links')
                if links_p:
                    abs_link = links_p.find('a', string='abs')
                    if abs_link and abs_link.get('href'):
                        abs_url = urljoin(self.base_url, abs_link.get('href'))
                        paper_urls.append(abs_url)
            
            logger.info(f"Found {len(paper_urls)} papers in volume {volume}")
            return paper_urls
            
        except Exception as e:
            logger.error(f"Error getting papers from {volume_url}: {e}")
            return []
    
    def parse_paper(self, abs_url: str) -> Optional[Dict]:
        """Parse a single ICML paper from its abstract URL."""
        try:
            # Get abstract from the abstract page
            abstract = self._get_abstract_from_page(abs_url)
            
            # Get paper metadata from volume page
            paper_metadata = self._get_paper_metadata_from_volume(abs_url)
            
            if not paper_metadata:
                logger.warning(f"Could not get metadata for {abs_url}")
                return None
            
            # Combine abstract with metadata
            paper = paper_metadata.copy()
            paper['abstract'] = abstract
            
            logger.debug(f"Parsed paper: {paper.get('title', 'Unknown')} ({len(paper.get('authors', []))} authors)")
            return paper
            
        except Exception as e:
            logger.error(f"Failed to parse {abs_url}: {e}")
            return None
    
    def _get_abstract_from_page(self, abs_url: str) -> str:
        """Extract abstract from abstract page."""
        try:
            response = self.session.get(abs_url)
            if not response:
                return ""
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Look for abstract div
            abstract_div = soup.find('div', id='abstract', class_='abstract')
            if abstract_div:
                return abstract_div.get_text().strip()
            
            # Fallback: look for any div with class abstract
            abstract_div = soup.find('div', class_='abstract')
            if abstract_div:
                return abstract_div.get_text().strip()
            
            logger.warning(f"No abstract found on {abs_url}")
            return ""
            
        except Exception as e:
            logger.error(f"Error getting abstract from {abs_url}: {e}")
            return ""
    
    def _get_paper_metadata_from_volume(self, abs_url: str) -> Optional[Dict]:
        """Get paper metadata from the volume page by finding the matching paper div."""
        try:
            # Extract volume from abs URL
            # abs_url like: https://proceedings.mlr.press/v202/aamand23a.html
            volume_match = re.search(r'/v(\d+)/', abs_url)
            if not volume_match:
                logger.error(f"Could not extract volume from {abs_url}")
                return None
            
            volume = volume_match.group(1)
            paper_id = self._extract_paper_id_from_abs_url(abs_url)
            
            # Get volume page
            volume_url = f"{self.base_url}v{volume}/"
            response = self.session.get(volume_url)
            if not response:
                return None
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Find the specific paper div by matching the abs link
            for paper_div in soup.find_all('div', class_='paper'):
                links_p = paper_div.find('p', class_='links')
                if links_p:
                    abs_link = links_p.find('a', string='abs')
                    if abs_link and abs_link.get('href'):
                        div_abs_url = urljoin(self.base_url, abs_link.get('href'))
                        if div_abs_url == abs_url:
                            # Found the matching div, extract metadata
                            return self._extract_metadata_from_paper_div(paper_div, volume)
            
            logger.warning(f"Could not find paper div for {abs_url}")
            return None
            
        except Exception as e:
            logger.error(f"Error getting metadata for {abs_url}: {e}")
            return None
    
    def _extract_metadata_from_paper_div(self, paper_div, volume: str) -> Dict:
        """Extract metadata from a paper div element."""
        metadata = {}
        
        # Extract title
        title_p = paper_div.find('p', class_='title')
        if title_p:
            metadata['title'] = title_p.get_text().strip()
        
        # Extract authors and other details
        details_p = paper_div.find('p', class_='details')
        if details_p:
            authors_span = details_p.find('span', class_='authors')
            if authors_span:
                author_text = authors_span.get_text(separator=' ', strip=True)
            
                # Replace non-breaking spaces with regular space
                author_text = author_text.replace('\xa0', ' ')

                # Authors are typically split by ', '
                authors = [a.strip() for a in author_text.split(',') if a.strip()]
                
                metadata['authors'] = authors
            
        # Extract PDF URL from links
        links_p = paper_div.find('p', class_='links')
        if links_p:
            pdf_link = None
            for link in links_p.find_all('a'):
                if 'Download PDF' in link.get_text() or link.get_text().strip() == 'pdf':
                    pdf_link = link.get('href')
                    break
            
            if pdf_link:
                metadata['pdf_url'] = urljoin(self.base_url, pdf_link)
        
        # Extract paper ID from the abs link or PDF link
        if links_p:
            abs_link = links_p.find('a', string='abs')
            if abs_link and abs_link.get('href'):
                metadata['id'] = self._extract_paper_id_from_abs_url(abs_link.get('href'))
        
        return metadata
    
    def _extract_paper_id_from_abs_url(self, abs_url: str) -> str:
        """Extract paper ID from abstract URL."""
        # Pattern: /v202/aamand23a.html -> aamand23a
        match = re.search(r'/v\d+/([^/]+)\.html', abs_url)
        if match:
            return match.group(1)
        
        # Fallback: use filename without extension
        return abs_url.split('/')[-1].replace('.html', '')