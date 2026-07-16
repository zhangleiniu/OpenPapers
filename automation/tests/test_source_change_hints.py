import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.due_policy import (
    AgentRunResult,
    claim_due_agent_run,
    complete_agent_run,
)
from automation.event_dates import (
    EventDateEstimate,
    EventDateTarget,
    initialize_event_dates,
)
from automation.source_change_hints import (
    apply_pending_source_change_hints,
    prepare_source_change_hint_journal,
    record_source_change_hints,
)


NOW = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)
TARGET = EventDateTarget("icml", 2026)


class Provider:
    name = model = prompt_version = "fixture"

    def __init__(self, event_date):
        self.event_date = event_date

    def estimate(self, request):
        return EventDateEstimate(self.event_date, "fixture")


class SourceChangeHintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state.sqlite3"
        self.journal = self.root / "production-wakeups.sqlite3"
        with sqlite3.connect(self.journal) as connection:
            prepare_source_change_hint_journal(connection)
        self.journal.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _events():
        return (
            {"venue": "icml", "year": 2026, "status": "available",
             "changed": True, "source_key": "private-source-one"},
            {"venue": "icml", "year": 2026, "status": "available",
             "changed": True, "source_key": "private-source-two"},
            {"venue": "aistats", "year": 2026, "status": "available",
             "changed": False, "source_key": "unchanged"},
            {"venue": "ijcai", "year": 2026, "status": "unavailable",
             "changed": True, "source_key": "disappeared"},
        )

    def _seed(self, event_date):
        initialize_event_dates(
            self.state, (TARGET,), Provider(event_date), clock=lambda: NOW
        )

    def test_changed_available_source_advances_only_future_schedule(self):
        self._seed(date(2026, 8, 18))
        inserted = record_source_change_hints(
            self.journal, self._events(), observed_at=NOW
        )
        duplicate = record_source_change_hints(
            self.journal, self._events(), observed_at=NOW
        )
        result = apply_pending_source_change_hints(
            self.journal, self.state, (TARGET,), observed_at=NOW,
            minimum_delay=timedelta(hours=6),
        )

        with ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW
        ) as repository:
            schedule = repository.get_agent_schedule("icml", 2026)
            history = repository.agent_run_history("icml", 2026)
        self.assertEqual(inserted, 1)
        self.assertEqual(duplicate, 0)
        self.assertEqual(result.applied_count, 1)
        self.assertEqual(schedule.next_check_at, "2026-07-16T20:00:00Z")
        self.assertEqual(history, ())
        self.assertEqual(
            claim_due_agent_run(
                self.state, clock=lambda: NOW + timedelta(hours=5)
            ).reason,
            "nothing_due",
        )
        self.assertEqual(
            claim_due_agent_run(
                self.state, clock=lambda: NOW + timedelta(hours=6)
            ).reason,
            "claimed",
        )
        replay = apply_pending_source_change_hints(
            self.journal, self.state, (TARGET,), observed_at=NOW,
            minimum_delay=timedelta(hours=6),
        )
        self.assertEqual(replay.applied_count, 0)

    def test_agent_run_after_observation_supersedes_stale_hint(self):
        self._seed(NOW.date())
        record_source_change_hints(self.journal, self._events(), observed_at=NOW)
        claim = claim_due_agent_run(self.state, clock=lambda: NOW).claim
        next_check = NOW + timedelta(days=7)
        completed = complete_agent_run(
            self.state, claim,
            AgentRunResult("not_ready", "Not ready.", next_check),
            clock=lambda: NOW,
        )

        result = apply_pending_source_change_hints(
            self.journal, self.state, (TARGET,),
            observed_at=NOW + timedelta(minutes=1),
            minimum_delay=timedelta(hours=6),
        )

        self.assertEqual(result.applied_count, 0)
        self.assertEqual(result.ignored_count, 1)
        with ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW
        ) as repository:
            schedule = repository.get_agent_schedule("icml", 2026)
        self.assertEqual(schedule.next_check_at, completed.next_check_at)

    def test_unconfigured_hint_is_ignored_without_creating_target(self):
        record_source_change_hints(
            self.journal,
            ({"venue": "aistats", "year": 2026, "status": "available",
              "changed": True},),
            observed_at=NOW,
        )
        self._seed(date(2026, 8, 18))

        result = apply_pending_source_change_hints(
            self.journal, self.state, (TARGET,), observed_at=NOW,
            minimum_delay=timedelta(hours=6),
        )

        self.assertEqual(result.ignored_count, 1)
        with ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW
        ) as repository:
            self.assertIsNone(repository.get_agent_schedule("aistats", 2026))

    def test_configured_hint_waits_for_date_schedule_then_applies(self):
        record_source_change_hints(self.journal, self._events(), observed_at=NOW)
        with ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW
        ):
            pass
        waiting = apply_pending_source_change_hints(
            self.journal, self.state, (TARGET,), observed_at=NOW,
            minimum_delay=timedelta(hours=6),
        )
        self.assertEqual(waiting.pending_count, 1)

        self._seed(date(2026, 8, 18))
        applied = apply_pending_source_change_hints(
            self.journal, self.state, (TARGET,), observed_at=NOW,
            minimum_delay=timedelta(hours=6),
        )
        self.assertEqual(applied.applied_count, 1)


if __name__ == "__main__":
    unittest.main()
