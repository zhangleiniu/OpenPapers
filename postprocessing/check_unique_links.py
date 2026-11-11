"""Report `url` values and DOIs shared by more than one paper.

Each paper should have its own landing page and its own DOI. A value shared by
several papers usually means the scraper stored a listing page or a
proceedings-volume DOI instead of the per-paper one, so downstream consumers
that link or deduplicate by URL/DOI get the wrong paper.

The DOI is read from the `doi` field of `bibtex` and from `bibtex_extra.doi`.
Duplicates are counted across every venue and year, not only within one file.
Also reports Springer book-level DOIs (`10.1007/978-...` without the
`_<chapter>` suffix), which are wrong for a single paper even when unique.

Exits 1 when any problem is found, 0 otherwise. Read-only.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import METADATA_DIR  # noqa: E402

_BIBTEX_DOI_RE = re.compile(r'\bdoi\s*=\s*[{"]\s*([^}"]+?)\s*[}"]', re.I)
_SPRINGER_BOOK_DOI_RE = re.compile(r'^10\.1007/978-[\d-]+$')

PaperRef = Tuple[str, str]  # (metadata file name, paper id)


def paper_dois(paper: Dict) -> Set[str]:
    """Return the lowercased DOIs a paper carries in bibtex/bibtex_extra."""
    dois = set(_BIBTEX_DOI_RE.findall(paper.get('bibtex') or ''))
    extra = paper.get('bibtex_extra') or {}
    if isinstance(extra, dict) and extra.get('doi'):
        dois.add(extra['doi'])
    return {d.strip().lower() for d in dois if d.strip()}


def iter_metadata_files(metadata_root: Path) -> Iterator[Path]:
    for path in sorted(metadata_root.glob('*/*.json')):
        yield path


def find_problems(metadata_root: Path) -> Dict[str, Dict[str, List[PaperRef]]]:
    by_url: Dict[str, List[PaperRef]] = defaultdict(list)
    by_doi: Dict[str, List[PaperRef]] = defaultdict(list)
    for path in iter_metadata_files(metadata_root):
        papers = json.loads(path.read_text(encoding='utf-8'))
        for paper in papers:
            ref = (path.name, str(paper.get('id', '')))
            url = (paper.get('url') or '').strip()
            if url:
                by_url[url].append(ref)
            for doi in paper_dois(paper):
                by_doi[doi].append(ref)
    return {
        'shared url': {k: v for k, v in by_url.items() if len(v) > 1},
        'shared doi': {k: v for k, v in by_doi.items() if len(v) > 1},
        'springer book doi': {k: v for k, v in by_doi.items()
                              if _SPRINGER_BOOK_DOI_RE.match(k)},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--metadata-root', type=Path, default=METADATA_DIR)
    parser.add_argument('--show', type=int, default=5,
                        help='papers listed per offending value (default 5)')
    args = parser.parse_args(argv)

    problems = find_problems(args.metadata_root)
    total = 0
    for kind, groups in problems.items():
        papers = sum(len(refs) for refs in groups.values())
        total += len(groups)
        print(f'{kind}: {len(groups)} value(s), {papers} paper(s)')
        for value, refs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            shown = ', '.join(f'{f}:{i}' for f, i in refs[:args.show])
            more = f' (+{len(refs) - args.show} more)' if len(refs) > args.show else ''
            print(f'  {len(refs):>4}  {value}\n        {shown}{more}')
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main())
