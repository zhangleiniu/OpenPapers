# NeurIPS (Conference on Neural Information Processing Systems)

## Source

| Years     | Source |
|-----------|--------|
| 1987–2024 | `https://papers.nips.cc/paper_files/paper/[year]` |
| 2025+     | `papercopilot/paperlists` GitHub JSON (automatic fallback when papers.nips.cc returns 404) |

## Coverage

1987-2025

## Data fields

| Field | Notes |
|-------|-------|
| `id` | Hex hash from URL (papers.nips.cc) or OpenReview forum ID (papercopilot) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ |
| `pdf_url` | ✓ |
| `openreview_url` | OpenReview forum URL (papercopilot years only) |

## Known issues

- **2022+ URL format change**: Paper URLs gained a track suffix (e.g. `{hash}-Abstract-Conference.html` → `{hash}-Paper-Conference.pdf`). Pre-2022 URLs have no suffix (`{hash}-Abstract.html` → `{hash}-Paper.pdf`). Both formats are handled.
- **2012, `9e7ba617ad9e69b39bd0c29335b79629`** ("An Integer Optimization Approach to Associative Classification"): No PDF available on the website or elsewhere; record is scraped with metadata only.
- **2012, `12780ea688a71dabc284b064add459a4`** ("A dynamic excitatory-inhibitory network in a VLSI chip for spiking information reregistrations"): No PDF available on the website or elsewhere; record is scraped with metadata only.
- **2025+**: Uses the papercopilot JSON which has no `pdf` field. The PDF URL
  is derived from the `site` field by replacing `/forum?` with `/pdf?`.
  Once papers.nips.cc publishes the proceedings for a given year, the scraper
  will automatically use that instead of papercopilot.