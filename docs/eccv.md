# ECCV (European Conference on Computer Vision)

## Source

All years: `https://www.ecva.net/papers.php`

For 2026, before ECVA publishes that year, the scraper falls back to the
official virtual site's accepted-poster list
(`https://eccv.ecva.net/virtual/2026/papers.html`) and each poster page. It
provides title, authors, abstract, and the final camera-ready PDF hosted on
`media.eventhosts.cc`. Springer has published the complete 2,834-paper ECCV
2026 proceedings (LNCS volumes 17001--17078), matching the official virtual
site's 2,834 records, so these records are archival. The PDF file is
named by submission number, which ECVA also uses, so virtual-site IDs
(`7443_ECCV_2026_paper`) match ECVA's IDs and ECVA's metadata is merged into
them on a later resumed run once ECVA adds 2026. The site's `/static` JSON paper feed is
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

- **ECVA DOI links**: the DOI comes from the paper page's "DOI" link. Two
  2022 volumes (LNCS 13694 and 13697) write it with a space before the
  chapter suffix (`.../978-3-031-19830-4 _1`); whitespace is removed before
  matching so the chapter DOI, not the volume DOI, is kept. Some 2024 pages
  link another paper's DOI; `_DOI_OVERRIDES` in `scrapers/eccv.py` holds the
  corrections verified on CrossRef by title and authors (`4746`, `12638`).
  `8774`'s link (`978-3-031-72890-7_21`) does not resolve, so it has no DOI.
  ECCV 2018, 2020 and 2026 records carry no DOI.

- **2026 source**: Springer's proceedings are subscription-gated and ECVA's
  mirror has not yet added 2026, so metadata and freely downloadable final
  camera-ready PDFs come from the official virtual site above.
- **2026 truncated source PDFs**: some `media.eventhosts.cc` PDFs are
  truncated at the source (no trailing `%%EOF`; the served bytes are the
  whole file). `_PDF_URL_OVERRIDES` in `scrapers/eccv.py` points these at a
  version-pinned replacement verified as the camera-ready (same authors and
  abstract, ECCV/LNCS format): `7104` → arXiv `2602.23290v2` (includes the
  supplementary) and `5826` → arXiv `2607.29463v1`. As of 2026-11-03, `189`,
  `1507`, `4160`, `7569` and `12161` are also truncated at the source with no
  camera-ready replacement found (arXiv has only earlier preprints of `1507`
  and `4160`, and no copy of the others); they remain flagged as invalid PDFs
  until ECVA publishes 2026 or a verified copy appears.
