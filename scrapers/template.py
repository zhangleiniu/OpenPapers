# scrapers/template.py
"""Template for implementing new conference scrapers."""

from typing import List, Dict, Optional
import logging
from bs4 import BeautifulSoup

from .base import BaseScraper
from utils import extract_authors

logger = logging.getLogger(__name__)


class TemplateScraper(BaseScraper):
    """Template scraper - copy this to create new conference scrapers."""
    
    def __init__(self):
        # Replace 'template' with actual conference name (must match config.py)
        super().__init__('template')
    
    def get_paper_urls(self, year: int) -> List[str]:
        """Get paper URLs for a given year."""
        logger.info(f"Getting {self.config['name']} {year} paper URLs...")
        
        # TODO: Implement conference-specific URL discovery
        # Common patterns:
        # 1. Direct proceedings page: f"{self.base_url}/proceedings/{year}"
        # 2. Search API: f"{self.base_url}/api/papers?year={year}"
        # 3. Main page with year filtering
        
        try:
            # Example implementation:
            url = f"{self.base_url}/proceedings/{year}"
            response = self.session.get(url)
            if not response:
                return []
            
            soup = BeautifulSoup(response.content, 'html.parser')
            paper_urls = []
            
            # TODO: Extract paper links based on site structure
            # Common patterns:
            # - Look for links with "paper", "abstract", "details" in href
            # - Check for specific CSS classes like ".paper-link"
            # - Find links in paper listing containers
            
            for link in soup.find_all('a', href=True):
                href = link.get('href')
                # TODO: Add conference-specific filtering logic
                if href and 'paper' in href:
                    full_url = self._make_absolute_url(href)
                    paper_urls.append(full_url)
            
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
        """Extract paper title."""
        # TODO: Conference-specific title extraction
        # Common selectors: h1, h2, .title, .paper-title, #title
        
        selectors = ['h1', 'h2', '.title', '.paper-title', '#title']
        for selector in selectors:
            elem = soup.select_one(selector)
            if elem:
                title = elem.get_text().strip()
                if title and len(title) > 3:
                    return title
        
        return ""
    
    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        """Extract authors."""
        # TODO: Conference-specific author extraction
        # Common patterns:
        # - CSS classes: .authors, .author-list, .paper-authors
        # - Meta tags: <meta name="author" content="...">
        # - Structured data: JSON-LD, microdata
        # - Specific HTML structures unique to the conference
        
        # Try CSS selectors
        selectors = ['.authors', '.author-list', '.paper-authors', '[class*="author"]']
        for selector in selectors:
            elem = soup.select_one(selector)
            if elem:
                text = elem.get_text().strip()
                authors = extract_authors(text)
                if authors:
                    return authors
        
        # Try meta tags
        meta_author = soup.find('meta', attrs={'name': 'author'})
        if meta_author and meta_author.get('content'):
            authors = extract_authors(meta_author.get('content'))
            if authors:
                return authors
        
        return []
    
    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        """Extract abstract."""
        # TODO: Conference-specific abstract extraction
        # Common selectors: .abstract, #abstract, .summary, .paper-abstract
        
        selectors = ['.abstract', '#abstract', '.summary', '.paper-abstract']
        for selector in selectors:
            elem = soup.select_one(selector)
            if elem:
                abstract = elem.get_text().strip()
                if len(abstract) > 50:
                    return abstract
        
        # Fallback: longest paragraph
        paragraphs = soup.find_all('p')
        if paragraphs:
            longest = max(paragraphs, key=lambda p: len(p.get_text()))
            abstract = longest.get_text().strip()
            if len(abstract) > 100:
                return abstract
        
        return ""
    
    def _extract_paper_id(self, url: str) -> str:
        """Extract paper ID from URL."""
        # TODO: Conference-specific ID extraction
        # Common patterns:
        # - Hash in URL: /paper/abc123def
        # - Numeric ID: /paper/12345
        # - Filename: paper_name.html
        
        import re
        
        # Try different patterns
        patterns = [
            r'/paper/([a-f0-9]{32,})',  # Long hash
            r'/paper/(\d+)',            # Numeric ID
            r'/([^/]+)\.html$'          # Filename
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        # Fallback: use last part of URL
        return url.split('/')[-1].replace('.html', '')
    
    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        """Extract PDF URL."""
        # TODO: Conference-specific PDF URL extraction
        # Common patterns:
        # - Direct link: <a href="paper.pdf">PDF</a>
        # - Replace page URL: page.html -> page.pdf
        # - API endpoint: /api/paper/{id}/pdf
        
        # Look for PDF links
        pdf_links = soup.find_all('a', href=True)
        for link in pdf_links:
            href = link.get('href')
            if href and ('.pdf' in href or 'pdf' in link.get_text().lower()):
                return self._make_absolute_url(href)
        
        # Fallback: construct from page URL
        if '.html' in page_url:
            return page_url.replace('.html', '.pdf')
        
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


class ICLRScraper(BaseScraper):
    """ICLR scraper - OpenReview"""
    
    def __init__(self):
        super().__init__('iclr')
    
    def get_paper_urls(self, year: int) -> List[str]:
        # ICLR uses OpenReview API
        # Would use their API to get paper lists
        pass
    
    def parse_paper(self, url: str) -> Optional[Dict]:
        # OpenReview has JSON API responses
        pass


class AAAIScraper(BaseScraper):
    """AAAI scraper"""
    
    def __init__(self):
        super().__init__('aaai')
    
    def get_paper_urls(self, year: int) -> List[str]:
        # AAAI has proceedings pages
        pass
    
    def parse_paper(self, url: str) -> Optional[Dict]:
        # AAAI specific parsing
        pass