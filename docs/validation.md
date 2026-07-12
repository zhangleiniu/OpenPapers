# Validation

Use strict validation after a scrape:

```bash
python main.py acl 2026 --require-complete
```

It checks required fields, PDF paths, physical files, and PDF signatures. A
failure exits with status 2.

For provisional sources, select the readiness target explicitly:

```bash
python main.py ijcai 2026 --no-pdfs --require-complete \
  --completeness-level metadata
```

- `announced`: stable ID, title, authors, year, venue, URL, and BibTeX.
- `metadata`: announced fields plus abstract.
- `archival`: metadata plus PDF URL and, unless `--no-pdfs` is used, a valid
  local PDF. Explicitly provisional records fail this level.

Run automated tests with:

```bash
python -m unittest discover -s tests -v
```

For the independent conference-year audit required before committing data,
also verify the expected count, duplicate IDs, minimum file size, and PDF
signature with:

```bash
python postprocessing/validate_year.py icml 2026 \
  --level metadata --require-pdfs --expected-count 6341
```

`statistics.md` is generated and tracked. It reports actual (including gapped)
year coverage, valid physical PDFs, missing/invalid PDFs, missing metadata, and
duplicate IDs. Never edit it manually:

```bash
python postprocessing/generate_statistics.py --write
python postprocessing/generate_statistics.py --check
```
