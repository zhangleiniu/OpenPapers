# UAI (Conference on Uncertainty in Artificial Intelligence)

## Sources

| Years | Source | Format |
|-------|--------|--------|
| 2015–2018 | `https://www.auai.org/uai[year]/` | Single accepted-papers page, all papers in one HTML table |
| 2019–present | `https://proceedings.mlr.press/` | One abstract page per paper |

### Legacy URLs (2015–2018)
- 2018: `https://www.auai.org/uai2018/accepted.php`
- 2017: `https://www.auai.org/uai2017/accepted.php`
- 2016: `https://www.auai.org/uai2016/proceedings.php`
- 2015: `https://www.auai.org/uai2015/acceptedPapers.shtml`

### MLR Press volumes (2019–present)
Known volume mappings (others discovered dynamically from the MLR Press main page):
- 2025: `v286`

## Coverage

Currently scraped through **2025**.

## Data fields

| Field | 2015–2018 | 2019–present |
|-------|-----------|--------------|
| `id` | Numeric ID from page (e.g. `277`) | Paper slug (e.g. `kugelgen20a`) |
| `title` | ✓ | ✓ |
| `authors` | ✓ | ✓ |
| `abstract` | ✓ | ✓ |
| `pdf_url` | ✓ | ✓ |
| `year` | ✓ | ✓ |

## Known issues

- **2015–2016**: HTML structure differs from 2017–2018 (different tag layout for title, authors, and PDF link).
- **2017–2018**: Some rows contain an award banner (`Best Student Paper`, etc.) inside a leading `<h4>` tag before the actual title — handled by skipping `<h4>` elements that wrap award text.