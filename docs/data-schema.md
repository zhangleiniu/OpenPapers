# Data Schema

Every conference-year JSON file contains a list of paper objects. The base
pipeline adds these common fields:

| Field | Meaning |
|---|---|
| `id` | Venue-specific stable identifier |
| `title` | Paper title |
| `authors` | Ordered list of author names |
| `abstract` | Source abstract or PDF-derived fallback |
| `year` | Publication year |
| `conference` | Lowercase venue key |
| `url` | Canonical metadata page |
| `pdf_url` | PDF source URL |
| `pdf_path` | Path relative to `SCRAPER_DATA_ROOT` |
| `pdf_downloaded` | Whether the downloader succeeded |
| `bibtex` | Locally generated BibTeX entry |

Optional provenance fields are `abstract_source` and `authors_source`. Values
such as `grobid` or `nougat` mean the source page lacked the field and it was
recovered from processed PDF output. Venue scrapers may add fields such as
`keywords`, `track`, `status`, `issue`, or `section`.

Early sources use these lifecycle fields:

| Field | Meaning |
|---|---|
| `metadata_source` | Adapter that supplied the current record, such as `openreview`, `official_accepted_list`, or `pmlr` |
| `source_id` | Identifier in the current source |
| `source_ids` | Known identifiers keyed by source, retained during reconciliation |
| `publication_status` | `provisional` before formal proceedings, `archival` afterwards |

The stable `id` does not change when a provisional record is reconciled with
formal proceedings. The archival identifier is added to `source_ids`, while
non-empty archival metadata takes precedence over the provisional version.

An accepted paper is retained even when no PDF can be found. PDF fallback order
is official proceedings, another authoritative author/institutional source, then
arXiv. Non-official use should be recorded in `pdf_source`; an unresolved paper
keeps `pdf_downloaded: false` and a documented venue exception.
