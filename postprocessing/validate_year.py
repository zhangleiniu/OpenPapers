"""Independently validate one conference-year metadata/PDF snapshot."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import DATA_ROOT, METADATA_DIR  # noqa: E402


def validate(papers, data_root: Path, level="archival", require_pdfs=None,
             expected_count=None, minimum_pdf_size=1024):
    if level not in {"announced", "metadata", "archival"}:
        raise ValueError(f"unknown completeness level: {level}")
    if require_pdfs is None:
        require_pdfs = level == "archival"
    required = ["id", "title", "authors", "year", "conference", "url", "bibtex"]
    if level in {"metadata", "archival"}:
        required.append("abstract")
    if level == "archival":
        required.append("pdf_url")

    issues = Counter()
    if expected_count is not None and len(papers) != expected_count:
        issues["paper_count"] = abs(len(papers) - expected_count)
    ids = Counter(paper.get("id") for paper in papers if paper.get("id"))
    issues["duplicate_ids"] = sum(
        count - 1 for count in ids.values() if count > 1)
    for paper in papers:
        for field in required:
            if not paper.get(field):
                issues[f"missing_{field}"] += 1
        if level == "archival" and paper.get("publication_status") == "provisional":
            issues["provisional"] += 1
        if not require_pdfs:
            continue
        pdf_path = paper.get("pdf_path")
        if not pdf_path:
            issues["missing_pdf_path"] += 1
            continue
        relative = pdf_path[5:] if pdf_path.startswith("data/") else pdf_path
        path = data_root / relative
        if not path.is_file():
            issues["missing_pdf_file"] += 1
            continue
        try:
            if path.stat().st_size < minimum_pdf_size:
                issues["undersized_pdf"] += 1
                continue
            with path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    issues["invalid_pdf_signature"] += 1
        except OSError:
            issues["unreadable_pdf"] += 1
    return {key: value for key, value in sorted(issues.items()) if value}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("conference")
    parser.add_argument("year", type=int)
    parser.add_argument("--metadata-root", type=Path, default=METADATA_DIR)
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    parser.add_argument("--level", choices=("announced", "metadata", "archival"),
                        default="archival")
    parser.add_argument("--require-pdfs", action="store_true")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--minimum-pdf-size", type=int, default=1024)
    args = parser.parse_args(argv)
    path = (args.metadata_root / args.conference.lower() /
            f"{args.conference.lower()}_{args.year}.json")
    try:
        papers = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(papers, list):
            raise ValueError("top-level metadata must be a list")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot read {path}: {exc}", file=sys.stderr)
        return 2
    issues = validate(
        papers, args.data_root, args.level,
        require_pdfs=(args.require_pdfs or args.level == "archival"),
        expected_count=args.expected_count,
        minimum_pdf_size=args.minimum_pdf_size,
    )
    result = {
        "conference": args.conference.lower(), "year": args.year,
        "papers": len(papers), "level": args.level, "issues": issues,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
