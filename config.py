# config.py
"""Configuration for multi-conference scraper."""
from dotenv import load_dotenv
load_dotenv()
import os
from pathlib import Path

# Data directory - can be changed with environment variable
DATA_ROOT = Path(os.getenv("SCRAPER_DATA_ROOT", "./data"))
METADATA_DIR = DATA_ROOT / "metadata"
PAPERS_DIR = DATA_ROOT / "papers"
CACHE_DIR = DATA_ROOT / "cache"

# Log file - defaults to project root; override via SCRAPER_LOG_FILE env var
LOG_FILE = Path(os.getenv("SCRAPER_LOG_FILE", "scraper.log"))

# Default HTTP settings
DEFAULT_REQUEST_DELAY = 1.0
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"