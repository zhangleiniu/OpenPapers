# AISTATS (International Conference on Artificial Intelligence and Statistics)

## Source

Archival source: `https://proceedings.mlr.press/`

For 2026, PMLR published the proceedings as volume `v300` on August 30, 2026.
The mapping is pinned because the volume was available before it appeared on
the PMLR main index. If a future volume has not yet been published, the scraper
can fall back to an explicitly configured official OpenReview invitation.

Known volume mappings (others discovered dynamically from the MLR Press main page):
- 2026: `v300`
- 2025: `v258`
- 1995: `r0`, 1997: `r1`, 1999: `r2`, 2001: `r3`, 2003: `r4`, 2005: `r5`
  (MLR Press "Reissue Series" — pre-dates AISTATS' regular `v`-numbered PMLR
  volumes; same page template, fixed volume IDs since dynamic discovery only
  matches `v<digits>` hrefs).

## Dataset coverage

See the generated [coverage and quality report](../statistics.md).

## Data fields

| Field | Notes |
|-------|-------|
| `id` | Paper slug (e.g. `smith25a`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ |
| `pdf_url` | ✓ |
| `bibtex_extra` | editor/volume/series/month/pages/organization parsed from PMLR's own citation block on the same page (no extra request) — see [data-schema.md](data-schema.md#bibtex-generation) |

## Known issues

- **1995–2005 (Reissue Series)**: AISTATS ran in odd years only until 2010, so
  2006 and 2008 have no proceedings (not a scraping gap). Pre-1995 editions
  (1985–1993) were only ever published as print books and have no open-access
  source. Each R-series volume includes one `Frontmatter` entry (the issue
  preface, not a paper) that the scraper currently retains as a regular
  record — harmless but worth filtering out downstream if paper counts need
  to be exact.
- **2018, `derezinski18a`**: the official PMLR PDF URL returns 404. The paper is
  retained and uses arXiv `1710.05110`, recorded with `pdf_source: arxiv`.
- **1999 (r2), `viswanathan99a`/`oates99a`/`ghosh99a`**: the official PMLR PDF
  URLs return 404 (abstract pages are fine). Metadata is retained with
  `pdf_downloaded: false`; no arXiv equivalent has been identified yet.
