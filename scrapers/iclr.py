# scrapers/iclr.py
"""ICLR scraper implementation for OpenReview years 2017-2023."""

import json
import os
import time
import requests
import re
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)


class ICLRScraper:
    """ICLR conference scraper for OpenReview years 2017-2023."""
    
    def __init__(self):
        self.api_base = "https://api.openreview.net/notes"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'ICLR-Paper-Scraper/1.0'
        })
        
        # Year-specific patterns discovered from our archaeological expedition
        self.year_patterns = {
            2017: {
                'invitation': 'ICLR.cc/2017/conference/-/submission',
                'decision_pattern': 'ICLR.cc/2017/conference/-/paper{paper_num}/acceptance',
                'decision_field': 'decision'
            },
            2018: {
                'invitation': 'ICLR.cc/2018/Conference/-/Blind_Submission', 
                'decision_pattern': 'ICLR.cc/2018/Conference/-/Acceptance_Decision',
                'decision_field': 'decision'
            },
            2019: {
                'invitation': 'ICLR.cc/2019/Conference/-/Blind_Submission',
                'decision_pattern': 'ICLR.cc/2019/Conference/-/Paper{paper_num}/Meta_Review',
                'decision_field': 'recommendation'
            },
            2020: {
                'invitation': 'ICLR.cc/2020/Conference/-/Blind_Submission',
                'decision_pattern': 'ICLR.cc/2020/Conference/Paper{paper_num}/-/Decision', 
                'decision_field': 'decision'
            },
            2021: {
                'invitation': 'ICLR.cc/2021/Conference/-/Blind_Submission',
                'decision_pattern': 'ICLR.cc/2021/Conference/Paper{paper_num}/-/Decision',
                'decision_field': 'decision'
            },
            2022: {
                'invitation': 'ICLR.cc/2022/Conference/-/Blind_Submission',
                'decision_pattern': 'ICLR.cc/2022/Conference/Paper{paper_num}/-/Decision',
                'decision_field': 'decision'
            },
            2023: {
                'invitation': 'ICLR.cc/2023/Conference/-/Blind_Submission',
                'decision_pattern': 'ICLR.cc/2023/Conference/Paper{paper_num}/-/Decision',
                'decision_field': 'decision'
            }
        }
    
    def scrape_year(self, year: int, download_pdfs: bool = True, resume: bool = True) -> List[Dict]:
        """
        Scrape a complete year of ICLR papers.
        
        Args:
            year: Year to scrape
            download_pdfs: Whether to download PDF files
            resume: Whether to resume from existing data
            
        Returns:
            List of paper metadata dictionaries
        """
        output_dir = 'data'  # Fixed output directory
        logger.info(f"🎯 Starting ICLR {year} scrape...")
        
        # Check if already completed and resume is enabled
        metadata_file = os.path.join(output_dir, 'metadata', 'iclr', f'iclr_{year}.json')
        if resume and os.path.exists(metadata_file):
            logger.info(f"Found existing metadata file: {metadata_file}")
            try:
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    existing_papers = json.load(f)
                if existing_papers:  # Make sure it's not empty
                    logger.info(f"✅ Resumed: {len(existing_papers)} papers already scraped for {year}")
                    return existing_papers
            except Exception as e:
                logger.warning(f"Failed to load existing metadata: {e}, starting fresh")
        
        # Get accepted papers
        try:
            accepted_papers = self.get_papers_for_year(year)
        except Exception as e:
            logger.error(f"Failed to get papers for {year}: {e}")
            return []  # Return empty list on error
        
        if not accepted_papers:
            logger.warning(f"❌ No accepted papers found for ICLR {year}")
            return []
        
        # Save papers and optionally download PDFs
        try:
            self.save_papers(accepted_papers, year, output_dir, download_pdfs=download_pdfs)
            logger.info(f"✅ ICLR {year} complete: {len(accepted_papers)} accepted papers saved")
        except Exception as e:
            logger.error(f"Failed to save papers for {year}: {e}")
            return []
        
        # Convert to metadata format for return
        metadata = []
        try:
            for paper in accepted_papers:
                paper_id = paper.get('id', '')
                content = paper.get('content', {})
                title = content.get('title', '')
                
                clean_title = self._clean_filename(title)
                pdf_filename = f"{paper_id}_{clean_title}.pdf"
                pdf_path = os.path.join('data', 'papers', 'iclr', str(year), pdf_filename)
                
                # Prepare PDF URL (make absolute if relative)
                pdf_url = content.get('pdf', '')
                if pdf_url and not pdf_url.startswith('http'):
                    pdf_url = f"https://openreview.net{pdf_url}"
                
                paper_metadata = {
                    'id': paper_id,
                    'title': title,
                    'authors': content.get('authors', []),
                    'abstract': content.get('abstract', ''),
                    'pdf_url': pdf_url,
                    'openreview_url': f"https://openreview.net/forum?id={paper_id}",
                    'year': year,
                    'conference': 'iclr',
                    'url': paper_id,
                    'pdf_path': pdf_path
                }
                metadata.append(paper_metadata)
            
            logger.info(f"Returning {len(metadata)} papers metadata for main.py")
            return metadata
            
        except Exception as e:
            logger.error(f"Failed to convert papers to metadata format: {e}")
            return []
    
    def scrape_multiple_years(self, years: List[int], download_pdfs: bool = True, resume: bool = True) -> Dict[int, List[Dict]]:
        """
        Scrape multiple years of ICLR papers.
        
        Args:
            years: List of years to scrape
            download_pdfs: Whether to download PDF files
            resume: Whether to resume from existing data
            
        Returns:
            Dictionary mapping year to list of paper metadata
        """
        logger.info(f"🚀 Starting ICLR scrape for years: {years}")
        
        results = {}
        for year in sorted(years):
            try:
                papers = self.scrape_year(year, download_pdfs=download_pdfs, resume=resume)
                results[year] = papers
                logger.info(f"Completed {year}. Waiting before next year...")
                time.sleep(10)  # Longer pause between years
            except Exception as e:
                logger.error(f"Failed to scrape ICLR {year}: {e}")
                results[year] = []
                continue
        
        logger.info("🏆 All ICLR years completed!")
        return results
    
    def get_papers_for_year(self, year: int) -> List[Dict]:
        """Get all accepted papers for a given year."""
        if year not in self.year_patterns:
            logger.error(f"Year {year} not supported. Available years: {list(self.year_patterns.keys())}")
            return []
        
        logger.info(f"Getting ICLR {year} papers...")
        
        # Step 1: Get all submitted papers
        submitted_papers = self._get_submitted_papers(year)
        if not submitted_papers:
            logger.error(f"No submitted papers found for {year}")
            return []
        
        logger.info(f"Found {len(submitted_papers)} submitted papers for {year}")
        
        # Step 2: Filter for accepted papers only
        accepted_papers = []
        
        for i, paper in enumerate(submitted_papers):
            logger.info(f"Processing paper {i+1}/{len(submitted_papers)}: {paper.get('content', {}).get('title', 'No title')[:50]}...")
            
            # Get decision
            decision = self._get_paper_decision(paper, year)
            
            # Check if accepted (using your smart detection logic)
            if decision and 'accept' in decision.lower():
                logger.info(f"✅ ACCEPTED: {decision}")
                
                # Add decision to paper metadata
                paper['decision'] = decision
                accepted_papers.append(paper)
            else:
                if decision:
                    logger.debug(f"❌ Not accepted: {decision}")
                else:
                    logger.debug(f"❌ No decision found")
            
            # Rate limiting - be nice to OpenReview
            time.sleep(1.5)
        
        logger.info(f"Found {len(accepted_papers)} accepted papers out of {len(submitted_papers)} total ({len(accepted_papers)/len(submitted_papers)*100:.1f}% acceptance rate)")
        return accepted_papers
    
    def _get_submitted_papers(self, year: int) -> List[Dict]:
        """Get all submitted papers for a year with proper pagination."""
        pattern = self.year_patterns[year]
        
        params = {
            'invitation': pattern['invitation'],
            'details': 'replyCount,invitation,original'
        }
        
        papers = []
        offset = 0
        limit = 1000  # OpenReview limit per request
        
        while True:
            params['offset'] = offset
            params['limit'] = limit
            
            logger.info(f"Fetching papers {offset+1}-{offset+limit}...")
            response = self._make_api_request(self.api_base, params)
            if not response:
                logger.error(f"Failed to get papers at offset {offset}")
                break
            
            batch_papers = response.get('notes', [])
            if not batch_papers:
                logger.info(f"No more papers found at offset {offset}")
                break
            
            papers.extend(batch_papers)
            logger.info(f"Retrieved {len(batch_papers)} papers (total so far: {len(papers)})")
            
            # If we got fewer papers than the limit, we've reached the end
            if len(batch_papers) < limit:
                logger.info(f"Reached end of papers (got {len(batch_papers)} < {limit})")
                break
            
            offset += limit
            time.sleep(2.0)  # Rate limiting between batches - be extra careful
        
        logger.info(f"✅ Total papers retrieved for {year}: {len(papers)}")
        return papers
    
    def _get_paper_decision(self, paper: Dict, year: int) -> Optional[str]:
        """Get decision for a paper."""
        paper_id = paper.get('id')
        paper_number = paper.get('number')
        
        if not paper_id:
            return None
        
        pattern = self.year_patterns[year]
        
        # Get all forum notes for this paper
        params = {'forum': paper_id}
        response = self._make_api_request(self.api_base, params)
        
        if not response:
            return None
        
        forum_notes = response.get('notes', [])
        
        # Look for decision note using year-specific patterns
        for note in forum_notes:
            invitation = note.get('invitation', '')
            content = note.get('content', {})
            
            # Check if this is a decision note based on invitation pattern
            is_decision_note = False
            
            if year == 2018:
                # 2018 uses simple Acceptance_Decision pattern
                is_decision_note = 'Acceptance_Decision' in invitation
            elif year == 2017:
                # 2017 uses paper-specific acceptance pattern
                is_decision_note = '/acceptance' in invitation
            else:
                # 2019-2023 use various patterns with paper numbers
                if paper_number:
                    expected_patterns = [
                        f'Paper{paper_number}/-/Decision',
                        f'Paper{paper_number}/Meta_Review',
                        f'Paper{paper_number}/-/Meta_Review'
                    ]
                    is_decision_note = any(pattern in invitation for pattern in expected_patterns)
            
            if is_decision_note:
                # Extract decision using the field name for this year
                decision_field = pattern['decision_field']
                decision = content.get(decision_field)
                
                if decision:
                    logger.debug(f"Found decision in field '{decision_field}': {decision}")
                    return decision
        
        return None
    
    def _make_api_request(self, url: str, params: Dict, max_retries: int = 5) -> Optional[Dict]:
        """Make API request with retry logic."""
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=30)
                
                if response.status_code == 429:
                    # Rate limited
                    wait_time = min((2 ** attempt) * 10, 120)  # Exponential backoff, max 2 minutes
                    logger.warning(f"Rate limited. Waiting {wait_time} seconds... (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
                
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"API request failed with status {response.status_code}")
                    return None
                    
            except requests.exceptions.Timeout:
                logger.warning(f"Request timeout (attempt {attempt+1}/{max_retries})")
                time.sleep(5)
                continue
            except Exception as e:
                logger.error(f"API request error: {e}")
                if attempt == max_retries - 1:
                    return None
                time.sleep(5)
        
        logger.error(f"Failed to make API request after {max_retries} attempts")
        return None
    
    def save_papers(self, papers: List[Dict], year: int, output_dir: str, download_pdfs: bool = True) -> None:
        """Save papers metadata and optionally download PDFs."""
        if not papers:
            logger.warning(f"No papers to save for {year}")
            return
        
        # Create directory structure: data/metadata/iclr/ and data/papers/iclr/YEAR/
        metadata_dir = os.path.join(output_dir, 'metadata', 'iclr')
        pdf_dir = os.path.join(output_dir, 'papers', 'iclr', str(year))
        os.makedirs(metadata_dir, exist_ok=True)
        if download_pdfs:
            os.makedirs(pdf_dir, exist_ok=True)
        
        # Prepare metadata for JSON
        metadata = []
        
        for paper in papers:
            paper_id = paper.get('id', '')
            content = paper.get('content', {})
            title = content.get('title', '')
            
            # Create clean filename for PDF
            clean_title = self._clean_filename(title)
            pdf_filename = f"{paper_id}_{clean_title}.pdf"
            pdf_path = os.path.join('data', 'papers', 'iclr', str(year), pdf_filename)
            
            # Create OpenReview forum URL
            openreview_url = f"https://openreview.net/forum?id={paper_id}"
            
            # Prepare PDF URL (make absolute if relative)
            pdf_url = content.get('pdf', '')
            if pdf_url and not pdf_url.startswith('http'):
                pdf_url = f"https://openreview.net{pdf_url}"
            
            # Extract metadata in your specified format
            paper_metadata = {
                'id': paper_id,
                'title': title,
                'authors': content.get('authors', []),
                'abstract': content.get('abstract', ''),
                'pdf_url': pdf_url,
                'openreview_url': openreview_url,
                'year': year,
                'conference': 'iclr',
                'url': paper_id,
                'pdf_path': pdf_path
            }
            
            metadata.append(paper_metadata)
            
            # Download PDF if requested
            if download_pdfs and pdf_url:
                self._download_pdf(pdf_url, pdf_filename, pdf_dir)
            elif download_pdfs:
                logger.warning(f"No PDF URL found for paper {paper_id}")
        
        # Save metadata as JSON: data/metadata/iclr/iclr_YEAR.json
        metadata_file = os.path.join(metadata_dir, f'iclr_{year}.json')
        with open(metadata_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Saved {len(papers)} papers metadata to {metadata_file}")
        if download_pdfs:
            logger.info(f"PDFs saved to {pdf_dir}")
        else:
            logger.info(f"PDF download skipped (--no-pdfs flag)")
    
    def scrape_all_years(self, output_dir: str = 'data', download_pdfs: bool = True) -> None:
        """
        Scrape all available ICLR years (for backwards compatibility).
        
        Args:
            output_dir: Output directory
            download_pdfs: Whether to download PDFs
        """
        years = list(self.year_patterns.keys())
        self.scrape_multiple_years(years, download_pdfs=download_pdfs, output_dir=output_dir)
    
    def _download_pdf(self, pdf_url: str, pdf_filename: str, pdf_dir: str) -> None:
        """Download PDF with retry logic."""
        if not pdf_url.startswith('http'):
            # Relative URL, make absolute
            pdf_url = f"https://openreview.net{pdf_url}"
        
        filepath = os.path.join(pdf_dir, pdf_filename)
        
        # Skip if already downloaded
        if os.path.exists(filepath):
            logger.debug(f"PDF already exists: {pdf_filename}")
            return
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.debug(f"Downloading PDF: {pdf_filename}")
                response = self.session.get(pdf_url, timeout=60, stream=True)
                
                if response.status_code == 200:
                    with open(filepath, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    
                    logger.debug(f"✅ Downloaded: {pdf_filename}")
                    return
                else:
                    logger.warning(f"PDF download failed with status {response.status_code}: {pdf_url}")
                    return
                    
            except Exception as e:
                logger.warning(f"PDF download error (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(5)
        
        logger.error(f"Failed to download PDF after {max_retries} attempts: {pdf_url}")
    
    def _clean_filename(self, title: str, max_length: int = 50) -> str:
        """Clean title for use in filename."""
        if not title:
            return "untitled"
        
        # Remove problematic characters
        import re
        cleaned = re.sub(r'[<>:"/\\|?*]', '', title)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        
        # Truncate if too long
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length].strip()
        
        return cleaned
    
    def scrape_all_years(self, output_dir: str = 'data', years: Optional[List[int]] = None) -> None:
        """Scrape all available ICLR years."""
        if years is None:
            years = list(self.year_patterns.keys())
        
        logger.info(f"🚀 Starting ICLR scrape for years: {years}")
        
        for year in sorted(years):
            try:
                papers = self.scrape_year(year, download_pdfs=True, resume=True)
                logger.info(f"Completed {year}: {len(papers)} papers. Waiting before next year...")
                time.sleep(10)  # Longer pause between years
            except Exception as e:
                logger.error(f"Failed to scrape ICLR {year}: {e}")
                continue
        
        logger.info("🏆 All ICLR years completed!")


# Example usage
if __name__ == "__main__":
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Create scraper
    scraper = ICLRScraper()
    
    # Scrape specific year
    # scraper.scrape_year(2023)
    
    # Or scrape all years - will create:
    # data/metadata/iclr/iclr_2017.json, iclr_2018.json, etc.
    # data/papers/iclr/2017/, 2018/, etc.
    scraper.scrape_all_years(years=[2017, 2018, 2019, 2020, 2021, 2022, 2023])