# ICLR (International Conference on Learning Representations)

## Source

| Years     | Source |
|-----------|--------|
| 2015–2016 | `iclr.cc` static archive pages + `arxiv.org` for abstracts |
| 2017–2025 | `api.openreview.net` / `api2.openreview.net` |
| 2019      | `iclr.cc/Downloads` JSON + OpenReview virtualsite pages |
| 2026+     | `papercopilot/paperlists` GitHub JSON |

## Coverage

2015-2026

## Strategy routing

Different years require different approaches to obtain the accepted paper list.
The scraper selects a strategy automatically:

| Year(s) | Strategy | Notes |
|---------|----------|-------|
| 2015–2016 | Static archive | iclr.cc archive HTML; abstracts fetched individually from arXiv |
| 2017 | `venue` | `venue` field in submission note |
| 2018 | `bulk_decision` | Separate decision notes, joined to submissions |
| 2019 | `downloads` | iclr.cc/Downloads JSON (~1 500 requests vs ~5 000 for per-paper API) |
| 2020 | `per_paper_decision` | One API request per submission to fetch decision |
| 2021 | `mixed` | `venue` field when populated, per-paper decision otherwise |
| 2022–2023 | `venue` | `venue` field in submission note |
| 2024–2025 | `venueid` | OpenReview v2 API, filter by `content.venueid` directly |
| 2026+ | `papercopilot` | papercopilot GitHub JSON; `site` field converted to PDF URL (`forum`→`pdf`) |

## Cache

All strategies share a single cache file at `data/cache/iclr_papers.json`,
keyed by year string. Once a year is cached, no API or web requests are made
on subsequent runs.

## Data fields

| Field | Notes |
|-------|-------|
| `id` | OpenReview forum ID (2017+) or arXiv ID (2015–2016) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ (fetched from arXiv for 2015–2016) |
| `keywords` | ✓ (2017+, empty list for 2015–2016) |
| `pdf_url` | ✓ |
| `openreview_url` | OpenReview forum URL (2017+) |
| `track` | Track name (2015–2016 only) |

## Known issues

- **2015–2016 abstracts**: Fetched individually from arXiv on first parse;
  subsequent runs use the disk cache and do not re-fetch.
- **2020**: `per_paper_decision` strategy is slow (~5 000 API requests).
  No faster alternative exists for this year.
- **2019**: The `downloads` strategy requires a POST to `iclr.cc/Downloads`
  with a CSRF token. If iclr.cc changes its HTML structure, the token
  extraction may break; fall back to `per_paper_decision` by adding 2019
  to `_YEAR_CONFIG` with the same config as 2020.
- **2026+**: Uses the papercopilot JSON which has no `pdf` field. The PDF URL
  is derived from the `site` field by replacing `/forum?` with `/pdf?`. If
  papercopilot changes their JSON schema, the `_papercopilot_entry_to_paper`
  method may need updating. To add future years, just add the year to
  `_PAPERCOPILOT_YEARS` in `scrapers/iclr.py`.