import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.codex_agent import (
    CodexProcessResult,
    _network_arguments,
    parse_codex_result,
    run_claimed_codex_agent,
)
from automation.control_state import ControlStateRepository
from automation.due_policy import DuePolicy, claim_due_agent_run
from automation.domain import Writer
from automation.event_dates import EventDateEstimate, EventDateTarget, initialize_event_dates


NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


class Provider:
    name = model = prompt_version = "fake"
    def estimate(self, request):
        return EventDateEstimate(NOW.date(), "fixture")


class FakeInvoker:
    def __init__(self, *, timeout=False, malformed=False, inconsistent=False):
        self.timeout = timeout
        self.malformed = malformed
        self.inconsistent = inconsistent
        self.invocation = None

    def invoke(self, invocation):
        self.invocation = invocation
        if self.timeout:
            raise subprocess.TimeoutExpired(invocation.argv, invocation.timeout_seconds)
        (invocation.cwd / "agent-change.txt").write_text("changed\n", encoding="utf-8")
        output = "bad" if self.malformed else json.dumps({
            "disposition": "success", "explanation": "fixture success",
            "suggested_retry_at": None, "failure_category": None,
        })
        if self.inconsistent:
            output = json.dumps({
                "disposition": "not_ready", "explanation": "not ready",
                "suggested_retry_at": None, "failure_category": "not_ready",
            })
        return CodexProcessResult(0, output, "")


def git(root, *args):
    return subprocess.run(("git", *args), cwd=root, text=True,
                          capture_output=True, check=True).stdout.strip()


class CodexAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "fixture")
        self.state = self.root / "state.sqlite3"
        initialize_event_dates(self.state, (EventDateTarget("icml", 2026),),
                               Provider(), clock=lambda: NOW)
        self.claim = claim_due_agent_run(self.state, clock=lambda: NOW).claim

    def tearDown(self):
        self.temp.cleanup()

    def test_success_edits_only_worktree_and_uses_safe_codex_flags(self):
        before = git(self.repo, "rev-parse", "HEAD")
        invoker = FakeInvoker()
        outcome = run_claimed_codex_agent(
            self.state, self.repo, self.root / "runs", self.claim,
            clock=lambda: NOW, invoker=invoker,
        )

        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertFalse((self.repo / "agent-change.txt").exists())
        self.assertTrue((outcome.worktree_path / "agent-change.txt").exists())
        self.assertIn("?? agent-change.txt", outcome.changed_files)
        argv = invoker.invocation.argv
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules",
                     "workspace-write", "never", "--output-schema"):
            self.assertIn(flag, argv)
        self.assertIn('mcp_servers={}', argv)
        self.assertIn("sandbox_workspace_write.network_access=true", argv)
        self.assertIn("features.network_proxy.enabled=true", argv)
        network_policy = next(
            item for item in argv if item.startswith("features.network_proxy.domains=")
        )
        self.assertIn('"icml.cc"="allow"', network_policy)
        self.assertIn('"proceedings.mlr.press"="allow"', network_policy)
        self.assertNotIn('"*"="allow"', network_policy)
        self.assertLess(argv.index("--ask-for-approval"), argv.index("exec"))
        self.assertEqual(outcome.result.disposition, "success")
        with ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW
        ) as repository:
            artifact = repository.get_agent_execution_artifact(self.claim.run_id)
            report = repository.get_agent_run_report(self.claim.run_id)
        self.assertEqual(artifact.lifecycle, "terminal")
        self.assertEqual(artifact.changed_files, ("?? agent-change.txt",))
        self.assertEqual(artifact.retention_status, "retained")
        self.assertEqual(report.status, "pending")
        self.assertEqual(report.schedule_status, "completed")

    def test_network_allowlist_is_exactly_scoped_to_claimed_venue(self):
        arguments = _network_arguments("colt")
        policy = arguments[-1]
        self.assertIn('"learningtheory.org"="allow"', policy)
        self.assertIn('"proceedings.mlr.press"="allow"', policy)
        self.assertNotIn("openreview.net", policy)
        with self.assertRaisesRegex(ValueError, "not in the catalog"):
            _network_arguments("unknown")

    def test_prompt_requests_bounded_evidence_based_not_ready_retry(self):
        invoker = FakeInvoker()
        policy = DuePolicy(
            minimum_retry_delay=timedelta(hours=1),
            max_suggested_retry_delay=timedelta(days=30),
        )
        run_claimed_codex_agent(
            self.state, self.repo, self.root / "runs", self.claim,
            clock=lambda: NOW, invoker=invoker, policy=policy,
        )

        prompt = invoker.invocation.argv[-1]
        self.assertIn("suggested_retry_at", prompt)
        self.assertIn("2026-07-15T15:00:00Z", prompt)
        self.assertIn("2026-08-14T14:00:00Z", prompt)
        self.assertIn("thousands of papers", prompt)
        self.assertIn("do not treat a date or source change as readiness proof", prompt)

    def test_retry_timestamp_requires_timezone_and_normalizes_to_utc(self):
        with self.assertRaisesRegex(ValueError, "retry time"):
            parse_codex_result(json.dumps({
                "disposition": "not_ready", "explanation": "Not ready.",
                "suggested_retry_at": "2026-07-16T09:00:00",
                "failure_category": None,
            }))
        result = parse_codex_result(json.dumps({
            "disposition": "not_ready", "explanation": "Not ready.",
            "suggested_retry_at": "2026-07-16T09:00:00-05:00",
            "failure_category": None,
        }))
        self.assertEqual(
            result.suggested_retry_at,
            datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc),
        )

    def test_timeout_and_malformed_output_fail_closed_and_preserve_worktree(self):
        for invoker, category, disposition, venue, suffix in (
            (FakeInvoker(timeout=True), "timeout", "failed", "icml", "runs"),
            (FakeInvoker(malformed=True), "invalid_result", "failed", "aistats", "runs2"),
            (FakeInvoker(inconsistent=True), None, "not_ready", "ijcai", "runs3"),
        ):
            with self.subTest(category=category, venue=venue):
                if category != "timeout":
                    state = self.root / f"{venue}.sqlite3"
                    initialize_event_dates(state, (EventDateTarget(venue, 2026),),
                                           Provider(), clock=lambda: NOW)
                    claim = claim_due_agent_run(state, clock=lambda: NOW).claim
                    runs = self.root / suffix
                else:
                    state, claim, runs = self.state, self.claim, self.root / "runs"
                outcome = run_claimed_codex_agent(
                    state, self.repo, runs, claim, clock=lambda: NOW, invoker=invoker
                )
                self.assertEqual(outcome.result.disposition, disposition)
                self.assertEqual(outcome.result.failure_category, category)
                self.assertTrue(outcome.worktree_path.exists())


if __name__ == "__main__":
    unittest.main()
