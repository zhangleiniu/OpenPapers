# Processing Pipeline

The pipeline has independent, resumable stages:

1. Discover accepted papers using the best currently available authoritative source.
2. Mark early OpenReview/accepted-list results provisional; use formal
   proceedings as the archival source when available.
3. Parse source metadata and download available PDFs incrementally.
4. Reconcile cross-source IDs without duplicating papers.
5. Generate BibTeX before each metadata checkpoint.
6. Optionally enrich missing abstracts/authors from existing GROBID output,
   falling back to Nougat output.
7. Validate against the announced, metadata, or archival readiness target.
8. Regenerate the tracked coverage report and README coverage list.
9. Update the venue page only when the new year adds a source/volume mapping,
   changes scraping policy, or has known missing/withdrawn-paper exceptions.

```bash
python main.py acl 2026
python main.py acl 2026 --enrich-missing --require-complete
python main.py aistats 2026 --no-pdfs --require-complete --completeness-level metadata
python postprocessing/generate_statistics.py --write
python postprocessing/generate_statistics.py --check
```

The `--check` command fails if either `statistics.md` or the generated README
coverage block is stale.

`--enrich-missing` consumes `$SCRAPER_DATA_ROOT/grobid_output` and
`nougat_output`; it does not launch those resource-intensive processors.
`postprocessing/backfill_missing_metadata_fields.py` provides the same recovery
logic for bulk historical repair. `postprocessing/rebuild_bibtex.py` is only for
historical migration or rebuilding after generator changes.
