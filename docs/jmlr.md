# JMLR (Journal of Machine Learning Research)

## Source

All years: `https://jmlr.org/papers/v[year - 1999]`

Volume number is derived from year: `volume = year - 1999` (v1 = 2000, v2 = 2001, ...).

## Dataset coverage

See the generated [coverage and quality report](../statistics.md).

## Data fields

| Field | Notes |
|-------|-------|
| `id` | Paper slug from URL (e.g. `meila00a`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ |
| `pdf_url` | ✓ |
| `bibtex_extra` | volume/number/pages fetched from the paper's own `.bib` file, linked as `[bib]` on the abstract page (one extra request) — see [data-schema.md](data-schema.md#bibtex-generation) |

## Known issues

- **Volumes 1–5 (2000–2004)**: Abstract HTML structure differs from later volumes — the abstract appears as raw text nodes after an `<h3>Abstract</h3>` tag rather than in a `<p class="abstract">` element. Both formats are handled.
