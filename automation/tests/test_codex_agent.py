import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from automation.codex_agent import CodexProcessResult, run_claimed_codex_agent
from automation.due_policy import claim_due_agent_run
from automation.event_dates import EventDateEstimate, EventDateTarget, initialize_event_dates


NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


class Provider:
    name = model = prompt_version = "fake"
    def estimate(self, request):
        return EventDateEstimate(NOW.date(), "fixture")


class FakeInvoker:
    def __init__(self, *, timeout=False, malformed=False):
        self.timeout = timeout
        self.malformed = malformed
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
        self.assertEqual(outcome.result.disposition, "success")

    def test_timeout_and_malformed_output_fail_closed_and_preserve_worktree(self):
        for invoker, category in (
            (FakeInvoker(timeout=True), "timeout"),
            (FakeInvoker(malformed=True), "invalid_result"),
        ):
            with self.subTest(category=category):
                if category == "invalid_result":
                    state = self.root / "second.sqlite3"
                    initialize_event_dates(state, (EventDateTarget("aistats", 2026),),
                                           Provider(), clock=lambda: NOW)
                    claim = claim_due_agent_run(state, clock=lambda: NOW).claim
                    runs = self.root / "runs2"
                else:
                    state, claim, runs = self.state, self.claim, self.root / "runs"
                outcome = run_claimed_codex_agent(
                    state, self.repo, runs, claim, clock=lambda: NOW, invoker=invoker
                )
                self.assertEqual(outcome.result.disposition, "failed")
                self.assertEqual(outcome.result.failure_category, category)
                self.assertTrue(outcome.worktree_path.exists())


if __name__ == "__main__":
    unittest.main()
