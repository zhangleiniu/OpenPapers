import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging

from .base import BaseScraper
from utils import RobustSession, save_papers
logger = logging.getLogger(__name__)


class UAIScraper1518(BaseScraper):
    """UAI conference scraper for 2015-2018 - all papers on one page."""
    
    def __init__(self):
        super().__init__('uai')

    
    #THIS IS NOT USED; JUST HERE TO SATISFY INHERITANCE
    def get_paper_urls(self, year: int) -> List[str]:
        """For UAI, return the accepted papers page URL instead of individual papers."""
        return [f"https://www.auai.org/uai{year}/accepted.php"]
    
    # THIS IS NOT USED; JUST HERE TO SATISFY INHERITANCE
    def parse_paper(self, url: str) -> Optional[Dict]:
        """Not used for UAI single-page format."""
        logger.warning("parse_paper() not used for UAI - all data extracted from single page")
        return None
    
    # Override method; UAI does not have traditional individual paper URLs; all info is on one giant page
    def scrape_year(self, year: int, download_pdfs: bool = True, resume: bool = True) -> List[Dict]:
        """Override the main scraping method for UAI's single-page format."""
        logger.info(f"Scraping UAI {year} from single page...")
        

        if(year > 2016): 
            self.url = f"https://www.auai.org/uai{year}/accepted.php"
        else: 
            year_specific_urls = {
            2016: "https://www.auai.org/uai2016/proceedings.php",
            2015: "https://www.auai.org/uai2015/acceptedPapers.shtml",
        }
            self.url = year_specific_urls.get(year, None)
        if not self.url:
            logger.error(f"No URL defined for UAI {year}")
            return []
        
        try:
            response = self.session.get(self.url)
            if not response:
                logger.error(f"Failed to fetch {self.url}")
                return []
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Extract all papers from the page
            papers = self._extract_all_papers_from_page(soup, year)
            logger.info(f"Extracted {len(papers)} papers from UAI {year}")

            if download_pdfs: 
                for paper in papers: 
                    pdf_success = self.download_pdf(paper, year)
                    if not pdf_success: 
                        logger.warning(f"Failed to download PDF for paper {paper.get('id', 'unknown')}")

            save_papers(papers, self.conference, year)
            return papers
            
        except Exception as e:
            logger.error(f"Error scraping UAI {year}: {e}")
            return []
    
    def _extract_all_papers_from_page(self, soup: BeautifulSoup, year: int) -> List[Dict]:
        """Extract all papers from the UAI accepted papers page."""
        papers = []
        seen_titles = set()  # Track seen titles to avoid duplicates
        
        try:
            # Find all table rows containing papers
            tr_tags = soup.find_all('tr')
            logger.info(f"Found {len(tr_tags)} table rows")
            
            for i, tr in enumerate(tr_tags):
                paper = self._extract_paper_from_row(tr, year)
                if paper and paper.get('title'):  # Only add if we have a valid paper with title
                    title = paper['title']
                    if title not in seen_titles:  # Check for duplicates
                        papers.append(paper)
                        seen_titles.add(title)
                        logger.debug(f"Added paper: {title[:50]}...")
                    else:
                        logger.debug(f"Skipping duplicate: {title[:50]}...")
            
            return papers
            
        except Exception as e:
            logger.error(f"Error extracting papers from page: {e}")
            return []
    
    def _extract_paper_from_row(self, tr, year: int) -> Optional[Dict]:
        """Extract paper data from a single table row."""
        try:
            # 1. Extract title (usually in an <a> tag or strong text)
            title = self._extract_title_from_row(tr, year)
            if not title:
                return None  # Skip rows without titles
            
            # 2. Extract authors 
            authors = self._extract_authors_from_row(tr, year)
            
            # 3. Extract abstract from collapse div
            abstract = self._extract_abstract_from_row(tr, year)
            
            # 4. Extract PDF URL
            pdf_url = self._extract_pdf_url_from_row(tr, year)
            
            # 5. Generate paper ID
            paper_id = self._extract_paper_id(tr, year)
            
            paper = {
                'id': paper_id,
                'title': title,
                'authors': authors,
                'abstract': abstract,
                'pdf_url': pdf_url,
                'year': year,
                'conference': 'UAI', 
                'url': self.url,
            }
            
            logger.debug(f"Extracted paper: {title[:50]}...")
            return paper
            
        except Exception as e:
            logger.error(f"Error extracting paper from row: {e}")
            return None
    
    def _extract_title_from_row(self, tr, year) -> str:
        if (year == 2016 or year == 2015):
            td_tags = tr.find_all('td')
            if td_tags:
                author_container = td_tags[1]
                if author_container:
                    div_tag = author_container.find('div')
                    if div_tag:
                        b_tag = div_tag.find('b')
                        if b_tag:
                            author_text = b_tag.get_text(strip=True)
                            return author_text
        else: 
            h4_tag = tr.find('h4')
            if h4_tag:
                title = h4_tag.get_text(strip=True)
                if title and len(title) > 3:
                    return title
        return ""
    
    def _extract_authors_from_row(self, tr, year) -> List[str]:
        """Extract authors using text nodes after h4."""
        if (year == 2016 or year == 2015):
            i_tags = tr.find('i')
            if i_tags: 
                author_text = i_tags.get_text(strip=True)
                if author_text:
                    # Split by semicolon, then by comma for each author
                    author_entries = [a.strip() for a in author_text.split(';') if a.strip()]
                    authors = []
                    for entry in author_entries:
                        # If there's a comma, take the part before the comma as the author name
                        if ',' in entry:
                            name = entry.split(',', 1)[0].strip()
                            if len(name) > 2:
                                authors.append(name)
                        else:
                            # If no comma, assume it's just the author name
                            if len(entry) > 2:
                                authors.append(entry)
                    return authors
        else: 
            h4_tag = tr.find('h4')
            if not h4_tag:
                return []
        
            wanted_author_text = (h4_tag.next_sibling)
            author_text = wanted_author_text.strip()
            
            if author_text:
                authors = [a.strip() for a in author_text.split(',')]
                return [a for a in authors if len(a) > 2]      
        return []
    
    def _extract_abstract_from_row(self, tr, year) -> str:
        """Extract abstract from table row."""
        # Look for collapse div containing abstract
        if (year == 2016 or year == 2015):
            div_tag = tr.find('div', class_='collapse')
            if div_tag:
                nested_div = div_tag.find('div')
                if nested_div:
                    abstract_text = nested_div.get_text(strip=True)
                    if abstract_text and len(abstract_text) > 3:
                        return abstract_text
        else: 
            collapse_div = tr.find('div', class_='collapse')
            if collapse_div:
                abstract_text = collapse_div.get_text(strip=True)
                if abstract_text and len(abstract_text) > 3:
                    return abstract_text    
        return ""
    
    def _extract_pdf_url_from_row(self, tr, year) -> str:
        if(year == 2016 or year == 2015):
            td_tag = tr.find('td')
            if td_tag:
                a_tag = td_tag.find('a', href=True)
                if a_tag:
                    return urljoin(f"https://www.auai.org/uai{year}/", a_tag['href'])
        else: 
            td_tag = tr.find('td')
            if td_tag: 
                a_tag = td_tag.find('a', href=True)
                if a_tag: 
                    return a_tag['href']

        return ""
    def _extract_paper_id(self, tr, year) -> str: 
        if (year == 2016 or year == 2015): 
            td_tags = tr.find('td')
            if td_tags: 
                b_tag = td_tags.find('b')
                if b_tag:
                    raw_id = b_tag.get_text(strip=True)
                    processed_id = re.search(r'ID:\s*(\d+)', raw_id)
                    if processed_id:
                        return processed_id.group(1)
        else: 
            h5_tag = tr.find('h5')
            if h5_tag:
                paper_id_raw = h5_tag.get_text(strip=True)
                if paper_id_raw:
                    id = re.search(r'ID:\s*(\d+)', paper_id_raw)
                    if id:
                        return id.group(1)
        return ""
