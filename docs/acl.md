# ACL (Annual Meeting of the Association for Computational Linguistics)

## Source

All years: `https://aclanthology.org/events/acl-[year]/`

## Dataset coverage

See the generated [coverage and quality report](../statistics.md). The current
canonical dataset includes ACL 2017–2026.

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
