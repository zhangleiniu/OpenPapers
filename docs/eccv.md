# ECCV (European Conference on Computer Vision)

## Source

All years: `https://www.ecva.net/papers.php`

Note: ECCV is held in even years only.

## Dataset coverage

See the generated [coverage and quality report](../statistics.md).

All available years are listed on a single page at
`https://www.ecva.net/papers.php`.

## Data fields

| Field | Notes |
|-------|-------|
| `id` | Filename stem from paper URL (e.g. `1234_ECCV_2024_paper`) |
| `title` | ✓ |
| `authors` | Trailing `*` (e.g. corresponding author markers) are stripped |
| `abstract` | ✓ |
| `pdf_url` | Resolved from relative `<a href>` on paper page via `urljoin` |
| `bibtex_extra` | booktitle/pages/doi/isbn/organization fetched via CrossRef DOI content negotiation (`Accept: application/x-bibtex` on `https://doi.org/<DOI>`), since the ecva.net mirror we scrape has no BibTeX of its own but does link the Springer DOI (one extra request, best-effort) — see [data-schema.md](data-schema.md#bibtex-generation) |

## Known issues

None.
