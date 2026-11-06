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
Only the named conference's own proceedings are scraped: main (or
long/short) papers plus the industry track. Findings volumes are a separate
publication and are never included (enforced in code, regardless of the
cached labels); system demonstrations, tutorials, student research
workshops, workshops, and co-located events are excluded too.

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
| `abstract` | Source page; older years backfilled from the PDF by GROBID (`abstract_source: grobid`) |
| `pdf_url` | ✓ |
| `bibtex_extra` | editor/address/pages/doi/organization parsed from the Anthology's own BibTeX export block on the same page (no extra request) — see [data-schema.md](data-schema.md#bibtex-generation) |

## Known issues

- NAACL is not held every year. Do not infer missing editions from the first
  and last year shown in a range; the generated report lists actual years.
- **Years through 2012 generally have no `abstract`** on the Anthology
  source page (not a scraping bug) — the scraper saves `abstract: ""`. In
  the canonical corpus these gaps were backfilled from the PDFs with GROBID
  on 2026-11-05 (`abstract_source: grobid`); the procedure, quality check,
  and known text artifacts are described under ACL's
  [known issues](acl.md#known-issues). Remaining gaps are mostly papers
  without an abstract and image-only or garbled PDFs; `statistics.md` has
  the current count. For an initial scrape of an old year use
  `--completeness-level announced`, then backfill from GROBID output.
- **2001, `N01-1022`**: the ACL Anthology PDF URL returns 404. Metadata is
  retained with `pdf_downloaded: false`; no alternative source identified yet.
