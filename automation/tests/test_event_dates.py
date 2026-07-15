import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from automation.control_state import (
    ControlStateRepository,
    EventDateScheduleError,
)
from automation.discovery import ProviderError
from automation.domain import Writer
from automation.event_dates import (
    EventDateEstimate,
    EventDateTarget,
    initialize_event_dates,
)


NOW = datetime(2026, 1, 10, 14, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class FakeProvider:
    name = "fake-date-search"
    model = "fake-model"
    prompt_version = "v1"

    def __init__(self, event_date=date(2026, 7, 13), *, error=None):
        self.event_date = event_date
        self.error = error
        self.calls = []

    def estimate(self, request):
        self.calls.append((request.venue_id, request.year))
        if self.error is not None:
            raise self.error
        return EventDateEstimate(
            event_date=self.event_date,
            explanation="Approximate conference start date from web search.",
        )


class EventDateInitializationTests(unittest.TestCase):
    def test_one_lookup_schedules_future_date_and_predate_replay_sleeps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            provider = FakeProvider()
            clock = MutableClock()
            target = EventDateTarget("icml", 2026)

            first = initialize_event_dates(
                path, (target,), provider, clock=clock
            )
            clock.value = NOW + timedelta(days=30)
            replay = initialize_event_dates(
                path, (target,), provider, clock=clock
            )

            self.assertEqual(provider.calls, [("icml", 2026)])
            self.assertEqual(first.registered_count, 1)
            self.assertEqual(first.attempted_count, 1)
            self.assertEqual(first.scheduled_count, 1)
            self.assertEqual(first.retry_count, 0)
            self.assertEqual(replay.registered_count, 0)
            self.assertEqual(replay.attempted_count, 0)
            self.assertEqual(replay.records[0].status, "scheduled")
            self.assertEqual(replay.records[0].estimated_event_date, "2026-07-13")
            self.assertEqual(replay.records[0].next_check_at,
                             "2026-07-13T13:00:00Z")

    def test_past_estimate_becomes_due_at_observation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            provider = FakeProvider(date(2025, 7, 13))

            outcome = initialize_event_dates(
                path,
                (EventDateTarget("icml", 2025),),
                provider,
                clock=MutableClock(),
            )

            self.assertEqual(outcome.records[0].next_check_at,
                             "2026-01-10T14:00:00Z")

    def test_missing_date_and_expected_provider_error_schedule_long_retry(self):
        for provider, expected_reason in (
            (FakeProvider(None), "date_not_found"),
            (
                FakeProvider(error=ProviderError(
                    "fixture failure", category="fixture_provider_failure"
                )),
                "fixture_provider_failure",
            ),
        ):
            with self.subTest(expected_reason=expected_reason), \
                    tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.sqlite3"
                clock = MutableClock()
                target = EventDateTarget("icml", 2026)

                first = initialize_event_dates(
                    path, (target,), provider, clock=clock
                )
                clock.value = NOW + timedelta(days=29)
                replay = initialize_event_dates(
                    path, (target,), provider, clock=clock
                )

                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(first.retry_count, 1)
                self.assertEqual(first.records[0].status, "pending")
                self.assertEqual(first.records[0].last_failure_category,
                                 expected_reason)
                self.assertEqual(first.records[0].next_check_at,
                                 "2026-02-09T14:00:00Z")
                self.assertEqual(replay.attempted_count, 0)

    def test_selection_limit_processes_one_new_target_per_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            provider = FakeProvider()
            targets = (
                EventDateTarget("icml", 2026),
                EventDateTarget("aistats", 2026),
            )

            first = initialize_event_dates(
                path, targets, provider, clock=MutableClock(), selection_limit=1
            )
            second = initialize_event_dates(
                path, targets, provider, clock=MutableClock(), selection_limit=1
            )

            self.assertEqual(first.attempted_count, 1)
            self.assertEqual(second.attempted_count, 1)
            self.assertEqual(provider.calls,
                             [("aistats", 2026), ("icml", 2026)])

    def test_monthly_lookup_budget_defers_without_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            provider = FakeProvider()
            targets = (
                EventDateTarget("aistats", 2026),
                EventDateTarget("icml", 2026),
            )

            first = initialize_event_dates(
                path, targets, provider, clock=MutableClock(),
                selection_limit=1, monthly_lookup_limit=1,
            )
            second = initialize_event_dates(
                path, targets, provider, clock=MutableClock(),
                selection_limit=1, monthly_lookup_limit=1,
            )

            self.assertEqual(first.attempted_count, 1)
            self.assertEqual(first.deferred_count, 0)
            self.assertEqual(second.attempted_count, 0)
            self.assertEqual(second.deferred_count, 1)
            self.assertEqual(provider.calls, [("aistats", 2026)])
            pending = next(
                record for record in second.records
                if record.venue_id == "icml"
            )
            self.assertEqual(pending.last_failure_category, "monthly_budget")
            self.assertEqual(pending.next_check_at, "2026-02-01T00:00:00Z")

    def test_unexpected_interruption_remains_active_and_blocks_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            provider = FakeProvider(error=RuntimeError("unexpected"))
            target = EventDateTarget("icml", 2026)

            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                initialize_event_dates(
                    path, (target,), provider, clock=MutableClock()
                )
            with self.assertRaisesRegex(
                EventDateScheduleError, "active or ambiguously interrupted"
            ):
                initialize_event_dates(
                    path, (target,), FakeProvider(), clock=MutableClock()
                )

            with ControlStateRepository(
                path,
                writer=Writer.LOCAL_CONTROL_PLANE,
                clock=MutableClock(),
            ) as repository:
                record = repository.get_event_date_schedule("icml", 2026)
                self.assertEqual(record.status, "active")
                self.assertEqual(len(repository.event_date_attempt_history(
                    "icml", 2026
                )), 1)

    def test_unknown_venue_is_rejected_before_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = FakeProvider()
            with self.assertRaisesRegex(ValueError, "unknown venue"):
                initialize_event_dates(
                    Path(directory) / "state.sqlite3",
                    (EventDateTarget("unknown", 2026),),
                    provider,
                    clock=MutableClock(),
                )
            self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
