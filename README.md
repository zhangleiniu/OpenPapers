# AI/ML Conference Paper Scraper

A robust Python tool for scraping academic papers from top AI and machine learning conferences.  
It extracts high-quality metadata and full PDFs to support applications like citation analysis and research recommendation.

---

## Supported Conferences

- **NeurIPS**(2000–2025)
- **ICML**(2013–2025)
- **ICLR**(2013–2025) 
- **AAAI**(2010–2025)
- **CVPR**(2012-2025)
- **COLT**(2011-2024)
- **UAI**(2015-2024)
- **JMLR**(2000-2025)
- **AISTATS**(2009-2024)
- **IJCAI**(2017-2024)
- **ACL**(2017-2025)
- **EMNLP**(2017-2024)
- **NAACL**(2013-2025)
- **ICCV**(2013-2023) 
- **ECCV**(2018-2024)

[Detailed Statistics](./statistics.md)

## Features

- Scrapes paper metadata (title, authors, abstract)
- Downloads PDFs automatically
- Resume capability for interrupted scraping
- Year-specific scrapers for different conference formats
- Robust error handling and rate limiting
- Configurable delays and retry mechanisms

## Installation

1. Clone the repository
2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file (optional) to configure data directory:
```bash
SCRAPER_DATA_ROOT=./data
```

## Usage

### Command Line Interface

List available conferences:
```bash
python main.py --list-conferences
```

Scrape a single year:
```bash
python main.py neurips 2022
```

Scrape multiple years:
```bash
python main.py iclr 2020 2021 2022
```

Skip PDF downloads (metadata only):
```bash
python main.py icml 2023 --no-pdfs
```

Start fresh (ignore existing data):
```bash
python main.py aaai 2024 --no-resume
```


## Data Structure

Papers are saved in the following structure:
```
data/
├── metadata/
│   └── conference/
│       └── conference_year.json
└── papers/
    └── conference/
        └── year/
            └── paper_files.pdf
```

## Configuration

Conference-specific settings are defined in `config.py`:
- Request delays and timeouts
- Retry attempts
- Rate limiting parameters
- Base URLs for each conference

## Logging

The scraper generates detailed logs saved to `scraper.log` and displays progress in the console. Use `--verbose` for debug-level logging.

## Notes

- Some conferences have year-specific scrapers for different website formats
- The scraper respects rate limits and includes delays between requests
- PDF downloads are optional and can be skipped for faster metadata collection
- All scraped data is saved incrementally to prevent data loss

## Motivation

In recent years, the rapid growth of AI and machine learning research has resulted in an overwhelming number of papers published annually, making it increasingly difficult for researchers to stay up to date with developments in their specific subfields. While platforms like Google Scholar, Semantic Scholar, OpenReview, and Paper Copilot attempt to aggregate publication data, our observations suggest that these sources often suffer from incomplete coverage and noisy metadata. To address this gap, we developed a suite of dedicated scrapers targeting the top-tier AI/ML conferences and journals, aiming to build a high-quality, comprehensive dataset of research papers. Our system extracts reliable metadata and downloads full PDFs, which can later be processed using tools like GROBID for structured content analysis. This curated dataset is intended to power downstream applications such as research limitation analysis, citation and reference recommendation, and intelligent paper reading recommendation. Our current focus spans conferences from 2013-ish onward—when deep learning began reshaping the field—though earlier years may also be partially included. 