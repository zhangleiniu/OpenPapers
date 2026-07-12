import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from postprocessing.backfill_missing_metadata_fields import enrich_papers
from postprocessing.generate_statistics import format_years, scan
from utils import assign_bibtex


class BibtexTests(unittest.TestCase):
    def test_generation_accepts_case_normalized_conference(self):
        paper = {
            "id": "u1",
            "title": "Useful Test",
            "authors": ["Ada Lovelace"],
            "year": 2026,
            "conference": "UAI",
        }

        assign_bibtex([paper])

        self.assertTrue(paper["bibtex"].startswith("@inproceedings{"))
        self.assertIn("Conference on Uncertainty", paper["bibtex"])

    def test_collision_keys_are_deterministic(self):
        papers = [
            {"id": pid, "title": "A Useful Test", "authors": ["Ada Lovelace"],
             "year": 2026, "conference": "acl"}
            for pid in ("b", "a")
        ]

        assign_bibtex(papers)

        by_id = {paper["id"]: paper["bibtex"].split("{", 1)[1].split(",", 1)[0]
                 for paper in papers}
        self.assertEqual(by_id, {
            "a": "lovelace2026usefula",
            "b": "lovelace2026usefulb",
        })


class EnrichmentTests(unittest.TestCase):
    def test_grobid_fills_abstract_and_authors_with_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xml_path = root / "grobid/acl/2026/p.grobid.tei.xml"
            xml_path.parent.mkdir(parents=True)
            xml_path.write_text(
                '<TEI xmlns="http://www.tei-c.org/ns/1.0">'
                '<teiHeader><fileDesc><sourceDesc><biblStruct><analytic>'
                '<author><persName><forename>Ada</forename>'
                '<surname>Lovelace</surname></persName></author>'
                '</analytic></biblStruct></sourceDesc></fileDesc>'
                '<profileDesc><abstract><p>Recovered abstract.</p></abstract>'
                '</profileDesc></teiHeader></TEI>',
                encoding="utf-8")
            paper = {
                "id": "p", "authors": [], "abstract": "",
                "pdf_path": "papers/acl/2026/p.pdf",
            }

            report = enrich_papers(
                [paper], root / "grobid", root / "nougat")

        self.assertEqual(paper["abstract"], "Recovered abstract.")
        self.assertEqual(paper["abstract_source"], "grobid")
        self.assertEqual(paper["authors"], ["Ada Lovelace"])
        self.assertEqual(paper["authors_source"], "grobid")
        self.assertEqual(report["filled"]["abstract"]["grobid"], 1)

    def test_nougat_is_used_when_grobid_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            md_path = root / "nougat/acl/2026/p.md"
            md_path.parent.mkdir(parents=True)
            md_path.write_text(
                "# Paper title\nAda Lovelace, Alan Turing\n\n"
                "###### Abstract\nFallback abstract.\n\n## 1 Introduction\nBody",
                encoding="utf-8")
            paper = {
                "id": "p", "authors": [], "abstract": "",
                "pdf_path": "papers/acl/2026/p.pdf",
            }

            enrich_papers([paper], root / "grobid", root / "nougat")

        self.assertEqual(paper["abstract"], "Fallback abstract.")
        self.assertEqual(paper["abstract_source"], "nougat")
        self.assertEqual(paper["authors_source"], "nougat")


class CompletenessTests(unittest.TestCase):
    def _complete_paper(self):
        return {
            "id": "p", "title": "Title", "authors": ["Ada Lovelace"],
            "abstract": "Abstract", "year": 2026, "conference": "acl",
            "url": "https://example.test/p", "pdf_url": "https://example.test/p.pdf",
            "pdf_path": "papers/acl/2026/p.pdf", "bibtex": "@inproceedings{x}",
        }

    def test_valid_pdf_is_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf = root / "papers/acl/2026/p.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.7\n%%EOF")
            with patch.object(main, "DATA_ROOT", root):
                issues = main.completeness_issues([self._complete_paper()])

        self.assertEqual(issues, {})

    def test_missing_metadata_and_invalid_pdf_are_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf = root / "papers/acl/2026/p.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"not a pdf")
            paper = self._complete_paper()
            paper["abstract"] = ""
            with patch.object(main, "DATA_ROOT", root):
                issues = main.completeness_issues([paper])

        self.assertEqual(issues, {"abstract": 1, "invalid_pdf": 1})


class CliTests(unittest.TestCase):
    def test_missing_year_returns_nonzero(self):
        result = subprocess.run(
            [sys.executable, "main.py", "acl"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 2)
        self.assertIn("Please specify at least one year", result.stdout)


class StatisticsTests(unittest.TestCase):
    def test_year_ranges_preserve_gaps(self):
        self.assertEqual(format_years([2013, 2015, 2016, 2018]),
                         "2013, 2015–2016, 2018")

    def test_scan_validates_real_pdf_and_quality_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            metadata = root / "metadata/acl"
            metadata.mkdir(parents=True)
            pdf = root / "papers/acl/2026/p.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.7\n" + b"x" * 1024)
            (metadata / "acl_2026.json").write_text(
                '[{"id":"p","title":"T","authors":["A B"],'
                '"abstract":"A","bibtex":"@x{p}",'
                '"pdf_path":"papers/acl/2026/p.pdf"}]', encoding="utf-8")

            row = scan(root / "metadata", root)["acl"][2026]

        self.assertEqual(row["papers"], 1)
        self.assertEqual(row["pdfs"], 1)
        self.assertEqual(row["missing_pdfs"], 0)


if __name__ == "__main__":
    unittest.main()
