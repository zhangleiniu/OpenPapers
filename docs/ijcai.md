# IJCAI (International Joint Conference on Artificial Intelligence)

## Source

Archival source: `https://www.ijcai.org/proceedings/[year]/`

For 2026, before that proceedings page exists, the scraper uses the official
conference accepted-paper page filtered to Main Track. It provides title,
authors, abstract, keywords, and the conference paper number, but no PDF URL;
the resulting records are provisional.

## Dataset coverage

See the generated [coverage and quality report](../statistics.md).

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

The 2026 accepted-paper fallback does not use Gemini: the official page exposes
an explicit `main-track` filter and count.

## Data fields

| Field | Notes |
|-------|-------|
| `id` | Derived from URL: `{year}-{paper_number}` (e.g. `2024-42`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ |
| `pdf_url` | ✓ |
| `bibtex_extra` | booktitle/editor/pages/doi/note fetched from the official "BibTeX" download link on the paper page (one extra request) — see [data-schema.md](data-schema.md#bibtex-generation) |

## Known issues

- **Pre-2017**: Proceedings exist on `ijcai.org` but years are not contiguous and the page structure differs from 2017+. Not currently implemented.
- **No PDF storage authorization**: `ijcai.org` is both the official and the
  only archival host for IJCAI papers (unlike ICML/NeurIPS/ICLR, which have a
  separate PMLR/OpenReview/papers.nips.cc mirror). The recorded production
  crawl-policy review
  ([p2-7-production-crawl-policy-review-2026-07-14.md](automation-system/archive/p2-7-production-crawl-policy-review-2026-07-14.md))
  authorizes `metadata_fetch` only for `ijcai.org`, not PDF storage, and no
  other reviewed domain covers IJCAI. This is expected and does not need a
  human decision on every run: an unsupervised agent run should scrape with
  `python main.py ijcai <year> --no-pdfs` (`pdf_url` is still captured — it's
  parsed off the already-fetched proceedings page, not a separate PDF
  request/download) and treat that as `success`, not `needs_human`. Only
  escalate to `needs_human` if the policy itself needs to change (e.g. a
  human wants to authorize PDF storage) or on a genuine access problem.
