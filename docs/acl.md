# ACL (Annual Meeting of the Association for Computational Linguistics)

## Source

All years: `https://aclanthology.org/events/acl-[year]/`

## Dataset coverage

See the generated [coverage and quality report](../statistics.md) for the
actual current year range — it is the source of truth, not this file.

The `events/acl-[year]/` scheme works uniformly back to 1979 (the 17th annual
meeting), confirmed by direct fetch. Meetings 1–16 (1963–1978) exist in the
Anthology's broader archive but are not exposed under the same `events/`
page structure, so they would need a separate scraping approach and are not
currently covered.

## Track filtering

ACL Anthology event pages list many volumes per year — main proceedings,
workshops, tutorials, findings, system demonstrations, and co-located events.
**Only main-conference proceedings are scraped**.

On first run for a given year, the full track list is sent to Gemini (via
Vertex AI) for classification. The result is cached in
`data/cache/acl_tracks.json`. If the model mislabels a year, edit the cache
file directly and rerun — the cached result will be used as-is.

If the API call fails, a skeleton entry with all tracks set to
`is_full_regular: false` is written to the cache file, and the run is
aborted with instructions to label manually.

## Data fields

| Field | Notes |
|-------|-------|
| `id` | ACL Anthology paper ID (e.g. `P10-1002`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | Source page, with GROBID/Nougat fallback when absent |
| `pdf_url` | ✓ |

## Known issues

- Track classification requires Vertex AI ADC on an uncached year. If it
  fails, the generated all-false cache must be reviewed manually before rerun.
- ACL 2026 source-page abstract gaps were recovered from GROBID and carry
  `abstract_source: grobid`.
- **Pre-~2015 years generally have no `abstract`** on the Anthology source
  page at all (not a scraping bug) — records are saved with `abstract: ""`.
  Recover these later via
  `postprocessing/backfill_missing_metadata_fields.py --abstract` once PDFs
  are processed by GROBID/Nougat. This means `--require-complete` at the
  default `archival`/`metadata` completeness level will flag these years as
  incomplete until abstracts are backfilled — use `--completeness-level
  announced` for an initial scrape of old years instead.
- **1979, `P79-1024`**: the ACL Anthology PDF URL returns 404. Metadata is
  retained with `pdf_downloaded: false`; no alternative source identified yet.
