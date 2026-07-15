import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.agent_worktree_retention import (
    WorktreeRetentionPolicy,
    prune_agent_worktrees,
)
from automation.codex_agent import CodexProcessResult, run_claimed_codex_agent
from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.due_policy import claim_due_agent_run
from automation.event_dates import EventDateEstimate, EventDateTarget, initialize_event_dates


NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


class Provider:
    name = model = prompt_version = "fake"

    def estimate(self, request):
        return EventDateEstimate(NOW.date(), "fixture")


class Invoker:
    def invoke(self, invocation):
        (invocation.cwd / "change.txt").write_text("changed\n", encoding="utf-8")
        return CodexProcessResult(0, json.dumps({
            "disposition": "success", "explanation": "fixture success",
            "suggested_retry_at": None, "failure_category": None,
        }), "")


def git(root, *args):
    return subprocess.run(
        ("git", *args), cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


class AgentWorktreeRetentionTests(unittest.TestCase):
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
        self.runs = self.root / "runs"
        self.outcomes = []
        for venue in ("aistats", "icml"):
            initialize_event_dates(
                self.state, (EventDateTarget(venue, 2026),), Provider(),
                clock=lambda: NOW,
            )
            claim = claim_due_agent_run(self.state, clock=lambda: NOW).claim
            self.outcomes.append(run_claimed_codex_agent(
                self.state, self.repo, self.runs, claim,
                clock=lambda: NOW, invoker=Invoker(),
            ))
        self.manual = self.runs / "manual-canary"
        git(self.repo, "worktree", "add", "-b", "manual-canary", str(self.manual))

    def tearDown(self):
        self.temp.cleanup()

    def test_count_bound_removes_only_registered_oldest_worktree(self):
        pruned = prune_agent_worktrees(
            self.state, self.repo, self.runs, clock=lambda: NOW,
            policy=WorktreeRetentionPolicy(
                max_retained=1, max_age=timedelta(days=30)
            ),
        )

        self.assertEqual(len(pruned), 1)
        self.assertFalse(pruned[0].worktree_path.exists())
        self.assertTrue(self.manual.exists())
        self.assertTrue(self.outcomes[1].worktree_path.exists())
        with ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW
        ) as repository:
            artifact = repository.get_agent_execution_artifact(pruned[0].run_id)
        self.assertEqual(artifact.retention_status, "removed")
        self.assertEqual(prune_agent_worktrees(
            self.state, self.repo, self.runs, clock=lambda: NOW,
            policy=WorktreeRetentionPolicy(
                max_retained=1, max_age=timedelta(days=30)
            ),
        ), ())

    def test_failed_removal_is_visible_and_retryable(self):
        failed = prune_agent_worktrees(
            self.state, self.root, self.runs, clock=lambda: NOW,
            policy=WorktreeRetentionPolicy(
                max_retained=1, max_age=timedelta(days=30)
            ),
        )
        self.assertEqual(failed[0].status, "removal_failed")
        self.assertTrue(failed[0].worktree_path.exists())
        self.assertTrue(self.manual.exists())

        retried = prune_agent_worktrees(
            self.state, self.repo, self.runs, clock=lambda: NOW,
            policy=WorktreeRetentionPolicy(
                max_retained=1, max_age=timedelta(days=30)
            ),
        )
        self.assertEqual(retried[0].status, "removed")
        self.assertFalse(retried[0].worktree_path.exists())

    def test_age_bound_removes_registered_worktrees_but_not_manual_canary(self):
        pruned = prune_agent_worktrees(
            self.state, self.repo, self.runs,
            clock=lambda: NOW + timedelta(days=31),
            policy=WorktreeRetentionPolicy(
                max_retained=10, max_age=timedelta(days=30)
            ),
        )
        self.assertEqual(len(pruned), 2)
        self.assertTrue(all(item.status == "removed" for item in pruned))
        self.assertTrue(self.manual.exists())

    def test_per_run_removal_limit_is_hard(self):
        first = prune_agent_worktrees(
            self.state, self.repo, self.runs,
            clock=lambda: NOW + timedelta(days=31),
            policy=WorktreeRetentionPolicy(
                max_retained=10,
                max_age=timedelta(days=30),
                max_removals_per_run=1,
            ),
        )
        self.assertEqual(len(first), 1)
        second = prune_agent_worktrees(
            self.state, self.repo, self.runs,
            clock=lambda: NOW + timedelta(days=31),
            policy=WorktreeRetentionPolicy(
                max_retained=10,
                max_age=timedelta(days=30),
                max_removals_per_run=1,
            ),
        )
        self.assertEqual(len(second), 1)
        self.assertTrue(self.manual.exists())


if __name__ == "__main__":
    unittest.main()
