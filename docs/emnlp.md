
# EMNLP (Conference on Empirical Methods in Natural Language Processing)

## Source

All years: `https://aclanthology.org/events/emnlp-[year]/`

## Dataset coverage

See the generated [coverage and quality report](../statistics.md).

## Track filtering

ACL Anthology event pages list many volumes per year — main proceedings,
workshops, tutorials, findings, system demonstrations, and co-located events.
Only main-conference proceedings are scraped.

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
| `abstract` | ✓ |
| `pdf_url` | ✓ |

## Known issues

- **2022, `2022.emnlp-main.804`**: ACL Anthology retains the official metadata
  but exposes no PDF URL. The paper is retained and uses arXiv `2112.10684`,
  recorded with `pdf_source: arxiv`.
