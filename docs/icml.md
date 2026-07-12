# ICML (International Conference on Machine Learning)

## Source

Archival source: `https://proceedings.mlr.press/`

For 2026, before the PMLR volume is published, the scraper falls back to the
official OpenReview invitation `ICML.cc/2026/Conference/-/Submission`. These
records and PDFs are marked provisional and will be reconciled with PMLR by
OpenReview ID plus normalized title/first-author identity.

Volume discovery is fully dynamic — no hardcoded mappings. The scraper
searches the MLR Press main page for a volume matching "ICML {year}" and
selects the main proceedings over workshop/satellite volumes.

## Dataset coverage

See the generated [coverage and quality report](../statistics.md).

## Volume disambiguation

Multiple PMLR volumes can match "ICML {year}" (e.g. main proceedings +
workshop proceedings). The scraper identifies the main proceedings by
matching titles that end with "ICML {year}" (nothing after the year),
excluding satellite events whose titles contain extra text after the year
(e.g. "Workshop on ...", "GRaM at ICML 2024").

## Data fields

| Field | Notes |
|-------|-------|
| `id` | Paper slug (e.g. `aamand24a`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ |
| `pdf_url` | ✓ |

## Known issues

- **Pre-2013**: Proceedings are not hosted on `proceedings.mlr.press` — each year has its own website with a different structure. Not currently implemented.
