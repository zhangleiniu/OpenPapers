# IJCAI (International Joint Conference on Artificial Intelligence)

## Source

All years: `ijcai.org`

## Coverage

2017-2025

## Track filtering

IJCAI proceedings pages list many tracks — main track, workshops, special
tracks, demonstrations, doctoral consortium, surveys, etc. Only
main-conference proceedings are scraped.

On first run for a given year, the full track list is sent to Gemini (via
Vertex AI) for classification. The result is cached in
`data/cache/ijcai_tracks.json`. If the model mislabels a year, edit the
cache file directly and rerun — the cached result will be used as-is.

If the API call fails, a skeleton entry with all tracks set to
`is_full_regular: false` is written to the cache file, and the run is
aborted with instructions to label manually.

## Data fields

| Field | Notes |
|-------|-------|
| `id` | Derived from URL: `{year}-{paper_number}` (e.g. `2024-42`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ |
| `pdf_url` | ✓ |

## Known issues

- **Pre-2017**: Proceedings exist on `ijcai.org` but years are not contiguous and the page structure differs from 2017+. Not currently implemented.