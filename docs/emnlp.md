
# EMNLP (Conference on Empirical Methods in Natural Language Processing)

## Source

All years: `https://aclanthology.org/events/emnlp-[year]/`

## Dataset coverage

See the generated [coverage and quality report](../statistics.md) for the
actual current year range — it is the source of truth, not this file.

The `events/emnlp-[year]/` scheme works uniformly back to 1996, EMNLP's first
edition, confirmed by direct fetch.

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
`data/cache/emnlp_tracks.json`. If the model mislabels a year, edit the cache
file directly and rerun — the cached result will be used as-is.

If the API call fails, a skeleton entry with all tracks set to
`is_full_regular: false` is written to the cache file, and the run is
aborted with instructions to label manually.

## Data fields

| Field | Notes |
|-------|-------|
| `id` | ACL Anthology paper ID (e.g. `2024.emnlp-main.1`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | Source page; older years backfilled from the PDF by GROBID (`abstract_source: grobid`) |
| `pdf_url` | ✓ |
| `bibtex_extra` | editor/address/pages/doi/organization parsed from the Anthology's own BibTeX export block on the same page (no extra request) — see [data-schema.md](data-schema.md#bibtex-generation) |

## Known issues

- **Track policy cleanup (2026-11-02)**: Findings records were removed from
  2020–2022 and 2025 and the industry track was added to 2022–2025, so every
  year follows the policy above.
- **2022, `2022.emnlp-main.804`**: ACL Anthology retains the official metadata
  but exposes no PDF URL. The paper is retained and uses arXiv `2112.10684`,
  recorded with `pdf_source: arxiv` and `pdf_url`
  `https://arxiv.org/pdf/2112.10684`.
- **2024, `2024.emnlp-industry.61` and `.67`**: the Anthology page has no
  abstract; it was copied from the PDF's first page (`abstract_source:
  pdf_text`).
- **Years through 2016 generally have no `abstract`** on the Anthology
  source page (not a scraping bug) — the scraper saves `abstract: ""`. In
  the canonical corpus these gaps were backfilled from the PDFs with GROBID
  on 2026-11-05 (`abstract_source: grobid`); the procedure, quality check,
  and known text artifacts are described under ACL's
  [known issues](acl.md#known-issues). Remaining gaps are mostly papers
  without an abstract and image-only or garbled PDFs; `statistics.md` has
  the current count. For an initial scrape of an old year use
  `--completeness-level announced`, then backfill from GROBID output.
