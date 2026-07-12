# AISTATS (International Conference on Artificial Intelligence and Statistics)

## Source

All years: `https://proceedings.mlr.press/`

Known volume mappings (others discovered dynamically from the MLR Press main page):
- 2025: `v258`

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

## Known issues

- **Pre-2009**: Coverage is incomplete. 2007 exists on MLR Press but has not been tested. Older editions (1995, 1997, 1999, 2001, 2003, 2005) are available as part of the MLR Press "Reissue Series" under `R`-prefixed volumes (`R0`–`R5`), which are not compatible with the current volume discovery logic and have not been implemented.
- **2018, `derezinski18a`**: the official PMLR PDF URL returns 404. The paper is
  retained and uses arXiv `1710.05110`, recorded with `pdf_source: arxiv`.
