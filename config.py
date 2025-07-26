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

# Create directories
METADATA_DIR.mkdir(parents=True, exist_ok=True)
PAPERS_DIR.mkdir(parents=True, exist_ok=True)

# Default HTTP settings
DEFAULT_REQUEST_DELAY = 1.0
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Conference-specific settings
CONFERENCES = {
    'neurips': {
        'name': 'NeurIPS',
        'base_url': 'https://papers.nips.cc/',
        'request_delay': 0.1,
        'retry_attempts': 3,
        'timeout': 30,
        'rate_limit_delay': 60,  # Extra delay if rate limited
    },
    'icml': {
        'name': 'ICML',
        'base_url': 'https://proceedings.mlr.press/',
        'request_delay': 0.15,
        'retry_attempts': 3,
        'timeout': 45,
        'rate_limit_delay': 120,
    },
    'iclr': {
        'name': 'International Conference on Learning Representations', 
        'base_url': 'https://iclr.cc/',
        'request_delay': 0.15,
        'retry_attempts': 3,
        'timeout': 45,
        'rate_limit_delay': 120,
        'years': [2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]
    },
    'aaai': {
        'name': 'AAAI',
        'base_url': 'https://aaai.org/',
        'request_delay': 0.2,
        'retry_attempts': 3,
        'timeout': 30,
        'rate_limit_delay': 90
    }, 
    'cvpr': {
        'name': 'CVPR',
        'base_url': 'https://openaccess.thecvf.com/',
        'request_delay': 0.1,
        'retry_attempts': 3,
        'timeout': 30,
        'rate_limit_delay': 60
    }, 
    'colt': {
        'name': 'COLT',
        'base_url': 'https://proceedings.mlr.press/',
        'request_delay': 0.15,
        'retry_attempts': 3,
        'timeout': 45,
        'rate_limit_delay': 120,
    }, 
    'uai': {
        'name': 'UAI',
        'base_url': 'https://proceedings.mlr.press/',
        'request_delay': 0.15,
        'retry_attempts': 3,
        'timeout': 45,
        'rate_limit_delay': 120, 
        'years': [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    }, 
    'aistats' : {
        'name': 'AISTATS',
        'base_url': 'https://proceedings.mlr.press/',
        'request_delay': 0.15,
        'retry_attempts': 3,
        'timeout': 45,
        'rate_limit_delay': 120,
    }, 
    'jmlr' : {
        'name': 'JMLR',
        'base_url': 'https://www.jmlr.org',
        'request_delay': 0.15,
        'retry_attempts': 3,
        'timeout': 45,
        'rate_limit_delay': 120,
    }, 
    'acl' : {
        'name': 'ACL',
        'base_url': 'https://aclanthology.org', 
        'request_delay': 0.15,
        'retry_attempts': 3,
        'timeout': 45,
        'rate_limit_delay': 120,
    }, 
    'ijcai' : {
        'name': 'IJCAI',
        'base_url': 'https://www.ijcai.org/',
        'request_delay': 0.15,
        'retry_attempts': 3,
        'timeout': 45,
        'rate_limit_delay': 120,
    }
}