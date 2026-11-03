# Validation

Use strict validation after a scrape:

```bash
python main.py acl 2026 --require-complete
```

It checks required fields, PDF paths, physical files, and PDF integrity. A
failure exits with status 2.

A PDF is valid only if it starts with `%PDF-` and has a `%%EOF` marker in its
last 1,024 bytes; a file without the trailing marker is truncated (an earlier
incremental-update `%%EOF` does not count). The same check
(`utils.pdf_file_problem`) is applied when a download completes: an invalid
download is discarded and never replaces the existing file, so a source that
serves a truncated PDF leaves the paper incomplete instead of landing a broken
file. `validate_year.py` reports these as `truncated_pdf`.

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
signature and trailing `%%EOF` with:

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
