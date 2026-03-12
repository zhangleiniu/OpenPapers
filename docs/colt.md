# COLT (Conference on Learning Theory)

## Source

All years: `https://proceedings.mlr.press/`

Known volume mappings (others discovered dynamically from the MLR Press main page):
- 2025: `v291`

## Coverage

2011-2025

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
- **2013, `Telgarsky13`** ("Boosting with the Logistic Loss is Consistent"): PDF link on MLR Press is broken. The paper is available on arXiv but no fallback has been implemented; the record is scraped with metadata only and no local PDF.