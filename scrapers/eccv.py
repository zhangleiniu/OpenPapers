from typing import List, Dict, Optional
import logging
from bs4 import BeautifulSoup

from .base import BaseScraper


logger = logging.getLogger(__name__)


class ECCVScraper(BaseScraper):
    """ECCV scraper."""
    
    def __init__(self):
        # Replace 'template' with actual conference name (must match config.py)
        super().__init__('eccv')
    
    def get_paper_urls(self, year: int) -> List[str]:
        """Get paper URLs for a given year."""
        logger.info(f"Getting {self.config['name']} {year} paper URLs...")
        
        try:
            # Example implementation:
            url = f"{self.base_url}/papers.php"
            response = self.session.get(url)
            if not response:
                return []
            
            soup = BeautifulSoup(response.content, 'html.parser')
            paper_urls = []
            
            
            button_tags = soup.find_all('button', class_='accordion')
            for button in button_tags:
                if str(year) in button.text:
                    panel = button.find_next_sibling('div', class_='accordion-content')
                    if panel:
                        dt_tags = panel.find_all('dt', class_="ptitle")
                        for dt in dt_tags:
                            a_tag = dt.find('a', href=True)
                            if a_tag:
                                full_url = self.base_url + a_tag['href']
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
        div_tag = soup.find('div', id='papertitle')
        if div_tag:
            title = div_tag.get_text().strip()
            if title:
                return title
        return ""

    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
       div_tag = soup.find('div', id='authors')
       if div_tag: 
           i_tag = div_tag.find('i')
           if i_tag:
               raw_authors = i_tag.get_text().replace('*', '').strip()
               if raw_authors:
                   authors = [author.strip() for author in raw_authors.split(',')]
                   return authors

       return []

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        div_tag = soup.find('div', id='abstract')
        if div_tag:
            abstract = div_tag.get_text().strip().strip('"').strip("'")
            if abstract:
                return abstract
        return ""
    
    def _extract_paper_id(self, url: str) -> str:
        import re
        match = re.search(r'/html/(.*?)(?:\.php|$)', url)
        if match:
            return match.group(1)
        return ""
    
    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        a_tag = soup.find('a', href= True, string='pdf')
        if a_tag:
            href = a_tag['href']
            url_portion = href.find('papers')
            if url_portion != -1:
                return self.base_url + href[url_portion:]
        return ""
    def _make_absolute_url(self, url: str) -> str:
        """Convert relative URL to absolute."""
        from urllib.parse import urljoin
        return urljoin(self.base_url, url)
