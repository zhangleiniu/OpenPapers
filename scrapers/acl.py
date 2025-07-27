# scrapers/template.py
"""Template for implementing new conference scrapers."""


from typing import List, Dict, Optional
import logging
from bs4 import BeautifulSoup
import os
import json
from .base import BaseScraper


logger = logging.getLogger(__name__)




class ACLScraper(BaseScraper):
   """ACL scraper - copy this to create new conference scrapers."""


   def __init__(self):
       super().__init__('acl')
   def labeled_json_exists(self) -> bool:
       return os.path.exists("data/acl/labeled.json")
  
   def load_labeled_tracks(self) -> dict:
       with open("data/acl/labeled.json") as f:
           return json.load(f)
      
   def get_relevant_tracks(self, year: int, labeled_tracks: dict) -> list:
       year_str = str(year)
       if year_str not in labeled_tracks:
           return []
       relevant_tracks = []
       for track in labeled_tracks[year_str]["tracks"]:
           if track.get("is_full_regular"):
               relevant_tracks.append(track["name"].lower())
       return relevant_tracks
  
   def get_track_names(self, year: int) -> list:
       url = f"{self.base_url}/events/acl-{year}/"
       response = self.session.get(url)
       if not response:
           logger.error(f"Failed to fetch {url}")
           return []
       soup = BeautifulSoup(response.content, 'html.parser')
       track_names = []
       a_tags = [a for a in soup.find_all('a', href=True) if a.get('class') == ['align-middle']] # some stuff contain align-middle as class name somewhere... so im making it so that it HAS TO ONLY BE align-middle
       for a in a_tags:
           if a.find_parent('h4', class_="d-sm-flex pb-2 border-bottom"):
               track_names.append(a.get_text(strip=True).lower())
       return track_names
              
   def get_conference_urls(self, year: int, relevant_tracks: list) -> list:
       try:
           url = f"{self.base_url}/events/acl-{year}/"
           response = self.session.get(url)
           if not response:
               return []
           soup = BeautifulSoup(response.content, 'html.parser')
           urls = []
           a_tags = [a for a in soup.find_all('a', href=True) if a.get('class') == ['align-middle']]
           for a in a_tags:
               if a.find_parent('h4', class_="d-sm-flex pb-2 border-bottom"):
                   track_name = a.get_text(strip=True).lower()
                   if track_name in relevant_tracks:
                       href = a['href']
                       if href:
                           urls.append(self.base_url + href)
           return urls
       except Exception as e:
           logger.error(f"Failed to get conference URLs: {e}")
           return []
      


   def get_paper_urls(self, year: int) -> List[str]:
       """Get paper URLs for a given year."""
       logger.info(f"Getting {self.config['name']} {year} paper URLs...")
       year_str = str(year)
       if self.labeled_json_exists():
           labeled_tracks = self.load_labeled_tracks()
           if year_str in labeled_tracks:
               relevant_tracks = self.get_relevant_tracks(year, labeled_tracks)
               logger.info(f"Relevant tracks for {year}: {relevant_tracks}")
               urls = self.get_conference_urls(year, relevant_tracks)
               paper_urls = []
               for url in urls:
                   logger.info(f"Found URL: {url}")
                   response = self.session.get(url)
                   if not response:
                       continue
                   soup = BeautifulSoup(response.content, 'html.parser')
                   strong_tags = soup.find_all('strong')
                   for strong_tag in strong_tags[1:]:
                       a = strong_tag.find('a', href=True, class_='align-middle')
                       if a:
                           paper_urls.append(self.base_url + a['href'])
               logger.info(f"Found {len(paper_urls)} paper URLs")
               return paper_urls           
      
       all_tracks = self.get_track_names(year)
       unlabeled_json = {
           year_str: {
               "tracks": [{"name": t} for t in all_tracks]
           }
       }
       os.makedirs("data/acl", exist_ok=True)
       with open(f"data/acl/unlabeled_{year}.json", "w") as f:
           json.dump(unlabeled_json, f, indent=2)
       logger.warning(
           f"Unlabeled track list for {year} written to data/acl/unlabeled_{year}.json. "
           "Please label this file using GPT and save as data/acl/labeled.json, then rerun."
       )
       exit(0)
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







