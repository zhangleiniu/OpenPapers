import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from automation.monitor import StateStore, load_registry, save_snapshot
from scrapers.base import BaseScraper
from scrapers.ijcai import IJCAIScraper
from scrapers.openreview import OpenReviewClient
from postprocessing.backfill_missing_metadata_fields import enrich_papers
from postprocessing.generate_statistics import (
    format_years, render_readme_coverage, replace_generated_section, scan,
)
from postprocessing.validate_year import validate
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

    def test_metadata_level_accepts_provisional_record_without_pdf(self):
        paper = self._complete_paper()
        paper.update(
            publication_status="provisional", pdf_url="", pdf_path="")

        issues = main.completeness_issues(
            [paper], require_pdfs=True, level="metadata")

        self.assertEqual(issues, {})
        self.assertEqual(
            main.completeness_issues(
                [paper], require_pdfs=True, level="archival"),
            {"pdf_url": 1, "provisional": 1, "pdf_path": 1})


class SourceLifecycleTests(unittest.TestCase):
    def test_openreview_client_requests_json(self):
        session = Mock()
        response = Mock()
        response.json.return_value = {"notes": []}
        session.get.return_value = response
        client = OpenReviewClient(session)
        client._login_attempted = True

        client.get_notes("invitation", "venue")

        self.assertEqual(
            session.get.call_args.kwargs["headers"]["Accept"],
            "application/json")

    def test_archival_source_merges_without_changing_stable_id(self):
        existing = {
            "id": "openreview-id", "title": "A Useful Paper",
            "authors": ["Ada Lovelace"], "abstract": "Draft",
            "metadata_source": "openreview", "source_id": "openreview-id",
            "source_ids": {"openreview": "openreview-id"},
            "publication_status": "provisional",
        }
        incoming = {
            "id": "lovelace26a", "title": "A Useful Paper",
            "authors": ["Ada Lovelace"], "abstract": "Camera ready",
            "metadata_source": "pmlr", "source_id": "lovelace26a",
            "source_ids": {"pmlr": "lovelace26a"},
            "publication_status": "archival",
        }

        self.assertTrue(
            BaseScraper._identity_keys(existing) &
            BaseScraper._identity_keys(incoming))
        BaseScraper._merge_record(existing, incoming)

        self.assertEqual(existing["id"], "openreview-id")
        self.assertEqual(existing["abstract"], "Camera ready")
        self.assertEqual(existing["source_ids"], {
            "openreview": "openreview-id", "pmlr": "lovelace26a"})
        self.assertEqual(existing["publication_status"], "archival")

    def test_same_provisional_source_refreshes_changed_metadata(self):
        existing = {
            "id": "paper", "title": "Old title", "authors": ["Ada"],
            "abstract": "Old abstract", "metadata_source": "openreview",
            "source_id": "paper", "publication_status": "provisional",
        }
        incoming = dict(existing, title="Camera-ready title",
                        abstract="Camera-ready abstract")

        BaseScraper._merge_record(existing, incoming)

        self.assertEqual(existing["title"], "Camera-ready title")
        self.assertEqual(existing["abstract"], "Camera-ready abstract")

    def test_openreview_note_preserves_provenance_and_direct_pdf(self):
        note = {
            "id": "paper-id",
            "content": {
                "title": {"value": "A Useful Paper"},
                "authors": {"value": ["Ada Lovelace"]},
                "abstract": {"value": "Abstract"},
                "venue": {"value": "ICML 2026 spotlight"},
                "pdf": {"value": "/pdf/hash.pdf"},
            },
        }

        paper = OpenReviewClient(None).note_to_paper(note)

        self.assertEqual(paper["pdf_url"], "https://openreview.net/pdf/hash.pdf")
        self.assertEqual(paper["status"], "Spotlight")
        self.assertEqual(paper["publication_status"], "provisional")

    def test_ijcai_accepted_list_parser(self):
        html = b'''<ol><li class="ij-paper">
          <span class="ij-pid">#29</span>
          <h3 class="ij-ptitle">A Useful Paper</h3>
          <div><span class="ij-author">Ada Lovelace</span></div>
          <div class="ij-abstract">Useful abstract.</div>
          <span class="ij-kw" title="Machine Learning - Theory">Theory</span>
        </li></ol>'''

        papers = IJCAIScraper._parse_accepted_list(
            html, 2026,
            "https://2026.ijcai.org/accepted-papers/?ijtrack=main-track")

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["id"], "2026-29")
        self.assertEqual(papers[0]["authors"], ["Ada Lovelace"])
        self.assertEqual(papers[0]["publication_status"], "provisional")

    def test_parallel_pdf_retry_checkpoints_distinct_papers(self):
        class RetryScraper:
            PDF_DOWNLOAD_WORKERS = 2
            CHECKPOINT_INTERVAL = 100
            conference = "test"

            @staticmethod
            def _has_valid_local_pdf(paper):
                return False

            def download_pdf(self, paper, year):
                time.sleep(0.001)
                paper["pdf_path"] = f"papers/test/{year}/{paper['id']}.pdf"
                return True

        papers = [
            {"id": str(index), "pdf_url": f"https://x/{index}.pdf"}
            for index in range(10)
        ]

        completed = BaseScraper._retry_missing_pdfs(RetryScraper(), papers, 2026)

        self.assertEqual(completed, 10)
        self.assertTrue(all(paper.get("pdf_path") for paper in papers))

    def test_local_pdf_check_rejects_html_with_pdf_extension(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "papers/icml/2026/p.pdf"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"<html>challenge</html>" + b"x" * 2048)
            paper = {"pdf_path": "papers/icml/2026/p.pdf"}
            with patch("scrapers.base.DATA_ROOT", root):
                self.assertFalse(BaseScraper._has_valid_local_pdf(paper))
            path.write_bytes(b"%PDF-1.7\n" + b"x" * 2048)
            with patch("scrapers.base.DATA_ROOT", root):
                self.assertTrue(BaseScraper._has_valid_local_pdf(paper))


class MonitorTests(unittest.TestCase):
    def test_registry_is_valid(self):
        entries = load_registry(
            Path(__file__).resolve().parents[1] /
            "automation" / "conferences.json")
        self.assertEqual(
            {(entry["venue"], entry["year"]) for entry in entries},
            {("icml", 2026), ("aistats", 2026), ("ijcai", 2026)})

    def test_state_is_separate_and_upserted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir) / "state.sqlite3")
            event = {
                "venue": "icml", "year": 2026, "source_key": "test:x",
                "checked_at": "2026-01-01T00:00:00+00:00",
                "status": "available", "content_hash": "abc",
                "item_count": 3, "detail": "",
                "snapshot_path": "/tmp/first.json",
            }
            store.put(event)
            event.update(content_hash="def", item_count=4)
            store.put(event)

            state = store.get("icml", 2026, "test:x")

        self.assertEqual(state["content_hash"], "def")
        self.assertEqual(state["item_count"], 4)
        self.assertEqual(state["snapshot_path"], "/tmp/first.json")

    def test_snapshot_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "state.sqlite3"
            event = {
                "venue": "ijcai", "year": 2026, "content_hash": "abc123",
            }
            first = save_snapshot(state, event, "official_html", b"first", ".html")
            second = save_snapshot(state, event, "official_html", b"second", ".html")

            self.assertNotEqual(first, second)
            self.assertEqual(Path(first).read_bytes(), b"first")
            self.assertEqual(Path(second).read_bytes(), b"second")


class IndependentValidationTests(unittest.TestCase):
    def test_count_duplicates_metadata_and_pdf_signature(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf = root / "papers/icml/2026/p.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"not a pdf" + b"x" * 2048)
            paper = {
                "id": "p", "title": "T", "authors": ["A"], "abstract": "A",
                "year": 2026, "conference": "icml", "url": "https://x",
                "bibtex": "@x{p}", "pdf_path": "papers/icml/2026/p.pdf",
            }

            issues = validate(
                [paper, dict(paper)], root, level="metadata",
                require_pdfs=True, expected_count=3)

        self.assertEqual(issues, {
            "duplicate_ids": 1, "invalid_pdf_signature": 2, "paper_count": 1})


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

    def test_readme_coverage_uses_actual_years(self):
        rendered = render_readme_coverage({
            "colt": {2025: {}, 2026: {}},
            "naacl": {2024: {}, 2026: {}},
        })
        self.assertEqual(rendered, (
            "- **COLT** (2025–2026)\n"
            "- **NAACL** (2024, 2026)"))

    def test_readme_coverage_labels_provisional_years(self):
        rendered = render_readme_coverage({
            "icml": {
                2025: {"provisional_papers": 0},
                2026: {"provisional_papers": 1},
            },
        })
        self.assertEqual(rendered, "- **ICML** (2025; provisional: 2026)")

    def test_generated_section_replacement_requires_markers(self):
        text = "before\n<!-- S -->\nold\n<!-- E -->\nafter\n"
        self.assertEqual(
            replace_generated_section(text, "<!-- S -->", "<!-- E -->", "new"),
            "before\n<!-- S -->\nnew\n<!-- E -->\nafter\n")
        with self.assertRaises(ValueError):
            replace_generated_section("no markers", "<!-- S -->", "<!-- E -->", "new")

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
