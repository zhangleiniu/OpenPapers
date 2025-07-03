# utils.py
"""Robust utilities with error handling."""

import requests
import time
import json
import re
import random
from pathlib import Path
from typing import List, Dict, Optional
import logging

from config import DEFAULT_REQUEST_DELAY, DEFAULT_RETRY_ATTEMPTS, DEFAULT_TIMEOUT, USER_AGENT

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class RobustSession:
    """HTTP session with robust error handling and rate limiting."""
    
    def __init__(self, delay: float = DEFAULT_REQUEST_DELAY, 
                 retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
                 timeout: int = DEFAULT_TIMEOUT):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})
        self.delay = delay
        self.retry_attempts = retry_attempts
        self.timeout = timeout
        self.last_request = 0
        self.rate_limited_until = 0
    
    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        """Make GET request with comprehensive error handling."""
        
        for attempt in range(self.retry_attempts + 1):
            try:
                # Check if we're rate limited
                if time.time() < self.rate_limited_until:
                    wait_time = self.rate_limited_until - time.time()
                    logger.warning(f"Rate limited, waiting {wait_time:.1f}s")
                    time.sleep(wait_time)
                
                # Normal rate limiting
                elapsed = time.time() - self.last_request
                if elapsed < self.delay:
                    sleep_time = self.delay - elapsed
                    # Add small random jitter to avoid thundering herd
                    sleep_time += random.uniform(0, 0.1)
                    time.sleep(sleep_time)
                
                self.last_request = time.time()
                
                # Make request
                response = self.session.get(url, timeout=self.timeout, **kwargs)
                
                # Handle different status codes
                if response.status_code == 200:
                    return response
                elif response.status_code == 429:
                    # Rate limited
                    retry_after = response.headers.get('Retry-After', 60)
                    self._handle_rate_limit(int(retry_after))
                    continue
                elif response.status_code in [500, 502, 503, 504]:
                    # Server errors - retry
                    logger.warning(f"Server error {response.status_code} for {url}, attempt {attempt + 1}")
                    time.sleep(2 ** attempt)  # Exponential backoff
                    continue
                elif response.status_code == 404:
                    logger.warning(f"Not found: {url}")
                    return None
                elif response.status_code == 403:
                    logger.error(f"Access forbidden: {url}")
                    return None
                else:
                    response.raise_for_status()
                    
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout for {url}, attempt {attempt + 1}")
                time.sleep(2 ** attempt)
                
            except requests.exceptions.ConnectionError:
                logger.warning(f"Connection error for {url}, attempt {attempt + 1}")
                time.sleep(2 ** attempt)
                
            except requests.exceptions.RequestException as e:
                logger.error(f"Request failed for {url}: {e}")
                if attempt == self.retry_attempts:
                    return None
                time.sleep(2 ** attempt)
        
        logger.error(f"All retry attempts failed for {url}")
        return None
    
    def _handle_rate_limit(self, retry_after: int):
        """Handle rate limiting."""
        # Set rate limit timeout
        self.rate_limited_until = time.time() + retry_after
        logger.warning(f"Rate limited for {retry_after}s")
    
    def download_file(self, url: str, filepath: Path) -> bool:
        """Download file with error handling."""
        try:
            # Skip if file already exists
            if filepath.exists():
                logger.info(f"File already exists: {filepath.name}")
                return True
            
            response = self.get(url, stream=True)
            if not response:
                return False
            
            # Create directory
            filepath.parent.mkdir(parents=True, exist_ok=True)
            
            # Download with progress for large files
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Log progress for large files
                        if total_size > 1024*1024 and downloaded % (1024*1024) == 0:  # Every MB
                            progress = (downloaded / total_size) * 100
                            logger.debug(f"Download progress: {progress:.1f}%")
            
            logger.info(f"Downloaded: {filepath.name} ({downloaded:,} bytes)")
            return True
            
        except Exception as e:
            logger.error(f"Download failed for {url}: {e}")
            # Clean up partial file
            if filepath.exists():
                try:
                    filepath.unlink()
                except:
                    pass
            return False


def save_papers(papers: List[Dict], conference: str, year: int):
    """Save papers to JSON with error handling."""
    try:
        from config import METADATA_DIR
        
        conf_dir = METADATA_DIR / conference
        conf_dir.mkdir(exist_ok=True)
        
        filepath = conf_dir / f"{conference}_{year}.json"
        
        # Create backup if file exists
        if filepath.exists():
            backup_path = filepath.with_suffix('.json.bak')
            filepath.rename(backup_path)
            logger.info(f"Created backup: {backup_path}")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(papers, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Saved {len(papers)} papers to {filepath}")
        
    except Exception as e:
        logger.error(f"Failed to save papers: {e}")


def load_papers(conference: str, year: int) -> List[Dict]:
    """Load papers from JSON with error handling."""
    try:
        from config import METADATA_DIR
        
        filepath = METADATA_DIR / conference / f"{conference}_{year}.json"
        
        if filepath.exists():
            with open(filepath, 'r', encoding='utf-8') as f:
                papers = json.load(f)
                logger.info(f"Loaded {len(papers)} existing papers")
                return papers
    
    except Exception as e:
        logger.error(f"Failed to load papers: {e}")
    
    return []


def sanitize_filename(filename: str) -> str:
    """Make filename safe for filesystem."""
    # Remove problematic characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Limit length
    return filename[:100].strip()


def get_paper_filename(paper: Dict) -> str:
    """Generate a good filename for a paper."""
    paper_id = paper.get('id', 'unknown')
    title = paper.get('title', '')
    
    if title:
        # Create readable filename with title
        safe_title = sanitize_filename(title)[:50]  # Limit title length
        return f"{paper_id}_{safe_title}.pdf"
    else:
        return f"{paper_id}.pdf"