# Processing Pipeline

The pipeline has independent, resumable stages:

1. Discover accepted papers using the venue-specific scraper and inclusion policy.
2. Parse source metadata and download PDFs incrementally.
3. Generate BibTeX before each metadata checkpoint.
4. Optionally enrich missing abstracts/authors from existing GROBID output,
   falling back to Nougat output.
5. Validate required metadata and real PDF files.
6. Regenerate the tracked coverage and quality report.

```bash
python main.py acl 2026
python main.py acl 2026 --enrich-missing --require-complete
python postprocessing/generate_statistics.py --write
python postprocessing/generate_statistics.py --check
```

`--enrich-missing` consumes `$SCRAPER_DATA_ROOT/grobid_output` and
`nougat_output`; it does not launch those resource-intensive processors.
`postprocessing/backfill_missing_metadata_fields.py` provides the same recovery
logic for bulk historical repair. `postprocessing/rebuild_bibtex.py` is only for
historical migration or rebuilding after generator changes.
