# scrapers/iclr_1516.py
"""ICLR scraper implementation for 2015-2016."""

import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

from .base import BaseScraper

logger = logging.getLogger(__name__)


class ICLRScraper1516(BaseScraper):
    """ICLR conference scraper."""
    
    def __init__(self):
        super().__init__('iclr')
    
    def get_paper_urls(self, year: int) -> List[str]:
        """Get paper URLs for ICLR year."""
        logger.info(f"Getting ICLR {year} paper URLs...")
        
        # ICLR URL patterns for different years
        if year == 2015:
            url = "https://iclr.cc/archive/www/doku.php%3Fid=iclr2015:accepted-main.html"
        elif year == 2016:
            url = "https://iclr.cc/archive/www/doku.php%3Fid=iclr2016:accepted-main.html"
        else:
            logger.error(f"Unsupported ICLR year: {year}")
            return []
        
        try:
            response = self.session.get(url)
            if response:
                paper_data = self._extract_papers_from_page(response, year)
                logger.info(f"Found {len(paper_data)} papers from {url}")
                # Store the parsed paper data for later use in parse_paper
                self._paper_cache = {p['url']: p for p in paper_data}
                return [p['url'] for p in paper_data]
            else:
                logger.error(f"Failed to fetch {url}")
                return []
        except Exception as e:
            logger.error(f"Error getting papers from {url}: {e}")
            return []
    
    def _extract_papers_from_page(self, response, year: int) -> List[Dict]:
        """Extract all papers and their metadata from the ICLR page."""
        soup = BeautifulSoup(response.content, 'html.parser')
        papers = []
        
        # Find the main page div
        page_div = soup.find('div', class_='page')
        if not page_div:
            logger.error("Could not find div with class 'page'")
            return []
        
        # Find all track sections (h3 elements)
        track_headers = page_div.find_all('h3')
        
        for header in track_headers:
            track_name = header.get_text().strip()
            logger.debug(f"Processing track: {track_name}")
            
            # Find the next div with class="level3" after this h3
            current = header
            level3_div = None
            while current:
                current = current.find_next_sibling()
                if current and current.name == 'div' and 'level3' in (current.get('class') or []):
                    level3_div = current
                    break
                elif current and current.name == 'h3':
                    # Hit the next track header, stop looking
                    break
            
            if not level3_div:
                logger.debug(f"No level3 div found for track: {track_name}")
                continue
            
            # Find all paper list items in this track
            paper_items = level3_div.find_all('li', class_='level1')
            
            for item in paper_items:
                paper_data = self._parse_paper_item(item, track_name, year)
                if paper_data:
                    papers.append(paper_data)
        
        return papers
    
    def _parse_paper_item(self, item, track_name: str, year: int) -> Optional[Dict]:
        """Parse a single paper item from the list."""
        try:
            li_div = item.find('div', class_='li')
            if not li_div:
                return None
            
            # Find the first arxiv link (ignore [code], [video], etc.)
            arxiv_link = None
            for link in li_div.find_all('a', href=True):
                href = link.get('href')
                if 'arxiv.org/abs/' in href:
                    # Make sure this isn't a [code] or [video] link
                    link_text = link.get_text().strip()
                    if not (link_text.startswith('[') and link_text.endswith(']')):
                        arxiv_link = link
                        break
            
            if not arxiv_link:
                logger.warning(f"No arXiv link found in item: {li_div.get_text()[:100]}")
                return None
            
            # Extract title and arXiv URL
            title = arxiv_link.get_text().strip()
            arxiv_url = arxiv_link.get('href')
            
            # Extract arXiv ID
            arxiv_id = self._extract_arxiv_id(arxiv_url)
            if not arxiv_id:
                logger.warning(f"Could not extract arXiv ID from: {arxiv_url}")
                return None
            
            # Extract authors from the remaining text
            # Get all text after the first link, clean HTML
            text_after_title = ""
            current = arxiv_link
            
            # Collect all text and elements after the title link
            while current:
                if current.next_sibling:
                    current = current.next_sibling
                    if hasattr(current, 'get_text'):
                        text_after_title += current.get_text()
                    elif isinstance(current, str):
                        text_after_title += current
                else:
                    break
            
            # Clean and parse authors
            authors = self._parse_authors(text_after_title)
            
            # Construct PDF URL
            pdf_url = f"http://arxiv.org/pdf/{arxiv_id}.pdf"
            
            return {
                'url': arxiv_url,  # Use arXiv URL as the paper URL
                'title': title,
                'authors': authors,
                'track': track_name,
                'arxiv_id': arxiv_id,
                'pdf_url': pdf_url,
                'year': year
            }
            
        except Exception as e:
            logger.error(f"Error parsing paper item: {e}")
            return None
    
    def _extract_arxiv_id(self, arxiv_url: str) -> Optional[str]:
        """Extract arXiv ID from URL."""
        match = re.search(r'arxiv\.org/abs/(\d+\.\d+)', arxiv_url)
        if match:
            return match.group(1)
        return None
    
    def _parse_authors(self, text: str) -> List[str]:
        """Parse authors from text using smart parsing logic."""
        # Clean the text
        text = re.sub(r'<[^>]+>', '', text)  # Remove any remaining HTML
        text = re.sub(r'\s+', ' ', text).strip()  # Normalize whitespace
        
        # Remove leading comma and whitespace
        text = re.sub(r'^[,\s]+', '', text)
        
        if not text:
            return []
        
        # Check if text contains " and "
        if ' and ' in text:
            # Split on commas first, then handle the "and" in the last part
            if ',' in text:
                # Find the last " and " and replace it with a comma temporarily
                parts = text.rsplit(' and ', 1)
                if len(parts) == 2:
                    # Replace the last "and" with a comma
                    text = parts[0] + ', ' + parts[1]
            else:
                # Simple case: "Author1 and Author2"
                return [author.strip() for author in text.split(' and ') if author.strip()]
        
        # Split on commas and clean each author
        authors = []
        for author in text.split(','):
            author = author.strip()
            if author:
                authors.append(author)
        
        return authors
    
    def parse_paper(self, url: str) -> Optional[Dict]:
        """Parse a single ICLR paper."""
        try:
            # Get the cached paper data from get_paper_urls
            if hasattr(self, '_paper_cache') and url in self._paper_cache:
                paper = self._paper_cache[url].copy()
            else:
                logger.warning(f"Paper not found in cache: {url}")
                return None
            
            # Get abstract from arXiv
            abstract = self._get_arxiv_abstract(paper['arxiv_id'])
            paper['abstract'] = abstract
            
            # Set the ID field
            paper['id'] = paper['arxiv_id']
            
            logger.debug(f"Parsed paper: {paper['title']} ({len(paper['authors'])} authors)")
            return paper
            
        except Exception as e:
            logger.error(f"Failed to parse {url}: {e}")
            return None
    
    def _get_arxiv_abstract(self, arxiv_id: str) -> str:
        """Get abstract from arXiv page."""
        try:
            arxiv_url = f"http://arxiv.org/abs/{arxiv_id}"
            response = self.session.get(arxiv_url)
            
            if not response:
                logger.warning(f"Failed to fetch arXiv page: {arxiv_url}")
                return ""
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Find the abstract blockquote
            abstract_block = soup.find('blockquote', class_='abstract')
            if abstract_block:
                # Remove the "Abstract:" descriptor
                descriptor = abstract_block.find('span', class_='descriptor')
                if descriptor:
                    descriptor.decompose()
                
                abstract = abstract_block.get_text().strip()
                logger.debug(f"Found abstract for {arxiv_id}: {len(abstract)} chars")
                return abstract
            else:
                logger.warning(f"No abstract found for arXiv ID: {arxiv_id}")
                return ""
                
        except Exception as e:
            logger.error(f"Error getting abstract for {arxiv_id}: {e}")
            return ""