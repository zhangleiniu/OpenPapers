"""Backfill missing abstract/authors in metadata from GROBID (primary) or
Nougat (fallback) output.

How it works:

1. It iterates every metadata JSON under the data root but processes a paper
   only if it has a pdf_path; everything else is skipped.

2. For a missing abstract it derives the GROBID path from pdf_path
   (data/papers/…/X.pdf → <grobid-root>/…/X.grobid.tei.xml) and parses the
   TEI profileDesc/abstract.

3. If the GROBID file isn't there (or yields nothing), it falls back to the
   Nougat path (<nougat-root>/…/X.md).

4. Four extractors: GROBID abstract (TEI <abstract>), GROBID authors
   (author/persName inside teiHeader only, so references are excluded),
   Nougat abstract (###### Abstract until the next # heading), Nougat authors
   (the line between the title and Abstract, footnote markers stripped).

5. --abstract fills only abstracts and writes abstract_source = grobid/nougat.

6. --authors fills only authors and writes authors_source (a separate field).
   The two flags are independent; pass one, the other, or both.

GROBID/Nougat processing itself lives in a separate pipeline (it needs GPU);
this script only consumes their output trees, which mirror the papers/ layout.

Usage:
    python postprocessing/backfill_missing_metadata_fields.py --abstract --authors \
        [--grobid-root PATH] [--nougat-root PATH]

Paths default to <SCRAPER_DATA_ROOT>/{metadata,grobid_output,nougat_output}.
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import DATA_ROOT, METADATA_DIR  # noqa: E402

# Current convention is "papers/..."; "data/papers/..." appears in metadata
# written before 2026-07. Longer prefix must be tried first.
PAPERS_PREFIXES = ("data/papers/", "papers/")

# Provenance fields written when a value is backfilled (distinct per field type).
ABSTRACT_SOURCE_FIELD = "abstract_source"
AUTHORS_SOURCE_FIELD = "authors_source"

# TEI namespace used by GROBID output.
TEI = "{http://www.tei-c.org/ns/1.0}"


def is_empty(value):
    """True if a field should count as missing (None / blank string / empty list)."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


# ----------------------------------------------------------------------
# pdf_path -> processed-file locations
# ----------------------------------------------------------------------
def stem_from_pdf_path(pdf_path):
    """papers/conf/year/file.pdf  ->  conf/year/file   (None if unparseable)."""
    for prefix in PAPERS_PREFIXES:
        idx = pdf_path.find(prefix)
        if idx != -1:
            sub = pdf_path[idx + len(prefix):]
            if sub.endswith(".pdf"):
                sub = sub[:-4]
            return sub
    return None


# ----------------------------------------------------------------------
# GROBID extractors (TEI XML)
# ----------------------------------------------------------------------
def extract_abstract_grobid(xml_path):
    try:
        root = ET.parse(xml_path).getroot()
    except Exception:
        return ""
    abs_el = root.find(f".//{TEI}profileDesc/{TEI}abstract")
    if abs_el is None:
        abs_el = root.find(f".//{TEI}abstract")
    if abs_el is None:
        return ""
    text = " ".join(t for t in abs_el.itertext())
    return re.sub(r"\s+", " ", text).strip()


def extract_authors_grobid(xml_path):
    try:
        root = ET.parse(xml_path).getroot()
    except Exception:
        return []
    # Only the document's own authors live in <teiHeader>; references are under <text>.
    header = root.find(f"{TEI}teiHeader")
    if header is None:
        return []
    authors = []
    for pers in header.findall(f".//{TEI}author/{TEI}persName"):
        forenames = [f.text.strip() for f in pers.findall(f"{TEI}forename")
                     if f.text and f.text.strip()]
        surname_el = pers.find(f"{TEI}surname")
        surname = surname_el.text.strip() if (surname_el is not None and surname_el.text) else ""
        name = " ".join(forenames + ([surname] if surname else [])).strip()
        if name:
            authors.append(name)
    # de-duplicate, preserve order
    seen, out = set(), []
    for a in authors:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


# ----------------------------------------------------------------------
# Nougat extractors (markdown)
# ----------------------------------------------------------------------
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_AFFIL_RE = re.compile(
    r"(@|department|universit|institut|laborator|school|college|\binc\b|\bcorp\b|"
    r"google|microsoft|meta|deepmind|footnote|affiliation|\bcorrespond)",
    re.I,
)


def _read_text(path):
    return open(path, encoding="utf-8", errors="ignore").read()


def _is_heading(line):
    return bool(_HEADING_RE.match(line))


def _heading_text(line):
    """Normalised heading text: '###### Abstract' -> 'abstract'."""
    return line.strip().lstrip("#").strip().strip("*").strip().lower()


def extract_abstract_nougat(md_path):
    lines = _read_text(md_path).splitlines()
    # locate the Abstract heading
    start = None
    for i, line in enumerate(lines):
        if _heading_text(line) == "abstract":
            start = i + 1
            break
    if start is None:
        return ""
    # collect until the next section heading (e.g. "## 1 Introduction")
    out = []
    for line in lines[start:]:
        if _is_heading(line):
            break
        if line.strip():
            out.append(line.strip())
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def extract_authors_nougat(md_path):
    """Best-effort: the author line sits between the title heading and the
    Abstract heading, before affiliation/email lines. Heuristic — review
    nougat-sourced authors."""
    lines = _read_text(md_path).splitlines()

    title_idx = next((i for i, l in enumerate(lines) if _is_heading(l)), None)
    abs_idx = next((i for i, l in enumerate(lines) if _heading_text(l) == "abstract"), None)

    start = (title_idx + 1) if title_idx is not None else 0
    end = abs_idx if abs_idx is not None else len(lines)

    block = []
    for line in lines[start:end]:
        s = line.strip()
        if not s or _is_heading(line):
            continue
        if _AFFIL_RE.search(s):   # affiliations/emails start here -> stop
            break
        block.append(s)
    if not block:
        return []

    joined = " , ".join(block)
    # strip footnote markers (superscript digits / symbols)
    joined = re.sub(r"[0-9\*†‡§¶]", "", joined)
    parts = re.split(r"[,;]|\band\b|&", joined)

    authors = []
    for p in parts:
        name = re.sub(r"\s+", " ", p).strip(" .")
        toks = name.split()
        if 1 < len(toks) <= 5 and "@" not in name and re.search(r"[^\W\d_]", name):
            authors.append(name)
    seen, out = set(), []
    for a in authors:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


# ----------------------------------------------------------------------
# Backfill one field: GROBID first, Nougat fallback
# ----------------------------------------------------------------------
def backfill_value(stem, grobid_root, nougat_root, grobid_fn, nougat_fn):
    """Return (value, source, reason). source in {grobid, nougat} or None."""
    gp = grobid_root / (stem + ".grobid.tei.xml")
    np_ = nougat_root / (stem + ".md")

    if gp.is_file():
        val = grobid_fn(gp)
        if val:
            return val, "grobid", None

    if np_.is_file():
        val = nougat_fn(np_)
        if val:
            return val, "nougat", None

    if not gp.is_file() and not np_.is_file():
        return None, None, "no grobid or nougat output"
    return None, None, "processed file present but nothing extracted"


def iter_metadata_files(metadata_root):
    """Yield every conference-year metadata JSON under the metadata root."""
    for path in sorted(metadata_root.glob("*/*.json")):
        if path.name.endswith(".bak"):
            continue
        yield path


def main(do_abstract, do_authors, metadata_root, grobid_root, nougat_root):
    total_with_pdf = 0
    missing = Counter()        # field -> count missing (among pdf_path entries)
    filled = defaultdict(Counter)   # field -> Counter(source)
    per_file_filled = defaultdict(Counter)  # "conf/year" -> Counter("field:source")
    unfilled = defaultdict(list)    # "conf/year" -> [(field, id, reason, title)]

    for metadata_file in iter_metadata_files(metadata_root):
        key = f"{metadata_file.parent.name}/{metadata_file.stem.rsplit('_', 1)[-1]}"
        try:
            papers = json.load(open(metadata_file, encoding="utf-8"))
        except Exception as e:
            print(f"[load error] {metadata_file}: {e}")
            continue

        changed = False
        for paper in papers:
            pdf_path = paper.get("pdf_path", "")
            if not pdf_path:           # only entries that downloaded successfully
                continue
            total_with_pdf += 1
            stem = stem_from_pdf_path(pdf_path)
            if stem is None:
                continue

            pid = paper.get("id", "<no-id>")
            title = (paper.get("title") or "")[:50]

            # --- abstract ---
            if do_abstract and is_empty(paper.get("abstract")):
                missing["abstract"] += 1
                val, src, reason = backfill_value(
                    stem, grobid_root, nougat_root,
                    extract_abstract_grobid, extract_abstract_nougat)
                if val:
                    paper["abstract"] = val
                    paper[ABSTRACT_SOURCE_FIELD] = src
                    changed = True
                    filled["abstract"][src] += 1
                    per_file_filled[key][f"abstract:{src}"] += 1
                else:
                    unfilled[key].append(("abstract", pid, reason, title))

            # --- authors ---
            if do_authors and is_empty(paper.get("authors")):
                missing["authors"] += 1
                val, src, reason = backfill_value(
                    stem, grobid_root, nougat_root,
                    extract_authors_grobid, extract_authors_nougat)
                if val:
                    paper["authors"] = val
                    paper[AUTHORS_SOURCE_FIELD] = src
                    changed = True
                    filled["authors"][src] += 1
                    per_file_filled[key][f"authors:{src}"] += 1
                else:
                    unfilled[key].append(("authors", pid, reason, title))

        if changed:
            json.dump(papers, open(metadata_file, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    output_file = Path("backfill_report.txt")
    lines = []

    def log(msg=""):
        print(msg)
        lines.append(msg)

    targets = []
    if do_abstract:
        targets.append("abstract")
    if do_authors:
        targets.append("authors")

    log(f"\n{'=' * 60}")
    log(f"Backfill targets                  : {', '.join(targets)}")
    log(f"Entries with pdf_path scanned     : {total_with_pdf}")
    for field in targets:
        f_filled = sum(filled[field].values())
        log(f"  {field}: missing {missing[field]} | "
            f"filled {f_filled} (grobid={filled[field]['grobid']}, "
            f"nougat={filled[field]['nougat']}) | "
            f"unfilled {missing[field] - f_filled}")
    log(f"{'=' * 60}")

    log("\nPER CONFERENCE / YEAR (filled)")
    log(f"{'=' * 60}")
    if per_file_filled:
        for key in sorted(per_file_filled):
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(per_file_filled[key].items()))
            log(f"  [{key}] — {breakdown}")
    else:
        log("  (nothing filled)")

    log(f"\n{'=' * 60}")
    log("UNFILLED (still missing after backfill)")
    log(f"{'=' * 60}")
    if any(unfilled.values()):
        for key in sorted(unfilled):
            if not unfilled[key]:
                continue
            log(f"\n  [{key}] — {len(unfilled[key])}")
            for field, pid, reason, title in unfilled[key]:
                log(f"    {field:<9} {pid:<12} | {reason:<42} | {title}")
    else:
        log("  (none)")

    open(output_file, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"\nReport written to: {output_file.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill missing abstract/authors in metadata from GROBID "
                    "(primary) or Nougat (fallback). Only entries with a pdf_path "
                    "are touched, and only the field(s) you request."
    )
    parser.add_argument("--abstract", action="store_true",
                        help="Fill only missing abstracts.")
    parser.add_argument("--authors", action="store_true",
                        help="Fill only missing authors.")
    parser.add_argument("--metadata-root", type=Path, default=METADATA_DIR,
                        help=f"Metadata directory (default: {METADATA_DIR})")
    parser.add_argument("--grobid-root", type=Path,
                        default=DATA_ROOT / "grobid_output",
                        help="GROBID TEI output tree mirroring papers/ layout "
                             f"(default: {DATA_ROOT / 'grobid_output'})")
    parser.add_argument("--nougat-root", type=Path,
                        default=DATA_ROOT / "nougat_output",
                        help="Nougat markdown output tree mirroring papers/ layout "
                             f"(default: {DATA_ROOT / 'nougat_output'})")
    args = parser.parse_args()

    if not (args.abstract or args.authors):
        parser.error("pass at least one of --abstract / --authors")

    main(do_abstract=args.abstract, do_authors=args.authors,
         metadata_root=args.metadata_root,
         grobid_root=args.grobid_root,
         nougat_root=args.nougat_root)
