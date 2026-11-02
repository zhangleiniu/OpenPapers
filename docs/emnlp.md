
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
| `abstract` | Source page; absent for most pre-~2015 years |
| `pdf_url` | ✓ |
| `bibtex_extra` | editor/address/pages/doi/organization parsed from the Anthology's own BibTeX export block on the same page (no extra request) — see [data-schema.md](data-schema.md#bibtex-generation) |

## Known issues

- **Track scope drift (pending cleanup)**: before the policy above was fixed
  (2026-11), per-year model labels included Findings for 2020–2022 and 2025
  and omitted the industry track for 2022–2025. Those canonical years still
  reflect the old labels until they are cleaned up (drop Findings, backfill
  industry); 2026 already follows the policy.
- **2022, `2022.emnlp-main.804`**: ACL Anthology retains the official metadata
  but exposes no PDF URL. The paper is retained and uses arXiv `2112.10684`,
  recorded with `pdf_source: arxiv`.
- **Pre-~2015 years generally have no `abstract`** on the Anthology source
  page (not a scraping bug) — records are saved with `abstract: ""`. Recover
  later via `postprocessing/backfill_missing_metadata_fields.py --abstract`
  once PDFs are processed by GROBID/Nougat; use `--completeness-level
  announced` for an initial scrape of old years to avoid `--require-complete`
  flagging them as incomplete.
