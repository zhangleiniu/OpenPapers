# COLT (Conference on Learning Theory)

## Source

All years: `https://proceedings.mlr.press/`

Known volume mappings (others discovered dynamically from the MLR Press main page):
- 2026: `v336`
- 2025: `v291`

## Dataset coverage

See the generated [coverage and quality report](../statistics.md).

## Data fields

| Field | Notes |
|-------|-------|
| `id` | Paper slug (e.g. `doe25a`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ |
| `pdf_url` | ✓ |

## Known issues

- The first `<div class="paper">` on the volume page is a conference overview/preface entry, not an actual paper — skipped during URL extraction.
- Volume discovery uses a loose regex (COLT + year anywhere in text) because titles vary significantly, e.g. "Proceedings of Thirty-Eighth Conference on Learning Theory".
- **Pre-2011**: Proceedings are not hosted on `proceedings.mlr.press`. Not currently implemented.
- **2013, `Telgarsky13`**: the official PMLR PDF URL returns 404. The paper is
  retained and uses arXiv `1305.2648`, recorded with `pdf_source: arxiv`.
