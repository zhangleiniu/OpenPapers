# scrapers/aaai.py
"""AAAI scraper implementation."""

import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

from .base import BaseScraper

logger = logging.getLogger(__name__)


class AAAIScraper(BaseScraper):
    """AAAI conference scraper."""
    
    def __init__(self):
        super().__init__('aaai')
        
        self.year_urls = {
            2020: [
                "https://ojs.aaai.org/index.php/AAAI/issue/view/255",
                "https://ojs.aaai.org/index.php/AAAI/issue/view/254",
                "https://ojs.aaai.org/index.php/AAAI/issue/view/253",
                "https://ojs.aaai.org/index.php/AAAI/issue/view/252",
                "https://ojs.aaai.org/index.php/AAAI/issue/view/251",
                "https://ojs.aaai.org/index.php/AAAI/issue/view/250",
                "https://ojs.aaai.org/index.php/AAAI/issue/view/249"
            ],
            2019: ["https://ojs.aaai.org/index.php/AAAI/issue/view/246"],
            2010: ["https://ojs.aaai.org/index.php/AAAI/issue/view/309"], 
            2011: ["https://ojs.aaai.org/index.php/AAAI/issue/view/308"],
            2012: ["https://ojs.aaai.org/index.php/AAAI/issue/view/307"],
            2013: ["https://ojs.aaai.org/index.php/AAAI/issue/view/306"],
            2014: ["https://ojs.aaai.org/index.php/AAAI/issue/view/305"],
            2015: ["https://ojs.aaai.org/index.php/AAAI/issue/view/304"],
            2016: ["https://ojs.aaai.org/index.php/AAAI/issue/view/303"],
            2017: ["https://ojs.aaai.org/index.php/AAAI/issue/view/302"],
            2018: ["https://ojs.aaai.org/index.php/AAAI/issue/view/301"]

        }
    
    def get_paper_urls(self, year: int) -> List[str]:
        """Get paper URLs for AAAI year."""
        logger.info(f"Getting AAAI {year} paper URLs...")
        
        urls = self.year_urls.get(year, [])
        if not urls:
            logger.warning(f"No URLs configured for AAAI {year}")
            return []
        
        all_paper_urls = []
        
        for url in urls:
            try:
                logger.info(f"Processing issue URL: {url}")
                response = self.session.get(url)
                if response:
                    paper_urls = self._extract_paper_links(response)
                    all_paper_urls.extend(paper_urls)
                    logger.info(f"Found {len(paper_urls)} papers from {url}")
                else:
                    logger.error(f"Failed to fetch {url}")
                    
            except Exception as e:
                logger.error(f"Error processing {url}: {e}")
                continue
        
        logger.info(f"Total unique papers found for {year}: {len(all_paper_urls)}")
        return all_paper_urls
    
    def _extract_paper_links(self, response) -> List[str]:
        """Extract paper links from AAAI issue page."""
        soup = BeautifulSoup(response.content, 'html.parser')
        paper_urls = []
        
        # Find the main sections div
        sections_div = soup.find('div', class_='sections')
        if not sections_div:
            logger.warning("No sections div found")
            return paper_urls
        
        # Find all section divs
        section_divs = sections_div.find_all('div', class_='section')
        
        for section_div in section_divs:
            # Get track title from h2
            track_h2 = section_div.find('h2')
            track_title = track_h2.get_text().strip() if track_h2 else "Unknown Track"
            logger.debug(f"Processing track: {track_title}")
            
            # Find the papers list (ul)
            papers_ul = section_div.find('ul')
            if not papers_ul:
                continue
            
            # Extract paper URLs from each li
            paper_items = papers_ul.find_all('li')
            for li in paper_items:
                title_h3 = li.find('h3', class_='title')
                if title_h3:
                    link = title_h3.find('a')
                    if link and link.get('href'):
                        paper_url = urljoin(self.base_url, link.get('href'))
                        paper_urls.append(paper_url)
        
        return paper_urls
    
    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single AAAI paper."""
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
            
            # Extract authors
            authors = self._extract_authors(soup)
            
            # Extract abstract
            abstract = self._extract_abstract(soup)
            
            # Extract issue information
            issue_info = self._extract_issue_info(soup)
            
            # Extract section
            section = self._extract_section(soup)
            
            # Extract PDF URL
            pdf_url = self._extract_pdf_url(soup)
            
            # Extract paper ID from URL
            paper_id = self._extract_paper_id(url)
            
            paper = {
                'id': paper_id,
                'title': title,
                'authors': authors,
                'abstract': abstract,
                'issue': issue_info,
                'section': section,
                'pdf_url': pdf_url
            }
            
            logger.debug(f"Parsed paper: {title} ({len(authors)} authors)")
            return paper
            
        except Exception as e:
            logger.error(f"Failed to parse {url}: {e}")
            return None
    
    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract paper title from h1.page_title."""
        title_h1 = soup.find('h1', class_='page_title')
        if title_h1:
            return title_h1.get_text().strip()
        return ""
    
    def _extract_authors(self, soup: BeautifulSoup) -> List[Dict]:
        """Extract authors list with names and affiliations."""
        authors = []
        
        authors_ul = soup.find('ul', class_='authors')
        if authors_ul:
            author_items = authors_ul.find_all('li')
            for li in author_items:
                name_span = li.find('span', class_='name')
                
                if name_span:
                    authors.append(name_span.get_text().strip(),)
        
        return authors
    
    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        """Extract paper abstract."""
        abstract_section = soup.find('section', class_='item abstract')
        if abstract_section:
            abstract_p = abstract_section.find('p')
            if abstract_p:
                return abstract_p.get_text().strip()
        return ""
    
    def _extract_issue_info(self, soup: BeautifulSoup) -> str:
        """Extract issue information."""
        issue_div = soup.find('div', class_='item issue')
        if issue_div:
            # Look for the first sub_item with "Issue" label
            sub_items = issue_div.find_all('section', class_='sub_item')
            for sub_item in sub_items:
                label_h2 = sub_item.find('h2', class_='label')
                if label_h2 and label_h2.get_text().strip() == 'Issue':
                    value_div = sub_item.find('div', class_='value')
                    if value_div:
                        title_link = value_div.find('a', class_='title')
                        if title_link:
                            return title_link.get_text().strip()
        return ""
    
    def _extract_section(self, soup: BeautifulSoup) -> str:
        """Extract section information."""
        issue_div = soup.find('div', class_='item issue')
        if issue_div:
            # Look for the sub_item with "Section" label
            sub_items = issue_div.find_all('section', class_='sub_item')
            for sub_item in sub_items:
                label_h2 = sub_item.find('h2', class_='label')
                if label_h2 and label_h2.get_text().strip() == 'Section':
                    value_div = sub_item.find('div', class_='value')
                    if value_div:
                        return value_div.get_text().strip()
        return ""
    
    def _extract_pdf_url(self, soup: BeautifulSoup) -> str:
        """Extract PDF download URL."""
        galleys_ul = soup.find('ul', class_='value galleys_links')
        if galleys_ul:
            pdf_link = galleys_ul.find('a', class_='obj_galley_link pdf')
            if pdf_link and pdf_link.get('href'):
                return urljoin(self.base_url, pdf_link.get('href'))
        return ""
    
    def _extract_paper_id(self, url: str) -> str:
        """Extract paper ID from AAAI URL."""
        # Pattern: /article/view/6528 or /article/view/6528/something
        match = re.search(r'/article/view/(\d+)', url)
        if match:
            return match.group(1)
        
        # Fallback: use last part of URL
        return url.split('/')[-1]