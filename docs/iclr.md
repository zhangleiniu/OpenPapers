# ICLR (International Conference on Learning Representations)

## Source

| Years     | Source |
|-----------|--------|
| 2013      | `api.openreview.net` (decision field in content) |
| 2014–2016 | `iclr.cc` static archive pages + `arxiv.org` for abstracts and PDFs |
| 2017–2023 | `api.openreview.net` (various strategies per year) |
| 2019      | `iclr.cc/Downloads` JSON + OpenReview virtualsite pages |
| 2024–2026 | `api2.openreview.net` (v2 API, `content.venueid`) |

## Coverage

2013-2026

## Strategy routing

Different years require different approaches to obtain the accepted paper list.
The scraper selects a strategy automatically:

| Year(s) | Strategy | Notes |
|---------|----------|-------|
| 2013 | `decision_content` | Filters by decision field prefix in note content |
| 2014 | `archive` (Google Sites) | Dedicated parser for iclr.cc/archive/2014 page; PDFs from arXiv |
| 2015–2016 | `archive` (DokuWiki) | iclr.cc archive HTML; workshop papers excluded; abstracts fetched from arXiv |
| 2017 | `venue` | `venue` field in submission note |
| 2018 | `bulk_decision` | Separate decision notes, joined to submissions |
| 2019 | `downloads` | iclr.cc/Downloads JSON (~1 500 requests vs ~5 000 for per-paper API) |
| 2020 | `per_paper_decision` | One API request per submission to fetch decision |
| 2021 | `mixed` | `venue` field when populated, per-paper decision otherwise |
| 2022–2023 | `venue` | `venue` field in submission note |
| 2024–2026 | `venueid` | OpenReview v2 API, filter by `content.venueid` directly |

## Cache

All strategies share a single cache file at `data/cache/iclr_papers.json`,
keyed by year string. Once a year is cached, no API or web requests are made
on subsequent runs.

## Data fields

| Field | Notes |
|-------|-------|
| `id` | OpenReview forum ID (2017+) or arXiv ID (2013–2016) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ (fetched from arXiv for 2014–2016) |
| `keywords` | ✓ (2017+, empty list for 2014–2016) |
| `pdf_url` | arXiv PDF (2014–2016), OpenReview PDF (2017+) |
| `openreview_url` | OpenReview forum URL (2017+) |
| `track` | Track name (2015–2016 only, e.g. "Main Conference - Oral Presentations") |
| `status` | Oral / Spotlight / Poster (when available from venue field) |

## Known issues

- **2023 venue labels**: ICLR 2023 used "notable top 5%" / "notable top 25%"
  instead of Oral / Spotlight. The venue filter accepts these (keyword
  "notable") and maps them to `status` Oral / Spotlight respectively.
  Data scraped before 2026-07 misses these ~370 papers — re-scrape 2023
  after deleting the `"2023"` key from `data/cache/iclr_papers.json`.
- **2014**: The 2014 archive page is a Google Sites page with a different HTML
  structure from the 2015–2016 DokuWiki pages. A dedicated parser handles it.
- **2015–2016 abstracts**: Fetched individually from arXiv on first parse;
  subsequent runs use the disk cache and do not re-fetch.
- **2020**: `per_paper_decision` strategy is slow (~5 000 API requests).
  No faster alternative exists for this year.
- **2019**: The `downloads` strategy requires a POST to `iclr.cc/Downloads`
  with a CSRF token. If iclr.cc changes its HTML structure, the token
  extraction may break; fall back to `per_paper_decision` by adding 2019
  to `_YEAR_CONFIG` with the same config as 2020.
