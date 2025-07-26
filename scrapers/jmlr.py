from typing import List, Dict, Optional
import logging
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)


class JMLRScraper(BaseScraper):
    """JMLR scraper."""

    def __init__(self):
        super().__init__('jmlr')
    
    def get_paper_urls(self, year: int) -> List[str]:
        """Get paper URLs for a given year."""
        from urllib.parse import urljoin
        logger.info(f"Getting {self.config['name']} {year} paper URLs...")
        self.year = year
        
        try:
            volume = year - 1999 
            url = f"{self.base_url}/papers/v{volume}/"
            response = self.session.get(url)
            if not response:
                return []
            
            soup = BeautifulSoup(response.content, 'html.parser')
            paper_urls = []
        #https://www.jmlr.org/papers/v1/meila00a.html
            dl_tags = soup.find_all('dl')
            for dl in dl_tags:
                dd_tag = dl.find('dd')
                if dd_tag:
                    a_tag = dd_tag.find('a', href=True, string=lambda s: s and 'abs' in s.lower())
                    if a_tag:
                        href = a_tag['href']
                        if href:
                            full_url = urljoin(self.base_url + f"/papers/v{volume}/", href)
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
        authors = ""
        i_tag = soup.find('i')
        if i_tag:
            author_text = i_tag.get_text().strip()
            if author_text: 
                authors = author_text.split(',')
                authors = [author.strip() for author in authors]
        return authors

    
    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        abstract = ""
        p_tag = soup.find('p', class_='abstract')
        if p_tag:
            abstract = p_tag.get_text().strip()
            if abstract:
                return abstract
        h3_tag = soup.find('h3', string=lambda s: s and s.strip().lower() == "abstract") #for volume 5 and below
        if h3_tag:
            abstract_parts = []
            for sib in h3_tag.next_siblings:
                if getattr(sib, 'name', None) in ('font', 'p', 'h3', 'h2', 'h1', 'div'):
                    break
                
                if hasattr(sib, 'get_text'):
                    text = sib.get_text(separator=' ', strip=True) #im putting a space here for the nested tags
                    if text:
                        text = text.replace('\n', '').replace('\r', ' ').strip()
                        abstract_parts.append(text)
                
                elif hasattr(sib, 'strip'):
                    text = sib.strip()
                    if text:
                        text = text.replace('\n', '').replace('\r', ' ').strip()
                        abstract_parts.append(text)
            abstract = ' '.join(abstract_parts)
            return abstract
        return ""
    
    def _extract_paper_id(self, url: str) -> str:
        import re
        # https://www.jmlr.org/papers/v1/meila00a.html
        match = re.search(r'v\d+/([^/]+)\.html', url)
        if match: 
            return match.group(1)
        return ""
    
    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        pdf_url = ""
        a_tag = soup.find('a', string=lambda s: s and 'pdf' in s.lower())
        if a_tag: 
            href = a_tag['href']
            if href:
                pdf_url = self._make_absolute_url(href)
                return pdf_url


    
    def _make_absolute_url(self, url: str) -> str:
        """Convert relative URL to absolute."""
        from urllib.parse import urljoin
        return urljoin(self.base_url, url)
