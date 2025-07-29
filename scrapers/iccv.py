from typing import List, Dict, Optional
import logging
from bs4 import BeautifulSoup

from .base import BaseScraper


logger = logging.getLogger(__name__)


class ICCVScraper(BaseScraper):
    """ICCV scraper."""
    
    def __init__(self):
        super().__init__('iccv')
    
    def get_paper_urls(self, year: int) -> List[str]:
        """Get paper URLs for a given year."""
        logger.info(f"Getting {self.config['name']} {year} paper URLs...")
        
        year_specific_urls = {
            2019: [
                f"{self.base_url}ICCV2019?day=2019-10-29",
                f"{self.base_url}ICCV2019?day=2019-10-30", #https://openaccess.thecvf.com/CVPR2019?day=2019-06-19
                f"{self.base_url}ICCV2019?day=2019-10-31",
                f"{self.base_url}ICCV2019?day=2019-11-01",
            ],
        }
        paper_urls = []
        try:
            # Example implementation: // https://openaccess.thecvf.com/ICCV2023?day=all
            urls_to_scrape = year_specific_urls.get(year, [f"{self.base_url}ICCV{year}?day=all"])
            for url in urls_to_scrape:
                response = self.session.get(url)
                if not response:
                    continue
                soup = BeautifulSoup(response.content, 'html.parser')
                
                
                dt_tags = soup.find_all('dt')
                for dt in dt_tags:
                    a_tag = dt.find('a', href=True)
                    if a_tag: 
                        href = a_tag['href']
                        if href: 
                            full_url = self.base_url + href
                            paper_urls.append(full_url)
            logger.info(f"Found {len(paper_urls)} papers from {url}")
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
        div_tag = soup.find('div', id='papertitle')
        if div_tag: 
            title = div_tag.get_text().strip()
            if title: 
                return title
    
    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        """Extract authors."""
        authors = ""
        authors_id = soup.find(id='authors')
        if authors_id:
            b_tag = authors_id.find('b')             # Get first <b> directly
            if b_tag:
                i_tag = b_tag.find('i')              # Get first <i> directly
                if i_tag:
                    authorText = i_tag.get_text().strip()
                    if authorText and len(authorText) > 3:
                        authors = authorText
        author_list = [a.strip() for a in authors.split(',') if a.strip()]
        return author_list
 
    
    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        """Extract abstract."""        
        abstract = ""
        abstract_id = soup.find(id='abstract')
        if abstract_id:
            abstract = abstract_id.get_text().strip()
            if abstract and len(abstract) > 3:
                return abstract

        return ""        
        

    
    def _extract_paper_id(self, url: str) -> str:
        """Extract paper ID from URL."""        
        import re 
        match = re.search(r'/([^/]+)\.html$', url)
        if match:
            return match.group(1)
        
        # Fallback: use last part of URL
        return url.split('/')[-1].replace('.html', '')
    

    def _extract_pdf_url(self, soup: BeautifulSoup, page_url: str) -> str:
        """Extract PDF URL."""
        pdf_url = page_url.replace('/html/', '/papers/').replace('.html', '.pdf')
        
        # special case for ICCV 2017
        if '_iccv_2017' in pdf_url:
            pdf_url = pdf_url.replace('content_iccv_2017', 'content_ICCV_2017')
        
        return pdf_url
    
    def _make_absolute_url(self, url: str) -> str:
        """Convert relative URL to absolute."""
        from urllib.parse import urljoin
        return urljoin(self.base_url, url)