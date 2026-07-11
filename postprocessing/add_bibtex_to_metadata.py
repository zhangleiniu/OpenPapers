"""
Add a `bibtex` field to every metadata entry, generated locally from the fields
we already have (title, authors, year, conference). No API, no extra scraping.

SAFE BY DEFAULT: running without --write performs a dry run (prints a report and
a few samples, changes nothing). Pass --write to actually add the `bibtex` field
to the metadata JSON files.
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import METADATA_DIR  # noqa: E402

# conference -> (entry_type, venue_field, venue_value, organization_or_None)
VENUE = {
    "aaai":    ("inproceedings", "booktitle", "Proceedings of the AAAI Conference on Artificial Intelligence", None),
    "acl":     ("inproceedings", "booktitle", "Proceedings of the Annual Meeting of the Association for Computational Linguistics", None),
    "emnlp":   ("inproceedings", "booktitle", "Proceedings of the Conference on Empirical Methods in Natural Language Processing", None),
    "naacl":   ("inproceedings", "booktitle", "Proceedings of the Conference of the North American Chapter of the Association for Computational Linguistics", None),
    "cvpr":    ("inproceedings", "booktitle", "Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition", None),
    "iccv":    ("inproceedings", "booktitle", "Proceedings of the IEEE/CVF International Conference on Computer Vision", None),
    "eccv":    ("inproceedings", "booktitle", "European Conference on Computer Vision", "Springer"),
    "iclr":    ("inproceedings", "booktitle", "International Conference on Learning Representations", None),
    "icml":    ("inproceedings", "booktitle", "International Conference on Machine Learning", "PMLR"),
    "aistats": ("inproceedings", "booktitle", "International Conference on Artificial Intelligence and Statistics", "PMLR"),
    "colt":    ("inproceedings", "booktitle", "Conference on Learning Theory", "PMLR"),
    "uai":     ("inproceedings", "booktitle", "Conference on Uncertainty in Artificial Intelligence", "PMLR"),
    "ijcai":   ("inproceedings", "booktitle", "International Joint Conference on Artificial Intelligence", None),
    "neurips": ("inproceedings", "booktitle", "Advances in Neural Information Processing Systems", None),
    "jmlr":    ("article", "journal", "Journal of Machine Learning Research", None),
}

# First-title-word stopwords skipped when building the cite key.
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "on", "in", "of", "to",
    "for", "and", "or", "how", "why", "what", "when", "where", "with", "without",
    "from", "by", "at", "as", "into", "via", "do", "does",
}

# Lowercase surname particles merged into the surname ("Nafie El Amrani").
PARTICLES = {
    "van", "von", "de", "del", "della", "der", "di", "da", "dos", "du",
    "la", "le", "el", "den", "ten", "ter", "vande",
}

# Common accent -> LaTeX. Unmapped non-ASCII is left as UTF-8 (biber handles it).
ACCENTS = {
    "ä": '{\\"a}', "ö": '{\\"o}', "ü": '{\\"u}', "ë": '{\\"e}', "ï": '{\\"i}', "ÿ": '{\\"y}',
    "Ä": '{\\"A}', "Ö": '{\\"O}', "Ü": '{\\"U}',
    "á": "{\\'a}", "é": "{\\'e}", "í": "{\\'i}", "ó": "{\\'o}", "ú": "{\\'u}", "ý": "{\\'y}",
    "Á": "{\\'A}", "É": "{\\'E}", "ç": "{\\c c}", "Ç": "{\\c C}",
    "à": "{\\`a}", "è": "{\\`e}", "ì": "{\\`i}", "ò": "{\\`o}", "ù": "{\\`u}",
    "â": "{\\^a}", "ê": "{\\^e}", "î": "{\\^i}", "ô": "{\\^o}", "û": "{\\^u}",
    "ñ": "{\\~n}", "ã": "{\\~a}", "õ": "{\\~o}",
    "ß": "{\\ss}", "ø": "{\\o}", "Ø": "{\\O}", "å": "{\\aa}", "Å": "{\\AA}",
    "ł": "{\\l}", "Ł": "{\\L}", "č": "{\\v c}", "š": "{\\v s}", "ž": "{\\v z}",
    "ś": "{\\'s}", "ń": "{\\'n}", "ć": "{\\'c}", "ą": "{\\k a}", "ę": "{\\k e}",
}

# LaTeX special characters that must be escaped inside field values.
SPECIAL = {
    "&": "\\&", "%": "\\%", "$": "\\$", "#": "\\#", "_": "\\_",
    "{": "\\{", "}": "\\}",
    "~": "\\textasciitilde{}", "^": "\\textasciicircum{}", "\\": "\\textbackslash{}",
}


def ascii_fold(s):
    """Drop accents for use in cite keys (Rügamer -> Rugamer, Weiß -> Weiss)."""
    s = (s.replace("ß", "ss").replace("ø", "o").replace("Ø", "O")
           .replace("ł", "l").replace("Ł", "L").replace("đ", "d").replace("Đ", "D"))
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def latex_escape(text):
    """Escape LaTeX specials and map common accents, per source character."""
    out = []
    for ch in text:
        if ch in ACCENTS:
            out.append(ACCENTS[ch])
        elif ch in SPECIAL:
            out.append(SPECIAL[ch])
        else:
            out.append(ch)
    return "".join(out)


def split_name(fullname):
    """('Nafie El Amrani') -> ('Nafie', 'El Amrani'); mononym -> ('', name)."""
    toks = fullname.split()
    if not toks:
        return "", ""
    if len(toks) == 1:
        return "", toks[0]
    surname = [toks[-1]]
    i = len(toks) - 2
    while i >= 1 and toks[i].lower() in PARTICLES:   # keep >=1 given token
        surname.insert(0, toks[i])
        i -= 1
    given = toks[:i + 1]
    return " ".join(given).strip(), " ".join(surname).strip()


def first_title_keyword(title):
    """First non-stopword title word, leading alphanumeric run, ASCII lowercased."""
    for word in title.strip().split():
        wl = ascii_fold(word).lower()
        m = re.match(r"[a-z0-9]+", wl)
        if not m:
            continue
        token = m.group(0)
        if token in STOPWORDS:
            continue
        return token
    return ""


def base_cite_key(conf, year, title, authors):
    given, surname = split_name(authors[0])
    last_token = surname.split()[-1] if surname else ""
    sk = re.sub(r"[^a-z0-9]", "", ascii_fold(last_token).lower())
    kw = first_title_keyword(title)
    return f"{sk}{year}{kw}"


def format_authors(authors):
    parts = []
    for a in authors:
        given, surname = split_name(a)
        if given:
            parts.append(f"{latex_escape(surname)}, {latex_escape(given)}")
        else:
            parts.append(latex_escape(surname))
    return " and ".join(parts)


def suffix(i):
    """0->a, 1->b, ... 25->z, 26->aa, ... (deterministic collision suffix)."""
    s, i = "", i + 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(97 + r) + s
    return s


def build_bibtex(conf, year, title, authors, key):
    etype, vfield, vvalue, org = VENUE[conf]
    lines = [f"@{etype}{{{key},",
             f"  title={{{latex_escape(title.strip())}}},",
             f"  author={{{format_authors(authors)}}},",
             f"  {vfield}={{{vvalue}}},",
             f"  year={{{year}}}"]
    if org:
        lines[-1] += ","
        lines.append(f"  organization={{{org}}}")
    lines.append("}")
    return "\n".join(lines)


def main(write, metadata_root):
    # Pass 1: load everything, validate, compute base keys -------------------
    file_papers = {}                  # path -> papers list (kept for writing)
    records = []                      # dicts describing each eligible entry
    skipped = defaultdict(list)       # reason -> [ "conf/year id" ]
    per_conf = Counter()

    for path in sorted(metadata_root.glob("*/*.json")):
        if path.name.endswith(".bak"):
            continue
        conf = path.parent.name
        year = path.stem.rsplit("_", 1)[-1]
        try:
            papers = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"[load error] {path}: {e}")
            continue
        file_papers[path] = papers

        for paper in papers:
            pid = paper.get("id", "<no-id>")
            where = f"{conf}/{year} {pid}"

            if conf not in VENUE:
                skipped["unknown conference"].append(where); continue
            title = paper.get("title")
            authors = paper.get("authors")
            yr = paper.get("year", year)
            if not isinstance(title, str) or not title.strip():
                skipped["no title"].append(where); continue
            if not isinstance(authors, list) or not authors or not all(
                    isinstance(a, str) and a.strip() for a in authors):
                skipped["no/!list authors"].append(where); continue
            if not (isinstance(yr, int) or (isinstance(yr, str) and yr.isdigit())):
                skipped["bad year"].append(where); continue

            bk = base_cite_key(conf, int(yr), title, authors)
            records.append({
                "paper": paper, "conf": conf, "year": int(yr),
                "title": title, "authors": authors, "id": str(pid),
                "base_key": bk,
                "sort": (conf, int(yr), str(pid), title),
            })
            per_conf[conf] += 1

    # Pass 2: resolve collisions deterministically ---------------------------
    groups = defaultdict(list)
    for rec in records:
        groups[rec["base_key"]].append(rec)

    collisions = 0
    for bk, recs in groups.items():
        if len(recs) == 1:
            recs[0]["key"] = bk
        else:
            collisions += 1
            for i, rec in enumerate(sorted(recs, key=lambda r: r["sort"])):
                rec["key"] = bk + suffix(i)

    # Pass 3: build bibtex; attach if writing --------------------------------
    for rec in records:
        bib = build_bibtex(rec["conf"], rec["year"], rec["title"],
                            rec["authors"], rec["key"])
        rec["bibtex"] = bib
        if write:
            rec["paper"]["bibtex"] = bib

    if write:
        for path, papers in file_papers.items():
            if any(("bibtex" in p) for p in papers):
                json.dump(papers, open(path, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)

    # Report -----------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print(f"Mode                     : {'WRITE' if write else 'DRY RUN (no files changed)'}")
    print(f"Entries with bibtex built: {len(records)}")
    print(f"Base-key collisions fixed: {collisions} (suffixed a/b/c…)")
    print(f"Skipped                  : {sum(len(v) for v in skipped.values())}")
    for reason, items in skipped.items():
        print(f"    {reason}: {len(items)}")
    print(f"{'=' * 60}")
    print("Per conference:")
    for conf in sorted(per_conf):
        print(f"  {conf:<9} {per_conf[conf]}")

    # one sample per conference
    print(f"\n{'=' * 60}\nSAMPLES (one per conference)\n{'=' * 60}")
    shown = set()
    for rec in records:
        if rec["conf"] in shown:
            continue
        shown.add(rec["conf"])
        print(f"\n# {rec['conf']} {rec['year']}")
        print(rec["bibtex"])

    if not write:
        print(f"\n{'=' * 60}\nThis was a DRY RUN. Re-run with --write to add the "
              f"`bibtex` field to the metadata.\n{'=' * 60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a `bibtex` field for each metadata entry from "
                    "existing fields (no API). Dry run unless --write is given."
    )
    parser.add_argument("--write", action="store_true",
                        help="Actually write the `bibtex` field into the metadata "
                             "JSONs. Without this flag, performs a dry run.")
    parser.add_argument("--metadata-root", type=Path, default=METADATA_DIR,
                        help=f"Metadata directory (default: {METADATA_DIR})")
    args = parser.parse_args()
    main(write=args.write, metadata_root=args.metadata_root)