# NAACL (Annual Conference of the North American Chapter of the Association for Computational Linguistics)

## Source

All years: `https://aclanthology.org/events/naacl-[year]/`

## Dataset coverage

See the generated [coverage and quality report](../statistics.md) for the
actual current year range — it is the source of truth, not this file. NAACL
is not held every year.

NAACL's first edition was 2000 (as the combined "ANLP-NAACL 2000"); the
`events/naacl-[year]/` scheme works uniformly back to it, confirmed by direct
fetch. Per the ACL Anthology's own venue index, NAACL was historically held
in 2000, 2001, 2003, 2004, 2006, 2007, 2009, 2010, 2012, and then the
2013+ pattern already reflected in this dataset — i.e. no edition in 2002,
2005, 2008, 2011, matching the same held/skipped pattern seen from 2013
onward.

## Track filtering

ACL Anthology event pages list many volumes per year — main proceedings,
workshops, tutorials, findings, system demonstrations, and co-located events.
Only main-conference proceedings are scraped.

On first run for a given year, the full track list is sent to Gemini (via
Vertex AI) for classification. The result is cached in
`data/cache/naacl_tracks.json`. If the model mislabels a year, edit the cache
file directly and rerun — the cached result will be used as-is.

If the API call fails, a skeleton entry with all tracks set to
`is_full_regular: false` is written to the cache file, and the run is
aborted with instructions to label manually.

## Data fields

| Field | Notes |
|-------|-------|
| `id` | ACL Anthology paper ID (e.g. `2024.naacl-long.1`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | Source page; absent for most pre-~2015 years |
| `pdf_url` | ✓ |

## Known issues

- NAACL is not held every year. Do not infer missing editions from the first
  and last year shown in a range; the generated report lists actual years.
- **Pre-~2015 years generally have no `abstract`** on the Anthology source
  page (not a scraping bug) — records are saved with `abstract: ""`. Recover
  later via `postprocessing/backfill_missing_metadata_fields.py --abstract`
  once PDFs are processed by GROBID/Nougat; use `--completeness-level
  announced` for an initial scrape of old years to avoid `--require-complete`
  flagging them as incomplete.
- **2001, `N01-1022`**: the ACL Anthology PDF URL returns 404. Metadata is
  retained with `pdf_downloaded: false`; no alternative source identified yet.
