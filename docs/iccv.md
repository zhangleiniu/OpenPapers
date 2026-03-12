# ICCV (IEEE/CVF International Conference on Computer Vision)

## Source

All years: `https://openaccess.thecvf.com/ICCV[year]/`

Note: ICCV is held in odd years only.

## Coverage

Currently scraped through **[YEAR]**. Tested back to **[YEAR]**.

## URL structure

| Years | URL format |
|-------|-----------|
| 2021–present | `https://openaccess.thecvf.com/ICCV{year}?day=all` |
| 2019 | Split across `?day=2019-10-29`, `2019-10-30`, `2019-10-31`, `2019-11-01` |

## Data fields

| Field | Notes |
|-------|-------|
| `id` | Filename stem from paper URL (e.g. `Smith_Some_Title_ICCV_2023_paper`) |
| `title` | ✓ |
| `authors` | ✓ |
| `abstract` | ✓ |
| `pdf_url` | Derived from paper page URL by replacing `/html/` → `/papers/` and `.html` → `.pdf` |

## Known issues

- **ICCV 2017**: PDF directory name uses uppercase (`content_ICCV_2017`) while the HTML path uses lowercase (`content_iccv_2017`). Handled with a special case in `_extract_pdf_url`.
- **Pre-2013 (ICCV 2011 and earlier)**: Not hosted on `openaccess.thecvf.com`. Not implemented.
