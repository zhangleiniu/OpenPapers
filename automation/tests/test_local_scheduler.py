import ast
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.control_state import (
    ControlStateError,
    ControlStateRepository,
    LeaseConflictError,
    SchedulerWakeupConflictError,
)
from automation.domain import OwnershipError, Writer
from automation.local_scheduler import (
    run_scheduler_wakeup,
    scheduler_wakeup_id,
)


FIXTURES = Path(__file__).with_name("fixtures")
MODULE = Path(__file__).resolve().parents[1] / "local_scheduler.py"
NOW = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def conference_state(*, venue="icml", year=2026, next_check_at=None):
    state = json.loads(
        (FIXTURES / "phase0" / "conference-state.v1.json").read_text(
            encoding="utf-8"
        )
    )
    state["venue_id"] = venue
    state["year"] = year
    state["next_check_at"] = next_check_at
    state["next_check_reason"] = (
        None if next_check_at is None else "unknown_schedule_fallback"
    )
    state["updated_at"] = "2026-07-14T13:00:00Z"
    return state


def seed_states(path, states, *, clock=None):
    resolved_clock = clock or MutableClock()
    with ControlStateRepository(
        path,
        writer=Writer.LOCAL_CONTROL_PLANE,
        clock=resolved_clock,
    ) as repository:
        lease = repository.acquire_lease("fixture-state-seed")
        try:
            for state in states:
                repository.store_conference_state(
                    state,
                    expected_revision=0,
                    lease=lease,
                    stored_at=resolved_clock(),
                )
        finally:
            repository.release_lease(lease)


class LocalSchedulerTests(unittest.TestCase):
    def test_due_not_due_and_missed_wakeup_use_the_fake_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            seed_states(path, (
                conference_state(
                    venue="icml",
                    next_check_at="2026-07-13T14:00:00Z",
                ),
                conference_state(
                    venue="aistats",
                    next_check_at="2026-07-15T14:00:00Z",
                ),
                conference_state(venue="ijcai", next_check_at=None),
            ))

            outcome = run_scheduler_wakeup(
                path,
                scheduled_for=NOW - timedelta(days=1),
                clock=MutableClock(),
            )

            self.assertTrue(outcome.applied)
            self.assertEqual(outcome.record.eligible_count, 1)
            self.assertEqual(outcome.record.new_selection_count, 1)
            self.assertEqual(outcome.record.duplicate_selection_count, 0)
            self.assertEqual(outcome.record.truncated_count, 0)
            self.assertEqual(
                [(item.venue_id, item.year) for item in outcome.selections],
                [("icml", 2026)],
            )

    def test_exact_replay_and_later_duplicate_wakeup_select_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            seed_states(path, (
                conference_state(next_check_at="2026-07-14T13:00:00Z"),
            ))
            clock = MutableClock()

            first = run_scheduler_wakeup(
                path, scheduled_for=NOW, clock=clock
            )
            exact = run_scheduler_wakeup(
                path,
                scheduled_for=NOW,
                clock=MutableClock(NOW + timedelta(seconds=30)),
            )
            later = run_scheduler_wakeup(
                path,
                scheduled_for=NOW + timedelta(minutes=1),
                clock=MutableClock(NOW + timedelta(minutes=1)),
            )

            self.assertTrue(first.applied)
            self.assertFalse(exact.applied)
            self.assertEqual(exact.record, first.record)
            self.assertEqual(exact.selections, first.selections)
            self.assertTrue(later.applied)
            self.assertEqual(later.record.new_selection_count, 0)
            self.assertEqual(later.record.duplicate_selection_count, 1)
            self.assertEqual(later.selections, ())
            with ControlStateRepository(
                path,
                writer=Writer.LOCAL_CONTROL_PLANE,
                clock=clock,
            ) as reopened:
                self.assertEqual(len(reopened.list_scheduler_wakeups()), 2)
                self.assertEqual(len(reopened.list_due_work_selections()), 1)

    def test_selection_limit_is_hard_and_records_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            seed_states(path, tuple(
                conference_state(
                    venue=venue,
                    next_check_at=f"2026-07-14T{hour:02d}:00:00Z",
                )
                for venue, hour in (("icml", 9), ("aistats", 10), ("ijcai", 11))
            ))

            outcome = run_scheduler_wakeup(
                path,
                scheduled_for=NOW,
                clock=MutableClock(),
                selection_limit=2,
            )

            self.assertEqual(outcome.record.eligible_count, 3)
            self.assertEqual(outcome.record.new_selection_count, 2)
            self.assertEqual(outcome.record.truncated_count, 1)
            self.assertEqual(len(outcome.selections), 2)

    def test_live_lease_and_ambiguous_restart_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            seed_states(
                path,
                (conference_state(next_check_at="2026-07-14T13:00:00Z"),),
                clock=clock,
            )
            with ControlStateRepository(
                path,
                writer=Writer.LOCAL_CONTROL_PLANE,
                clock=clock,
            ) as holder:
                lease = holder.acquire_lease("competing-local-owner")
                with self.assertRaises(LeaseConflictError):
                    run_scheduler_wakeup(
                        path, scheduled_for=NOW, clock=clock
                    )
                holder.release_lease(lease)

                lease = holder.acquire_lease("interrupted-scheduler", ttl_seconds=1)
                holder.begin_scheduler_wakeup(
                    scheduler_wakeup_id(NOW),
                    scheduled_for=NOW,
                    due_cutoff_at=NOW,
                    selection_limit=10,
                    lease=lease,
                )

            later = NOW + timedelta(seconds=2)
            with self.assertRaisesRegex(
                SchedulerWakeupConflictError, "ambiguously interrupted"
            ):
                run_scheduler_wakeup(
                    path, scheduled_for=later, clock=MutableClock(later)
                )
            with ControlStateRepository(
                path,
                writer=Writer.LOCAL_CONTROL_PLANE,
                clock=MutableClock(later),
            ) as reopened:
                wakeups = reopened.list_scheduler_wakeups()
                self.assertEqual(len(wakeups), 1)
                self.assertEqual(wakeups[0].status, "active")
                self.assertEqual(reopened.list_due_work_selections(), ())

    def test_owner_time_and_boundaries_reject_before_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cloud_path = root / "cloud.sqlite3"
            with ControlStateRepository(cloud_path):
                pass
            with self.assertRaises(OwnershipError):
                run_scheduler_wakeup(
                    cloud_path, scheduled_for=NOW, clock=MutableClock()
                )

            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                run_scheduler_wakeup(
                    root / "naive.sqlite3",
                    scheduled_for=NOW,
                    clock=MutableClock(NOW.replace(tzinfo=None)),
                )
            with self.assertRaisesRegex(ValueError, "later"):
                run_scheduler_wakeup(
                    root / "future.sqlite3",
                    scheduled_for=NOW + timedelta(seconds=1),
                    clock=MutableClock(),
                )
            with self.assertRaisesRegex(ControlStateError, "selection limit"):
                run_scheduler_wakeup(
                    root / "bound.sqlite3",
                    scheduled_for=NOW,
                    clock=MutableClock(),
                    selection_limit=0,
                )

    def test_module_has_no_orchestration_network_command_or_environment_import(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue({
            "prefect", "google", "requests", "urllib", "os", "subprocess",
        }.isdisjoint(imported))
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("getenv", source)
        self.assertNotIn("shell", source)


if __name__ == "__main__":
    unittest.main()
