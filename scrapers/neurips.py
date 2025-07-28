"""NeurIPS scraper implementation."""

import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

from .base import BaseScraper

logger = logging.getLogger(__name__)


class NeurIPSScraper(BaseScraper):
    """NeurIPS conference scraper."""
    
    def __init__(self):
        super().__init__('neurips')
    
    def get_paper_urls(self, year: int) -> List[str]:
        """Get paper URLs for NeurIPS year."""
        logger.info(f"Getting NeurIPS {year} paper URLs...")
        
        # NeurIPS URL pattern for papers by year
        url = f"{self.base_url}paper_files/paper/{year}"
        
        try:
            response = self.session.get(url)
            if response:
                paper_urls = self._extract_paper_links(response, year)
                if paper_urls:
                    logger.info(f"Found {len(paper_urls)} papers from {url}")
                    return paper_urls
                else:
                    logger.warning(f"No papers found at {url}")
                    return []
            else:
                logger.error(f"Failed to fetch {url}")
                return []
        except Exception as e:
            logger.error(f"Error getting papers from {url}: {e}")
            return []
    
    def _extract_paper_links(self, response, year: int) -> List[str]:
        """Extract paper links from response."""
        soup = BeautifulSoup(response.content, 'html.parser')
        paper_urls = set()  # Use set to avoid duplicates
        
        # Look for NeurIPS paper patterns
        for link in soup.find_all('a', href=True):
            href = link.get('href')
            if not href:
                continue
            
            # Match NeurIPS patterns: hash URLs with year
            if ('hash' in href and str(year) in href) or \
               (f'/{year}/' in href and ('Abstract' in href or '.html' in href)):
                
                full_url = urljoin(self.base_url, href)
                
                # Prefer Abstract pages over others
                if 'Abstract' in full_url or '.html' in full_url:
                    paper_urls.add(full_url)
        
        return list(paper_urls)
    
    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single NeurIPS paper."""
        try:
            response = self.session.get(url)
            if not response:
                return None
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Extract title
            title = self._extract_title(soup)
            if not title:
                logger.warning(f"No title found for {url}")
                return None
            
            # Extract authors (NeurIPS specific: often in italic elements)
            authors = self._extract_authors(soup)
            
            # Extract abstract
            abstract = self._extract_abstract(soup)
            
            # Extract paper ID from URL
            paper_id = self._extract_paper_id(url)
            
            # Construct PDF URL
            pdf_url = self._construct_pdf_url(url)
            
            paper = {
                'id': paper_id,
                'title': title,
                'authors': authors,
                'abstract': abstract,
                'pdf_url': pdf_url
            }
            
            logger.debug(f"Parsed paper: {title} ({len(authors)} authors)")
            return paper
            
        except Exception as e:
            logger.error(f"Failed to parse {url}: {e}")
            return None
    
    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract paper title."""
        col_div = soup.find('div', class_='col p-3')
        if col_div:
            h4_elem = col_div.find('h4')
            if h4_elem:
                title = h4_elem.get_text().strip()

        return title if title else ""
    
    def _extract_authors(self, soup: BeautifulSoup) -> List[str]:
        """Extract authors - NeurIPS specific logic."""
        authors = []
        
        authors_h4 = soup.find('h4', string='Authors')
        if authors_h4:
            p_elem = authors_h4.find_next_sibling('p')      # Find next <p>
            if p_elem:
                i_elem = p_elem.find('i')                    # Find <i> inside <p>
                if i_elem:
                    author_text = i_elem.get_text().strip()  # Get author text
                    if ',' in author_text:
                        authors = [a.strip() for a in author_text.split(',')]
                    else:
                        authors = [author_text] 
        return authors
    
    
    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        """Extract paper abstract from NeurIPS specific structure."""
        # NeurIPS specific pattern: <h4>Abstract</h4> followed by <p>abstract text</p>
        abstract_h4 = soup.find('h4', string='Abstract')
        if abstract_h4:
            # Find the next non-empty <p> element after the <h4>Abstract</h4>
            current = abstract_h4
            while current:
                current = current.find_next_sibling('p')
                if current:
                    abstract = current.get_text().strip()
                    if abstract and len(abstract) > 50:  # Skip empty <p></p> and find substantial content
                        logger.debug(f"Found abstract under <h4>Abstract</h4>: {len(abstract)} chars")
                        return abstract
        
    
    def _extract_paper_id(self, url: str) -> str:
        """Extract paper ID from NeurIPS URL."""
        # Pattern: /hash/abc123def456-Abstract.html
        match = re.search(r'/hash/([a-f0-9]+)', url)
        if match:
            return match.group(1)
        
        # Fallback: use last part of URL
        return url.split('/')[-1].replace('.html', '').replace('-Abstract', '')
        

    def _construct_pdf_url(self, paper_url: str) -> str:
        """Construct PDF URL from paper URL for NeurIPS papers (all years)."""
        if '/hash/' in paper_url:
            pdf_url = paper_url.replace('/hash/', '/file/')
            
            # Match both old and new patterns using regex
            match = re.search(r'-Abstract(?:-(\w+(?:_\w+)*))?\.html$', pdf_url)
            if match:
                track_suffix = match.group(1)
                if track_suffix:
                    # e.g., Datasets_and_Benchmarks, Conference, etc.
                    pdf_suffix = f'-Paper-{track_suffix}.pdf'
                    pdf_url = re.sub(r'-Abstract-\w+(?:_\w+)*\.html$', pdf_suffix, pdf_url)
                else:
                    # pre-2022 format (no track suffix)
                    pdf_url = pdf_url.replace('-Abstract.html', '-Paper.pdf')
                return pdf_url
            else:
                raise ValueError(f"Unrecognized abstract URL format: {paper_url}")
        else:
            raise ValueError(f"Unexpected paper URL format: {paper_url}")