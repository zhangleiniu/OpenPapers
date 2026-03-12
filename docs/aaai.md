# AAAI (AAAI Conference on Artificial Intelligence)

## Source

All years: `https://ojs.aaai.org/index.php/AAAI/issue/archive/`

## Coverage

2010–2025

## Architecture

AAAI proceedings are published as multiple OJS journal issues per year
(e.g. "Technical Tracks Vol. 1–5", plus mixed issues containing IAAI/EAAI/
student content). Scraping requires two levels of LLM filtering:

**Level 1 — Issue filtering** (`data/cache/aaai_pages.json`):
Gemini identifies which issues belong to a given year's main program.
The archive page is checked for freshness on every run; only new issues
(prepended to the archive since the last run) are re-labelled, so
incremental runs are cheap.

**Level 2 — Section filtering** (`data/cache/aaai_tracks.json`):
For each relevant issue, Gemini identifies which sections contain regular
AAAI full papers, excluding IAAI, EAAI, student abstracts, demonstrations,
etc. Section labels are cached per issue URL and never recomputed.

To correct a mislabeled issue or section, edit the relevant cache file
directly and rerun — cached entries are used as-is.

## Data fields

| Field | Notes |
|-------|-------|
| `id` | OJS article ID from URL (e.g. `12345`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ |
| `issue` | Issue title (e.g. `Vol. 38 No. 16: AAAI-24 Technical Tracks`) |
| `section` | Section/track name within the issue |
| `pdf_url` | ✓ |

## Known issues

None.