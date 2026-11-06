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
Only the named conference's own proceedings are scraped: main (or
long/short) papers plus the industry track. Findings volumes are a separate
publication and are never included (enforced in code, regardless of the
cached labels); system demonstrations, tutorials, student research
workshops, workshops, and co-located events are excluded too.

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
| `bibtex_extra` | editor/address/pages/doi/organization parsed from the Anthology's own BibTeX export block on the same page (no extra request) — see [data-schema.md](data-schema.md#bibtex-generation) |

## Known issues

- **Track policy cleanup (2026-11-02)**: the industry track was added to
  2023 and 2025 (2024 lists none on the Anthology).
- Track classification requires Vertex AI ADC on an uncached year. If it
  fails, the generated all-false cache must be reviewed manually before rerun.
- ACL 2026 source-page abstract gaps were recovered from GROBID and carry
  `abstract_source: grobid`.
- **Years through 2016 generally have no `abstract`** on the Anthology
  source page at all (not a scraping bug) — the scraper saves
  `abstract: ""`. In the canonical corpus these gaps were backfilled from
  the PDFs on 2026-11-05 (GROBID 0.9.1 header parsing, then
  `postprocessing/backfill_missing_metadata_fields.py --abstract`); those
  records carry `abstract_source: grobid` and hold GROBID's unmodified text,
  which can retain footnote markers or a mis-joined line-break hyphen
  (`stateof-the-art`). GROBID abstracts that failed a check against the PDF
  text (garbled text layer, no Abstract heading so the first body paragraph
  was taken, truncation) were not written; their TEI is kept under
  `grobid_output_rejected/` so the backfill cannot read it. The remaining
  gaps are mostly papers without an abstract, image-only scans, and
  OCR-damaged 1980s–1990s scans; `statistics.md` has the current count.
- A fresh scrape of an old year still produces empty abstracts, so
  `--require-complete` at the default `archival`/`metadata` completeness
  level flags it as incomplete — use `--completeness-level announced` for
  the initial scrape, then run GROBID over its PDFs into `grobid_output/` and
  backfill with `--enrich-missing` or the backfill script.
- **1979, `P79-1024`**: the ACL Anthology PDF URL returns 404. Metadata is
  retained with `pdf_downloaded: false`; no alternative source identified yet.
