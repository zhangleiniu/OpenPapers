# Validation

Use strict validation after a scrape:

```bash
python main.py acl 2026 --require-complete
```

It checks required fields, PDF paths, physical files, and PDF signatures. A
failure exits with status 2. Run automated tests with:

```bash
python -m unittest discover -s tests -v
```

`statistics.md` is generated and tracked. It reports actual (including gapped)
year coverage, valid physical PDFs, missing/invalid PDFs, missing metadata, and
duplicate IDs. Never edit it manually:

```bash
python postprocessing/generate_statistics.py --write
python postprocessing/generate_statistics.py --check
```
