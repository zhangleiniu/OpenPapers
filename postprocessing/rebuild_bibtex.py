"""Rebuild BibTeX fields in existing metadata files.

New scrapes already generate BibTeX in ``BaseScraper``.  This command remains
for historical datasets and for rebuilding entries after the shared generator
changes.  The implementation intentionally delegates to ``utils`` so online
scraping and offline migration cannot drift apart.

Running without ``--write`` is a dry run.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import METADATA_DIR  # noqa: E402
from utils import assign_bibtex  # noqa: E402


def iter_metadata_files(metadata_root):
    for path in sorted(metadata_root.glob("*/*.json")):
        if not path.name.endswith(".bak"):
            yield path


def main(write, metadata_root):
    loaded = {}
    load_errors = []
    entries_total = changed = missing = 0
    per_conf = Counter()

    for path in iter_metadata_files(metadata_root):
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                raise ValueError("top-level JSON value is not a list")
        except Exception as exc:
            load_errors.append((path, str(exc)))
            continue
        loaded[path] = entries
        before = [paper.get("bibtex") for paper in entries]
        assign_bibtex(entries)
        entries_total += len(entries)
        changed += sum(old != paper.get("bibtex")
                       for old, paper in zip(before, entries))
        missing += sum(not paper.get("bibtex") for paper in entries)
        per_conf.update(
            paper.get("conference", "<missing>") for paper in entries)

    if write:
        for path, entries in loaded.items():
            path.write_text(
                json.dumps(entries, ensure_ascii=False, indent=2),
                encoding="utf-8")

    print(f"Mode: {'WRITE' if write else 'DRY RUN'}")
    print(f"Metadata files: {len(loaded)}")
    print(f"Entries: {entries_total}")
    print(f"BibTeX changed/added: {changed}")
    print(f"Entries still without BibTeX: {missing}")
    print(f"Load errors: {len(load_errors)}")
    for conf, count in sorted(per_conf.items()):
        print(f"  {conf}: {count}")
    for path, error in load_errors:
        print(f"  [load error] {path}: {error}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rebuild BibTeX in existing metadata using the same "
                    "generator as the scraper. Dry run unless --write is used.")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--metadata-root", type=Path, default=METADATA_DIR)
    args = parser.parse_args()
    main(write=args.write, metadata_root=args.metadata_root)
