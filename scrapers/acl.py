# scrapers/template.py
"""Template for implementing new conference scrapers."""

from typing import List, Dict, Optional
import logging
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger(__name__)


class ACLScraper(BaseScraper):
    """ACL scraper - copy this to create new conference scrapers."""

    def __init__(self):
        # Replace 'template' with actual conference name (must match config.py)
        super().__init__('acl')
    

    def get_conference_url(self, year: int) -> str: 
        try: 
            import re
            url = f"{self.base_url}/events/acl-{year}/" # https://aclanthology.org/events/acl-2023/
            response = self.session.get(url)
            if not response:
                return 'invalid url'
            soup = BeautifulSoup(response.content, 'html.parser')
            a_tags = soup.find_all('a', href=True, class_='align-middle')
            for a in a_tags: 
                if a.find_parent('h4'):
                    if re.search(r'\bProceedings\s+of\s+the\s+\d{2}(st|nd|rd|th)\s+Annual\s+Meeting\s+of\s+the\s+Association\s+for\s+Computational\s+Linguistics\b', a.text, re.IGNORECASE):
                        href = a['href']
                        if href:
                            return self.base_url + href
        except Exception as e:
            logger.error(f"Failed to get conference URL: {e}")
            return "Could not find the conference URL"
        

    def get_paper_urls(self, year: int) -> List[str]:
        """Get paper URLs for a given year."""
        logger.info(f"Getting {self.config['name']} {year} paper URLs...")
        try:
            url = self.get_conference_url(year)
            paper_urls = []
            response = self.session.get(url)
            if not response: 
                return []
            soup = BeautifulSoup(response.content, 'html.parser')
            strong_tags = soup.find_all('strong')
            for strong_tag in strong_tags[1:]:
                a = strong_tag.find('a', href=True, class_='align-middle')
                if a:
                    paper_urls.append(self.base_url + a['href'])
            logger.info(f"Found {len(paper_urls)} paper URLs")
            return paper_urls
            
        except Exception as e:
            logger.error(f"Failed to get paper URLs: {e}")
            return []
    
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
        title = ""
        title = soup.find('h2', id='title')
        if title:
            return title.get_text().strip()
        return "failed"
    
    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        authors = []
        p_tag = soup.find('p', class_='lead')
        if p_tag: 
            a_tags = p_tag.find_all('a', href=True)
            for a in a_tags:
                authors.append(a.get_text().strip())
            return authors
        return []
    
    def _extract_abstract(self, soup: BeautifulSoup) -> str:
       abstract = ""
       h5_tag = soup.find('h5', class_='card-title')
       if h5_tag: 
           abstract = h5_tag.find_next_sibling('span').get_text().strip()
       return abstract if abstract else "No abstract found"

    
    def _extract_paper_id(self, url: str) -> str:
        import re
        match = re.search(r'https://aclanthology.org/(.*)', url)
        paper_id_with_slash = match.group(1) if match else ""
        return paper_id_with_slash.rstrip('/')
    
    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        dt_tag = soup.find('dt', string='PDF:')
        dd_tag = dt_tag.find_next_sibling('dd') if dt_tag else None
        if dd_tag and dd_tag.a:
            return dd_tag.a['href']
        return ""
    
    def _make_absolute_url(self, url: str) -> str:
        """Convert relative URL to absolute."""
        from urllib.parse import urljoin
        return urljoin(self.base_url, url)


# Example scrapers for reference:

class ICMLScraper(BaseScraper):
    """ICML scraper - proceedings.mlr.press"""
    
    def __init__(self):
        super().__init__('icml')
    
    def get_paper_urls(self, year: int) -> List[str]:
        # ICML uses volume numbers, need to map year to volume
        # This would need to be implemented based on ICML's structure
        pass
    
    def parse_paper(self, url: str) -> Optional[Dict]:
        # ICML has a specific format on MLR Press
        pass
