import argparse
import json
import tempfile
import unittest
from pathlib import Path

from automation.agent_success_rehearsal import (
    AgentSuccessRehearsalError,
    _HistoricalDateProvider,
    _independent_validation,
    _private_directory,
    run,
)
from automation.agent_report_recovery import (
    _worktree_path,
    run as recover_report,
)
from automation.discovery import request_from_catalog
from automation.configuration import load_venue_catalog


class AgentSuccessRehearsalTests(unittest.TestCase):
    def test_report_recovery_requires_separate_resend_authority_first(self):
        with self.assertRaisesRegex(AgentSuccessRehearsalError, "requires resend"):
            recover_report(argparse.Namespace(authorize_resend_live=False))

    def test_persisted_worktree_paths_are_normalized_by_recovery_module(self):
        self.assertEqual(_worktree_path("/tmp/rehearsal"), Path("/tmp/rehearsal"))
        with self.assertRaisesRegex(AgentSuccessRehearsalError, "path is invalid"):
            _worktree_path("relative/rehearsal")

    def test_all_separate_live_authorities_are_required_before_install_access(self):
        args = argparse.Namespace(
            authorize_codex_live=True,
            authorize_downloads_live=False,
            authorize_resend_live=True,
            authorization_id="test-authorization",
        )
        with self.assertRaisesRegex(AgentSuccessRehearsalError, "require authority"):
            run(args)

    def test_authorization_directory_is_private_and_create_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "proof"
            self.assertEqual(_private_directory(root, create=True), root)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                _private_directory(root, create=True)

    def test_fixed_provider_refuses_target_substitution(self):
        catalog = load_venue_catalog()
        accepted = request_from_catalog(catalog, "colt", 2011)
        self.assertEqual(
            _HistoricalDateProvider().estimate(accepted).event_date.isoformat(),
            "2011-07-07",
        )
        rejected = request_from_catalog(catalog, "colt", 2012)
        with self.assertRaisesRegex(AgentSuccessRehearsalError, "target changed"):
            _HistoricalDateProvider().estimate(rejected)

    def test_independent_archival_validation_reads_only_worktree_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            worktree = Path(temporary)
            metadata = worktree / "data" / "metadata" / "colt"
            papers = worktree / "data" / "papers" / "colt" / "2011"
            metadata.mkdir(parents=True)
            papers.mkdir(parents=True)
            pdf = papers / "paper.pdf"
            pdf.write_bytes(b"%PDF-" + b"x" * 1024)
            payload = [{
                "id": "paper", "title": "Paper", "authors": ["Author"],
                "year": 2011, "conference": "COLT", "url": "https://example.test",
                "bibtex": "@article{x}", "abstract": "Abstract",
                "pdf_url": "https://example.test/paper.pdf",
                "pdf_path": "data/papers/colt/2011/paper.pdf",
            }]
            (metadata / "colt_2011.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            self.assertEqual(_independent_validation(worktree), (1, {}))
            pdf.unlink()
            with self.assertRaisesRegex(
                AgentSuccessRehearsalError, "independent archival validation failed"
            ):
                _independent_validation(worktree)


if __name__ == "__main__":
    unittest.main()
