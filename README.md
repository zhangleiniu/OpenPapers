# AI/ML Conference Paper Scraper

A Python tool for scraping papers from 15 selected AI/ML conferences and journals.
It extracts high-quality **metadata** and full **PDFs** to support applications like citation analysis and research recommendation.

It applies venue-specific inclusion rules to retain archival main-program content
(including configured long, short, and industry tracks) while excluding workshops,
demos, tutorials, and other secondary material.

---

## Supported Conferences

<!-- BEGIN GENERATED COVERAGE -->
- **NeurIPS** (2000–2025)
- **ICML** (2013–2025; provisional: 2026)
- **ICLR** (2013–2026)
- **AAAI** (2010–2026)
- **CVPR** (2013–2026)
- **COLT** (2011–2026)
- **UAI** (2015–2025)
- **JMLR** (2000–2026)
- **AISTATS** (2009–2025; provisional: 2026)
- **IJCAI** (2017–2025; provisional: 2026)
- **ACL** (2017–2026)
- **EMNLP** (2017–2025)
- **NAACL** (2013, 2015–2016, 2018–2019, 2021–2022, 2024–2025)
- **ICCV** (2013, 2015, 2017, 2019, 2021, 2023, 2025)
- **ECCV** (2018, 2020, 2022, 2024)
<!-- END GENERATED COVERAGE -->

[Generated coverage and quality report](./statistics.md) — regenerate with
`python postprocessing/generate_statistics.py --write` after scraping. The
command also updates the marker-delimited list above; do not edit generated
coverage by hand.

> ⚠️ **Note:** Due to access restrictions, the tool currently **does not support** scraping papers from **KDD**, **TPAMI**, and **ICDM**, as their full metadata or PDFs are not publicly available without a subscription or institutional access.

## Features

- Scrapes paper metadata (title, authors, abstract)
- Generates a BibTeX citation (`bibtex` field) for each paper automatically
- Downloads PDFs automatically
- Resume capability for interrupted scraping
- Year-specific scrapers for different conference formats
- Timely provisional sources before formal proceedings are published
- Robust error handling and rate limiting
- Configurable delays and retry mechanisms

## Installation

1. Clone the repository
2. Create a virtual environment and install dependencies:
```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

3. Create a `.env` file to configure optional paths and credentials required by
the scrapers you use:
```bash
# Data storage root (default: ./data)
SCRAPER_DATA_ROOT=./data

# Log file path (default: scraper.log in project root)
SCRAPER_LOG_FILE=scraper.log

# Required for AAAI, ACL, EMNLP, NAACL, and IJCAI scrapers (LLM track filtering)
GCP_PROJECT_ID=your-project-id
GCP_LOCATION=us-central1
GEMINI_MODEL=gemini-2.5-flash

# Required for authenticated OpenReview API access and protected PDFs
OPENREVIEW_USERNAME=you@example.com
OPENREVIEW_PASSWORD=your-password
```
See [Google Cloud Setup](./docs/GOOGLE_CLOUD_SETUP.md) for Vertex AI configuration.

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

Fill missing abstracts/authors from already-produced GROBID output, falling
back to Nougat output:
```bash
python main.py acl 2026 --enrich-missing
```

Fail the command if required metadata or downloaded PDF files are incomplete:
```bash
python main.py acl 2026 --enrich-missing --require-complete
```

For a newly announced year whose proceedings are not yet available, validate
the public metadata without treating archival publication as complete:
```bash
python main.py aistats 2026 --no-pdfs --require-complete \
  --completeness-level metadata
```

Completeness levels are `announced` (identity, title, and authors), `metadata`
(also abstract), and `archival` (the default strict metadata/PDF contract).
Years sourced from OpenReview or an official accepted-paper page are marked
`provisional` until reconciled with formal proceedings.

`--enrich-missing` consumes the processed files under
`$SCRAPER_DATA_ROOT/{grobid_output,nougat_output}`; it does not launch the
external, resource-intensive GROBID or Nougat pipelines itself. It is safe to
rerun and only fills empty fields.

Start fresh (ignore existing data):
```bash
python main.py aaai 2024 --no-resume
```

### Source monitoring

The versioned registry in `automation/conferences.json` describes conference
years and candidate sources. A cheap deterministic monitor checks OpenReview,
official HTML lists, and PMLR without invoking an LLM:
```bash
python automation/monitor.py --venue icml --year 2026
```

Runtime hashes, counts, and status are stored separately under
`$SCRAPER_DATA_ROOT/monitor/state.sqlite3`. The installed local service uses
this deterministic monitor for daily change/error coverage; it does not use
the monitor as proof that papers are ready or as authority to run a scraper.
See the [current automation deployment](./docs/automation.md).

Prefect, container, and cloud deployment support is an optional component kept
under [`automation/deployment`](./automation/deployment/README.md). The core
scrapers do not require those dependencies.

The target local-first control plane estimates each venue/year's event date
once, sleeps until that date, and then gives the venue/year to Codex or Claude
Code in an isolated worktree. The agent decides readiness, investigates
sources, and may repair and run the scraper; the maintainer reviews and commits
manually. The local LaunchDaemon now has schema-10 state, private configuration
v2, a clean no-remote agent source, bounded production composition, durable
artifacts, worktree retention, and replay-safe Resend report wiring. All new
external effects remain explicitly disabled: no automatic Gemini lookup,
Codex run, or Resend email occurs until its separate live canary and activation
are authorized. The baseline deterministic monitor remains active. See the
[automation system development guide](./docs/automation-system/README.md).


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

Shared settings are defined in `config.py`; venue URLs and venue-specific
delays live in each scraper class:
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
- BibTeX is generated during scraping. The script
  `postprocessing/rebuild_bibtex.py` is retained only for rebuilding
  historical metadata and uses the same generator as the live scraper.
- `postprocessing/backfill_missing_metadata_fields.py` remains available for
  independent bulk repair; `--enrich-missing` exposes the same fallback in the
  main CLI.
- See the [documentation index](./docs/index.md), [data schema](./docs/data-schema.md),
  [pipeline](./docs/pipeline.md), [current automation deployment](./docs/automation.md),
  and [validation guide](./docs/validation.md).

## Citation

If you use this project in your research, please cite our paper, accepted at
the 2026 SIAM International Conference on Data Mining (SDM26). The proceedings
DOI and page numbers are not available yet; this entry links to the current
preprint and will be updated after publication.

```bibtex
@inproceedings{ratul2026masterset,
  title     = {{MasterSet}: A Large-Scale Benchmark for Must-Cite Citation Recommendation in the {AI/ML} Literature},
  author    = {Ratul, Md Toyaha Rahman and Chen, Zhiqian and Fu, Kaiqun and Ji, Taoran and Zhang, Lei},
  booktitle = {Proceedings of the 2026 SIAM International Conference on Data Mining (SDM26)},
  year      = {2026},
  url       = {https://arxiv.org/abs/2604.17680}
}
```

## License

The source code in this repository is licensed under the [MIT License](./LICENSE).

The scraper output is not covered by the software license. This project does
not claim ownership of paper metadata, abstracts, or PDFs retrieved from public
conference and publisher websites. Those materials remain subject to the
rights and terms of their respective authors, publishers, and source websites.

## Motivation

In recent years, the rapid growth of AI and machine learning research has resulted in an overwhelming number of papers published annually, making it increasingly difficult for researchers to stay up to date with developments in their specific subfields. While platforms like Google Scholar, Semantic Scholar, OpenReview, and Paper Copilot attempt to aggregate publication data, our observations suggest that these sources often suffer from incomplete coverage and noisy metadata. To address this gap, we developed a suite of dedicated scrapers targeting the top-tier AI/ML conferences and journals, aiming to build a high-quality, comprehensive dataset of research papers. Our system extracts reliable metadata and downloads full PDFs, which can later be processed using tools like GROBID for structured content analysis. This curated dataset is intended to power downstream applications such as research limitation analysis, citation and reference recommendation, and intelligent paper reading recommendation. Our current focus spans conferences from 2013-ish onward—when deep learning began reshaping the field—though earlier years may also be partially included.


## Limitations

- **Some abstracts are absent from the source pages.** Older proceedings pages
  (notably NAACL 2013/2015/2016 on the ACL Anthology, plus a handful of early
  JMLR and AAAI entries) never recorded abstracts, so a fresh scrape leaves
  those `abstract` fields empty — this is not a scraping bug. These gaps can be
  backfilled from the downloaded PDFs with
  `python postprocessing/backfill_missing_metadata_fields.py --abstract`,
  which extracts the abstract from GROBID TEI output (primary) or Nougat
  markdown (fallback) and records the origin in an `abstract_source` field.
  The generated [quality report](./statistics.md) is the source of truth for
  remaining gaps in the canonical dataset.
