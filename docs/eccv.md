# ECCV (European Conference on Computer Vision)

## Source

All years: `https://www.ecva.net/papers.php`

For 2026, before ECVA publishes that year, the scraper falls back to the
official virtual site's accepted-poster list
(`https://eccv.ecva.net/virtual/2026/papers.html`) and each poster page. It
provides title, authors, abstract, and the camera-ready PDF hosted on
`media.eventhosts.cc`; the resulting records are provisional. The PDF file is
named by submission number, which ECVA also uses, so provisional IDs
(`7443_ECCV_2026_paper`) match the later archival IDs and the ECVA records
replace them on a later resumed run. The site's `/static` JSON paper feed is
disallowed by robots.txt and is not used.

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

- **2026 provisional source**: Springer's proceedings are subscription-gated
  and ECVA had not yet published 2026 as of 2026-10-19, so 2026 currently
  comes from the virtual site above and does not pass the archival
  completeness level until ECVA publishes.
