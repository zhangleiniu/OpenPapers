# utils.py
"""Robust utilities with error handling."""

import os
import requests
import time
import json
import re
import random
from pathlib import Path
from typing import List, Dict, Optional
import logging
import unicodedata
from collections import defaultdict

from config import DEFAULT_REQUEST_DELAY, DEFAULT_RETRY_ATTEMPTS, DEFAULT_TIMEOUT, USER_AGENT

logger = logging.getLogger(__name__)


# conference -> (entry_type, venue_field, venue_value, organization_or_None)
VENUE = {
    "aaai":    ("inproceedings", "booktitle", "Proceedings of the AAAI Conference on Artificial Intelligence", None),
    "acl":     ("inproceedings", "booktitle", "Proceedings of the Annual Meeting of the Association for Computational Linguistics", None),
    "emnlp":   ("inproceedings", "booktitle", "Proceedings of the Conference on Empirical Methods in Natural Language Processing", None),
    "naacl":   ("inproceedings", "booktitle", "Proceedings of the Conference of the North American Chapter of the Association for Computational Linguistics", None),
    "cvpr":    ("inproceedings", "booktitle", "Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition", None),
    "iccv":    ("inproceedings", "booktitle", "Proceedings of the IEEE/CVF International Conference on Computer Vision", None),
    "eccv":    ("inproceedings", "booktitle", "European Conference on Computer Vision", "Springer"),
    "iclr":    ("inproceedings", "booktitle", "International Conference on Learning Representations", None),
    "icml":    ("inproceedings", "booktitle", "International Conference on Machine Learning", "PMLR"),
    "aistats": ("inproceedings", "booktitle", "International Conference on Artificial Intelligence and Statistics", "PMLR"),
    "colt":    ("inproceedings", "booktitle", "Conference on Learning Theory", "PMLR"),
    "uai":     ("inproceedings", "booktitle", "Conference on Uncertainty in Artificial Intelligence", "PMLR"),
    "ijcai":   ("inproceedings", "booktitle", "International Joint Conference on Artificial Intelligence", None),
    "neurips": ("inproceedings", "booktitle", "Advances in Neural Information Processing Systems", None),
    "jmlr":    ("article", "journal", "Journal of Machine Learning Research", None),
}

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "on", "in", "of", "to",
    "for", "and", "or", "how", "why", "what", "when", "where", "with", "without",
    "from", "by", "at", "as", "into", "via", "do", "does",
}

PARTICLES = {
    "van", "von", "de", "del", "della", "der", "di", "da", "dos", "du",
    "la", "le", "el", "den", "ten", "ter", "vande",
}

ACCENTS = {
    "ä": '{\\"a}', "ö": '{\\"o}', "ü": '{\\"u}', "ë": '{\\"e}', "ï": '{\\"i}', "ÿ": '{\\"y}',
    "Ä": '{\\"A}', "Ö": '{\\"O}', "Ü": '{\\"U}',
    "á": "{\\'a}", "é": "{\\'e}", "í": "{\\'i}", "ó": "{\\'o}", "ú": "{\\'u}", "ý": "{\\'y}",
    "Á": "{\\'A}", "É": "{\\'E}", "ç": "{\\c c}", "Ç": "{\\c C}",
    "à": "{\\`a}", "è": "{\\`e}", "ì": "{\\`i}", "ò": "{\\`o}", "ù": "{\\`u}",
    "â": "{\\^a}", "ê": "{\\^e}", "î": "{\\^i}", "ô": "{\\^o}", "û": "{\\^u}",
    "ñ": "{\\~n}", "ã": "{\\~a}", "õ": "{\\~o}",
    "ß": "{\\ss}", "ø": "{\\o}", "Ø": "{\\O}", "å": "{\\aa}", "Å": "{\\AA}",
    "ł": "{\\l}", "Ł": "{\\L}", "č": "{\\v c}", "š": "{\\v s}", "ž": "{\\v z}",
    "ś": "{\\'s}", "ń": "{\\'n}", "ć": "{\\'c}", "ą": "{\\k a}", "ę": "{\\k e}",
}

SPECIAL = {
    "&": "\\&", "%": "\\%", "$": "\\$", "#": "\\#", "_": "\\_",
    "{": "\\{", "}": "\\}",
    "~": "\\textasciitilde{}", "^": "\\textasciicircum{}", "\\": "\\textbackslash{}",
}


def create_gemini_model(system_prompt: str):
    """Initialize Vertex AI and return a GenerativeModel, or None if not configured.

    Reads GCP_PROJECT_ID, GCP_LOCATION, and GEMINI_MODEL from the environment.
    Safe to call multiple times — vertexai.init() is idempotent.
    """
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel
    except ImportError:
        logger.error("vertexai package not installed.")
        return None

    project_id = os.getenv("GCP_PROJECT_ID")
    if not project_id:
        logger.warning("GCP_PROJECT_ID not set — LLM features disabled.")
        return None

    location   = os.getenv("GCP_LOCATION", "us-central1")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel(model_name=model_name, system_instruction=system_prompt)
        logger.info(f"Vertex AI initialized with model {model_name}")
        return model
    except Exception as e:
        logger.error(f"Failed to initialize Vertex AI: {e}")
        return None


def llm_json_config():
    """Return a GenerationConfig for strict JSON output at low temperature.

    Returned as a function (not a module-level constant) to avoid importing
    vertexai at module load time for scrapers that don't need it.
    """
    from vertexai.generative_models import GenerationConfig
    return GenerationConfig(response_mime_type="application/json", temperature=0.1)


class RobustSession:
    """HTTP session with robust error handling and rate limiting."""
    
    def __init__(self, delay: float = DEFAULT_REQUEST_DELAY, 
                 retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
                 timeout: int = DEFAULT_TIMEOUT):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        self.delay = delay
        self.retry_attempts = retry_attempts
        self.timeout = timeout
        self.last_request = 0
        self.rate_limited_until = 0
    
    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        """Make GET request with retry and rate-limit handling."""
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> Optional[requests.Response]:
        """Make POST request with retry and rate-limit handling."""
        return self._request("POST", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """Internal: execute an HTTP request with retries and rate limiting."""
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
                    sleep_time = self.delay - elapsed + random.uniform(0, 0.1)
                    time.sleep(sleep_time)

                self.last_request = time.time()

                timeout = kwargs.pop('timeout', self.timeout)
                response = self.session.request(method, url, timeout=timeout, **kwargs)

                if response.status_code == 200:
                    return response
                elif response.status_code == 429:
                    retry_after = response.headers.get('Retry-After', 60)
                    self._handle_rate_limit(int(retry_after))
                    continue
                elif response.status_code in [500, 502, 503, 504]:
                    logger.warning(f"Server error {response.status_code} for {url}, attempt {attempt + 1}")
                    time.sleep(2 ** attempt)
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
        conf_dir.mkdir(parents=True, exist_ok=True)
        
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
    




def _ascii_fold(s):
    s = (s.replace("ß", "ss").replace("ø", "o").replace("Ø", "O")
           .replace("ł", "l").replace("Ł", "L").replace("đ", "d").replace("Đ", "D"))
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _latex_escape(text):
    out = []
    for ch in text:
        if ch in ACCENTS:
            out.append(ACCENTS[ch])
        elif ch in SPECIAL:
            out.append(SPECIAL[ch])
        else:
            out.append(ch)
    return "".join(out)


def _split_name(fullname):
    """('Nafie El Amrani') -> ('Nafie', 'El Amrani'); mononym -> ('', name)."""
    toks = fullname.split()
    if not toks:
        return "", ""
    if len(toks) == 1:
        return "", toks[0]
    surname = [toks[-1]]
    i = len(toks) - 2
    while i >= 1 and toks[i].lower() in PARTICLES:
        surname.insert(0, toks[i])
        i -= 1
    return " ".join(toks[:i + 1]).strip(), " ".join(surname).strip()


def _first_title_keyword(title):
    for word in title.strip().split():
        wl = _ascii_fold(word).lower()
        m = re.match(r"[a-z0-9]+", wl)
        if not m:
            continue
        token = m.group(0)
        if token in STOPWORDS:
            continue
        return token
    return ""


def _base_cite_key(conf, year, title, authors):
    _, surname = _split_name(authors[0])
    last_token = surname.split()[-1] if surname else ""
    sk = re.sub(r"[^a-z0-9]", "", _ascii_fold(last_token).lower())
    return f"{sk}{year}{_first_title_keyword(title)}"


def _format_authors(authors):
    parts = []
    for a in authors:
        given, surname = _split_name(a)
        if given:
            parts.append(f"{_latex_escape(surname)}, {_latex_escape(given)}")
        else:
            parts.append(_latex_escape(surname))
    return " and ".join(parts)


def _suffix(i):
    s, i = "", i + 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(97 + r) + s
    return s


def _build_bibtex(conf, year, title, authors, key):
    etype, vfield, vvalue, org = VENUE[conf]
    lines = [f"@{etype}{{{key},",
             f"  title={{{_latex_escape(title.strip())}}},",
             f"  author={{{_format_authors(authors)}}},",
             f"  {vfield}={{{vvalue}}},",
             f"  year={{{year}}}"]
    if org:
        lines[-1] += ","
        lines.append(f"  organization={{{org}}}")
    lines.append("}")
    return "\n".join(lines)


def _eligible(paper):
    title = paper.get("title")
    authors = paper.get("authors")
    year = paper.get("year")
    conf = paper.get("conference")
    if not isinstance(conf, str) or conf.lower() not in VENUE:
        return False
    if not isinstance(title, str) or not title.strip():
        return False
    if not isinstance(authors, list) or not authors:
        return False
    if not all(isinstance(a, str) and a.strip() for a in authors):
        return False
    if not (isinstance(year, int) or (isinstance(year, str) and str(year).isdigit())):
        return False
    return True


def assign_bibtex(papers):
    """Set a `bibtex` field on every eligible paper in the list, with cite keys
    made unique within this list. Papers missing title/authors/year are left
    without a `bibtex` field. Deterministic: re-running yields identical keys."""
    eligible = [p for p in papers if _eligible(p)]

    # group by base key, then suffix collisions deterministically
    groups = defaultdict(list)
    for p in eligible:
        conf = p["conference"].lower()
        bk = _base_cite_key(conf, int(p["year"]), p["title"], p["authors"])
        groups[bk].append(p)

    for bk, group in groups.items():
        if len(group) == 1:
            keyed = [(group[0], bk)]
        else:
            ordered = sorted(group, key=lambda p: (str(p.get("id", "")), p["title"]))
            keyed = [(p, bk + _suffix(i)) for i, p in enumerate(ordered)]
        for paper, key in keyed:
            paper["bibtex"] = _build_bibtex(
                paper["conference"].lower(), int(paper["year"]),
                paper["title"], paper["authors"], key)

    return papers
