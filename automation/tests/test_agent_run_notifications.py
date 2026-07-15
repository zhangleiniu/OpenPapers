import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.agent_run_notifications import deliver_agent_run_email
from automation.codex_agent import CodexProcessResult, run_claimed_codex_agent
from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.due_policy import claim_due_agent_run
from automation.event_dates import (
    EventDateEstimate,
    EventDateTarget,
    initialize_event_dates,
)
from automation.notifications import FailureCategory, TransportFailure, TransportReceipt


NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


class Provider:
    name = model = prompt_version = "fake"

    def estimate(self, request):
        return EventDateEstimate(NOW.date(), "fixture")


class Invoker:
    def invoke(self, invocation):
        (invocation.cwd / "agent-change.txt").write_text("changed\n", encoding="utf-8")
        return CodexProcessResult(0, json.dumps({
            "disposition": "not_ready",
            "explanation": "Proceedings are not published yet.",
            "suggested_retry_at": None,
            "failure_category": None,
        }), "")


class Transport:
    def __init__(self, result, on_send=None):
        self.result = result
        self.on_send = on_send
        self.calls = []

    def send(self, intent, *, idempotency_key):
        self.calls.append((intent, idempotency_key))
        if self.on_send is not None:
            self.on_send()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def git(root, *args):
    return subprocess.run(
        ("git", *args), cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


class AgentRunNotificationTests(unittest.TestCase):
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
        initialize_event_dates(
            self.state, (EventDateTarget("icml", 2026),), Provider(),
            clock=lambda: NOW,
        )
        self.claim = claim_due_agent_run(self.state, clock=lambda: NOW).claim
        run_claimed_codex_agent(
            self.state, self.repo, self.root / "runs", self.claim,
            clock=lambda: NOW, invoker=Invoker(),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_delivered_report_contains_review_and_retry_state_and_suppresses_replay(self):
        transport = Transport(TransportReceipt("receipt:accepted"))
        outcome = deliver_agent_run_email(
            self.state, self.claim.run_id, transport, clock=lambda: NOW
        )

        self.assertEqual(outcome.status, "delivered")
        self.assertEqual(len(transport.calls), 1)
        intent, key = transport.calls[0]
        self.assertEqual(key, intent.notification_id)
        for expected in (
            "icml 2026", "not_ready", "Proceedings are not published yet.",
            "agent-change.txt", "Worktree:", "Retry state:",
        ):
            self.assertIn(expected, intent.body)

        replay = Transport(TransportReceipt("receipt:unused"))
        suppressed = deliver_agent_run_email(
            self.state, self.claim.run_id, replay, clock=lambda: NOW
        )
        self.assertFalse(suppressed.attempted)
        self.assertEqual(replay.calls, [])

    def test_transient_failure_retries_and_permanent_failure_is_visible(self):
        transient = Transport(TransportFailure(FailureCategory.TIMEOUT))
        failed = deliver_agent_run_email(
            self.state, self.claim.run_id, transient, clock=lambda: NOW
        )
        self.assertEqual(failed.status, "retryable")

        retried = deliver_agent_run_email(
            self.state, self.claim.run_id,
            Transport(TransportReceipt("receipt:retry")), clock=lambda: NOW,
        )
        self.assertEqual(retried.status, "delivered")
        self.assertEqual(retried.attempt_number, 2)

        other = self.root / "other.sqlite3"
        initialize_event_dates(
            other, (EventDateTarget("aistats", 2026),), Provider(),
            clock=lambda: NOW,
        )
        claim = claim_due_agent_run(other, clock=lambda: NOW).claim
        run_claimed_codex_agent(
            other, self.repo, self.root / "other-runs", claim,
            clock=lambda: NOW, invoker=Invoker(),
        )
        permanent_transport = Transport(
            TransportFailure(FailureCategory.INVALID_RECIPIENT)
        )
        permanent = deliver_agent_run_email(
            other, claim.run_id, permanent_transport, clock=lambda: NOW
        )
        self.assertEqual(permanent.status, "permanent_failure")
        replay = Transport(TransportReceipt("receipt:unused"))
        self.assertFalse(deliver_agent_run_email(
            other, claim.run_id, replay, clock=lambda: NOW
        ).attempted)
        self.assertEqual(replay.calls, [])

    def test_lease_loss_after_acceptance_leaves_visible_in_flight_state(self):
        class Clock:
            value = NOW

            def __call__(self):
                return self.value

            def expire(self):
                self.value += timedelta(seconds=301)

        from automation.control_state import LeaseLostError

        clock = Clock()
        transport = Transport(
            TransportReceipt("receipt:accepted"), on_send=clock.expire
        )
        with self.assertRaises(LeaseLostError):
            deliver_agent_run_email(
                self.state, self.claim.run_id, transport, clock=clock
            )
        replay = Transport(TransportReceipt("receipt:unused"))
        outcome = deliver_agent_run_email(
            self.state, self.claim.run_id, replay, clock=clock
        )
        self.assertEqual(outcome.status, "in_flight")
        self.assertFalse(outcome.attempted)
        self.assertEqual(replay.calls, [])


if __name__ == "__main__":
    unittest.main()
