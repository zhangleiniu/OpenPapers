import ast
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from automation.control_state import AgentScheduleError, ControlStateRepository
from automation.domain import Writer
from automation.due_policy import (
    AgentRunResult,
    DuePolicy,
    claim_due_agent_run,
    complete_agent_run,
    resume_agent_schedule,
)
from automation.event_dates import (
    EventDateEstimate,
    EventDateTarget,
    initialize_event_dates,
)


NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
MODULE = Path(__file__).resolve().parents[1] / "due_policy.py"


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class DateProvider:
    name = "fake-date"
    model = "fake"
    prompt_version = "v1"

    def __init__(self, event_date=NOW.date()):
        self.event_date = event_date

    def estimate(self, request):
        return EventDateEstimate(self.event_date, "Fixture date.")


def seed(path, targets, *, clock=None, event_date=NOW.date()):
    resolved_clock = clock or MutableClock()
    initialize_event_dates(
        path,
        tuple(EventDateTarget(venue, year) for venue, year in targets),
        DateProvider(event_date),
        clock=resolved_clock,
        selection_limit=len(targets),
    )


def seed_continuous(path, venue_id, year, *, clock=None):
    """Seed a continuous-lifecycle venue the way it's actually registered in
    production: register_continuous_event_date + ensure_scheduled_agent_target,
    never event-date discovery (there is no date to discover)."""
    resolved_clock = clock or MutableClock()
    now = resolved_clock()
    with ControlStateRepository(
        path, writer=Writer.LOCAL_CONTROL_PLANE, clock=resolved_clock,
    ) as repository:
        lease = repository.acquire_lease("fixture-continuous")
        try:
            repository.register_continuous_event_date(
                venue_id, year, registered_at=now, lease=lease,
            )
            repository.ensure_scheduled_agent_target(
                venue_id, year, next_check_at=now, registered_at=now, lease=lease,
            )
        finally:
            repository.release_lease(lease)


class DueStatePolicyTests(unittest.TestCase):
    def test_predate_wakeup_is_idle_and_due_claim_has_global_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            seed(
                path,
                (("icml", 2026), ("aistats", 2026)),
                clock=clock,
                event_date=date(2026, 7, 16),
            )

            early = claim_due_agent_run(path, clock=clock)
            clock.value = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)
            first = claim_due_agent_run(path, clock=clock)
            blocked = claim_due_agent_run(path, clock=clock)

            self.assertEqual(early.reason, "nothing_due")
            self.assertEqual(first.reason, "claimed")
            self.assertIsNotNone(first.claim)
            self.assertEqual(blocked.reason, "active_run")
            self.assertIsNone(blocked.claim)
            self.assertEqual(blocked.schedule.active_run_id, first.claim.run_id)

    def test_success_and_needs_human_stop_automatic_work(self):
        for disposition, expected_status in (
            ("success", "completed"),
            ("needs_human", "needs_human"),
        ):
            with self.subTest(disposition=disposition), \
                    tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.sqlite3"
                clock = MutableClock()
                seed(path, (("icml", 2026),), clock=clock)
                claim = claim_due_agent_run(path, clock=clock).claim

                record = complete_agent_run(
                    path,
                    claim,
                    AgentRunResult(disposition, "Fixture outcome."),
                    clock=clock,
                )

                self.assertEqual(record.status, expected_status)
                self.assertIsNone(record.next_check_at)
                self.assertEqual(
                    claim_due_agent_run(path, clock=clock).reason,
                    "nothing_due",
                )

    def test_continuous_venue_success_recurs_instead_of_terminating(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            seed_continuous(path, "jmlr", 2026, clock=clock)

            claim = claim_due_agent_run(path, clock=clock).claim
            first = complete_agent_run(
                path, claim, AgentRunResult("success", "New papers scraped."),
                clock=clock, is_continuous=True,
            )
            self.assertEqual(first.status, "scheduled")
            self.assertEqual(first.last_disposition, "success")
            self.assertEqual(first.next_check_at, "2026-08-14T14:00:00Z")

            # It really recurs: advance past the recheck time, claim again,
            # and confirm success never reaches a terminal 'completed' state.
            clock.value = NOW + timedelta(days=31)
            second_claim = claim_due_agent_run(path, clock=clock).claim
            self.assertIsNotNone(second_claim)
            second = complete_agent_run(
                path, second_claim, AgentRunResult("success", "Checked again."),
                clock=clock, is_continuous=True,
            )
            self.assertEqual(second.status, "scheduled")
            self.assertIsNotNone(second.next_check_at)

    def test_continuous_venue_not_ready_and_failed_behave_normally(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            seed_continuous(path, "jmlr", 2026, clock=clock)
            claim = claim_due_agent_run(path, clock=clock).claim

            record = complete_agent_run(
                path, claim, AgentRunResult("not_ready", "Nothing new yet."),
                clock=clock, is_continuous=True,
            )

            self.assertEqual(record.status, "scheduled")
            self.assertEqual(record.last_disposition, "not_ready")

    def test_non_continuous_success_is_unaffected_by_the_new_parameter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            seed(path, (("icml", 2026),), clock=clock)
            claim = claim_due_agent_run(path, clock=clock).claim

            record = complete_agent_run(
                path, claim, AgentRunResult("success", "Fixture outcome."),
                clock=clock,
            )

            self.assertEqual(record.status, "completed")
            self.assertIsNone(record.next_check_at)

    def test_not_ready_uses_bounded_suggestion_then_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            seed(path, (("icml", 2026),), clock=clock)
            claim = claim_due_agent_run(path, clock=clock).claim
            suggestion = NOW + timedelta(days=5)

            suggested = complete_agent_run(
                path,
                claim,
                AgentRunResult(
                    "not_ready", "Proceedings are not ready.", suggestion
                ),
                clock=clock,
            )
            clock.value = suggestion
            second = claim_due_agent_run(path, clock=clock).claim
            fallback = complete_agent_run(
                path,
                second,
                AgentRunResult(
                    "not_ready",
                    "Suggestion violates the venue cooldown.",
                    suggestion + timedelta(hours=1),
                ),
                clock=clock,
            )

            self.assertEqual(suggested.next_check_at, "2026-07-20T14:00:00Z")
            self.assertEqual(suggested.suggested_retry_at, suggested.next_check_at)
            self.assertEqual(fallback.next_check_at, "2026-07-23T14:00:00Z")
            self.assertIsNone(fallback.suggested_retry_at)

    def test_failed_runs_back_off_pause_reject_replay_and_can_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            seed(path, (("icml", 2026),), clock=clock)
            policy = DuePolicy(systemic_failure_threshold=99)
            expected = (
                "2026-07-16T14:00:00Z",
                "2026-07-19T14:00:00Z",
                None,
            )
            final = None
            first_claim = None
            for index, next_check in enumerate(expected):
                claim = claim_due_agent_run(
                    path, clock=clock, policy=policy
                ).claim
                first_claim = first_claim or claim
                final = complete_agent_run(
                    path,
                    claim,
                    AgentRunResult(
                        "failed", "Fixture execution failed.",
                        failure_category="fixture_failure",
                    ),
                    clock=clock,
                    policy=policy,
                )
                self.assertEqual(final.next_check_at, next_check)
                if next_check is not None:
                    clock.value = datetime.fromisoformat(
                        next_check.replace("Z", "+00:00")
                    )

            self.assertEqual(final.status, "paused")
            self.assertEqual(final.consecutive_failures, 3)
            with self.assertRaisesRegex(AgentScheduleError, "stale"):
                complete_agent_run(
                    path,
                    first_claim,
                    AgentRunResult(
                        "failed", "Duplicate.", failure_category="duplicate"
                    ),
                    clock=clock,
                    policy=policy,
                )
            resumed = resume_agent_schedule(
                path, "icml", 2026, clock=clock
            )
            self.assertEqual(resumed.status, "scheduled")
            self.assertEqual(resumed.consecutive_failures, 0)

    def test_monthly_budget_defers_without_creating_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            seed(path, (("icml", 2026), ("aistats", 2026)), clock=clock)
            policy = DuePolicy(
                monthly_run_limit=1, systemic_failure_threshold=99
            )
            first = claim_due_agent_run(path, clock=clock, policy=policy)
            complete_agent_run(
                path,
                first.claim,
                AgentRunResult("success", "Fixture success."),
                clock=clock,
                policy=policy,
            )

            deferred = claim_due_agent_run(path, clock=clock, policy=policy)

            self.assertEqual(deferred.reason, "monthly_budget")
            self.assertEqual(deferred.schedule.next_check_at,
                             "2026-08-01T00:00:00Z")
            self.assertEqual(deferred.schedule.attempt_count, 0)

    def test_distinct_venue_failures_open_then_release_systemic_circuit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            seed(
                path,
                (("icml", 2026), ("aistats", 2026), ("ijcai", 2026)),
                clock=clock,
            )
            policy = DuePolicy(
                systemic_failure_threshold=2,
                max_consecutive_failures=9,
                monthly_run_limit=10,
            )
            for _ in range(2):
                claim = claim_due_agent_run(path, clock=clock, policy=policy).claim
                complete_agent_run(
                    path,
                    claim,
                    AgentRunResult(
                        "failed", "Same systemic fixture failure.",
                        failure_category="systemic_fixture",
                    ),
                    clock=clock,
                    policy=policy,
                )

            deferred = claim_due_agent_run(path, clock=clock, policy=policy)
            clock.value = NOW + timedelta(hours=24)
            recovered = claim_due_agent_run(path, clock=clock, policy=policy)

            self.assertEqual(deferred.reason, "systemic_failure")
            self.assertEqual(deferred.schedule.next_check_at,
                             "2026-07-16T14:00:00Z")
            self.assertEqual(recovered.reason, "claimed")

    def test_history_retains_machine_result_and_explanation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            seed(path, (("icml", 2026),), clock=clock)
            claim = claim_due_agent_run(path, clock=clock).claim
            complete_agent_run(
                path,
                claim,
                AgentRunResult("success", "Validated 123 papers."),
                clock=clock,
            )

            with ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE, clock=clock
            ) as repository:
                history = repository.agent_run_history("icml", 2026)
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0].disposition, "success")
                self.assertEqual(history[0].explanation,
                                 "Validated 123 papers.")

    def test_policy_module_has_no_network_process_or_environment_effect(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue({
            "google", "requests", "urllib", "subprocess", "os", "prefect",
        }.isdisjoint(imported))


if __name__ == "__main__":
    unittest.main()
