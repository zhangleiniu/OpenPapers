import ast
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.control_state import (
    CONTROL_SCHEMA_VERSION,
    AgentScheduleError,
    ControlStateError,
    ControlStateRepository,
    LeaseConflictError,
    LeaseLostError,
    SchemaMigrationError,
    _MIGRATIONS,
    _REQUIRED_COLUMNS_V11,
)
from automation.contracts import artifact_fingerprint
from automation.domain import OwnershipError, Writer


MODULE = Path(__file__).resolve().parents[1] / "control_state.py"
NOW = datetime(2026, 7, 13, 20, 30, tzinfo=timezone.utc)
ACTIVE_TABLES = tuple(_REQUIRED_COLUMNS_V11)


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, *, seconds):
        self.value += timedelta(seconds=seconds)


def create_schema_10(path: Path, *, populated: bool = False) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 11):
            for statement in _MIGRATIONS[version]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (version, "2026-07-13T20:30:00Z"),
            )
        connection.execute(
            "INSERT INTO control_ownership VALUES (1, 'local_control_plane', ?)",
            ("2026-07-13T20:30:00Z",),
        )
        if populated:
            run_id = "agent-run:" + artifact_fingerprint({
                "venue_id": "colt", "year": 2011, "attempt_number": 1,
            })
            report_id = "agent-run-report:" + artifact_fingerprint({
                "run_id": run_id,
            })
            connection.execute(
                "INSERT INTO event_date_schedule VALUES "
                "('colt', 2011, 'scheduled', ?, '2011-07-09', ?, "
                "'fixture', 'fixture-model', 'v1', 1, NULL, NULL, ?)",
                ("2026-08-01T00:00:00Z",) * 3,
            )
            connection.execute(
                "INSERT INTO event_date_attempt VALUES "
                "('date-attempt-001', 'colt', 2011, 1, ?, ?, 'scheduled', "
                "'fixture', 'fixture-model', 'v1', '2011-07-09', NULL)",
                ("2026-07-13T20:00:00Z", "2026-07-13T20:01:00Z"),
            )
            connection.execute(
                "INSERT INTO agent_schedule VALUES "
                "('colt', 2011, 'completed', NULL, 1, NULL, 0, 'success', "
                "?, NULL, NULL, ?)",
                ("2026-07-13T20:02:00Z", "2026-07-13T20:02:00Z"),
            )
            connection.execute(
                "INSERT INTO agent_run_attempt VALUES "
                "(?, 'colt', 2011, 1, ?, ?, 'success', "
                "'42 papers independently validated', NULL, NULL)",
                (run_id, "2026-07-13T20:01:00Z", "2026-07-13T20:02:00Z"),
            )
            connection.execute(
                "INSERT INTO agent_execution_artifact VALUES "
                "(?, 'terminal', '/tmp/runs', '/tmp/runs/worktree', "
                "'agent/colt-2011', 'abc123', ?, ?, '{\"items\":[\"main.py\"]}', 0, 0, "
                "'retained', NULL, NULL)",
                (run_id, "2026-07-13T20:01:00Z", "2026-07-13T20:02:00Z"),
            )
            connection.execute(
                "INSERT INTO agent_run_report VALUES "
                "(?, ?, 'delivered', 'completed', "
                "NULL, 1, ?, ?, ?, NULL, 'receipt-001')",
                (report_id, run_id) + ("2026-07-13T20:02:00Z",) * 3,
            )
            connection.execute(
                "INSERT INTO agent_run_report_attempt VALUES "
                "(?, 1, ?, ?, 'delivered', NULL, 'receipt-001')",
                (report_id, "2026-07-13T20:02:00Z", "2026-07-13T20:03:00Z"),
            )
        connection.execute("PRAGMA user_version = 10")


def snapshot_active_rows(path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ACTIVE_TABLES
            if table != "schema_migrations"
        }


class SchemaAndBoundaryTests(unittest.TestCase):
    def test_empty_database_migrates_and_reopens_at_explicit_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control" / "state.sqlite3"
            with ControlStateRepository(path) as repository:
                self.assertEqual(repository.schema_version, CONTROL_SCHEMA_VERSION)
                self.assertEqual(repository._user_tables(), set(ACTIVE_TABLES))
            with ControlStateRepository(path) as reopened:
                versions = reopened._connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                self.assertEqual(
                    [row[0] for row in versions],
                    list(range(1, CONTROL_SCHEMA_VERSION + 1)),
                )

    def test_populated_schema_10_migrates_without_changing_active_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            create_schema_10(path, populated=True)
            before = snapshot_active_rows(path)
            with ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE, clock=MutableClock()
            ) as repository:
                self.assertEqual(repository.schema_version, 11)
                self.assertEqual(repository._user_tables(), set(ACTIVE_TABLES))
                self.assertEqual(repository.control_owner, Writer.LOCAL_CONTROL_PLANE)
                run_id = "agent-run:" + artifact_fingerprint({
                    "venue_id": "colt", "year": 2011, "attempt_number": 1,
                })
                self.assertEqual(repository.get_agent_run_attempt(run_id).disposition, "success")
                self.assertEqual(repository.get_agent_execution_artifact(run_id).changed_files, ("main.py",))
                self.assertEqual(repository.get_agent_run_report(run_id).status, "delivered")
            self.assertEqual(snapshot_active_rows(path), before)

    def test_malformed_or_ambiguous_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "malformed.sqlite3"
            create_schema_10(malformed)
            with sqlite3.connect(malformed) as connection:
                connection.execute("DROP TABLE agent_run_report_attempt")
            before = malformed.read_bytes()
            with self.assertRaisesRegex(SchemaMigrationError, "missing tables"):
                ControlStateRepository(
                    malformed, writer=Writer.LOCAL_CONTROL_PLANE
                )
            self.assertEqual(malformed.read_bytes(), before)

            ambiguous = Path(directory) / "ambiguous.sqlite3"
            with ControlStateRepository(ambiguous):
                pass
            with sqlite3.connect(ambiguous) as connection:
                connection.execute("CREATE TABLE retired_state (value TEXT)")
            with self.assertRaisesRegex(SchemaMigrationError, "unexpected tables"):
                ControlStateRepository(ambiguous)

    def test_unrecognized_future_and_wrong_owner_databases_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            populated = Path(directory) / "unversioned.sqlite3"
            with sqlite3.connect(populated) as connection:
                connection.execute("CREATE TABLE source_state (venue TEXT)")
            with self.assertRaisesRegex(SchemaMigrationError, "populated unversioned"):
                ControlStateRepository(populated)

            future = Path(directory) / "future.sqlite3"
            with sqlite3.connect(future) as connection:
                connection.execute(f"PRAGMA user_version = {CONTROL_SCHEMA_VERSION + 1}")
            with self.assertRaisesRegex(SchemaMigrationError, "newer"):
                ControlStateRepository(future)

            local = Path(directory) / "local.sqlite3"
            with ControlStateRepository(local, writer=Writer.LOCAL_CONTROL_PLANE):
                pass
            with self.assertRaisesRegex(OwnershipError, "owned by"):
                ControlStateRepository(local, writer=Writer.CLOUD_CONTROL_PLANE)
            with self.assertRaises(OwnershipError):
                ControlStateRepository(
                    Path(directory) / "worker.sqlite3", writer=Writer.MAC_WORKER
                )

    def test_module_has_no_network_or_retired_domain_import(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        roots = {name.split(".", 1)[0] for name in imports}
        self.assertTrue({"requests", "urllib3", "prefect", "google"}.isdisjoint(roots))
        self.assertTrue({
            "automation.cases", "automation.job_queue", "automation.lifecycle",
            "automation.reminders", "automation.verification",
        }.isdisjoint(imports))


class LeaseTests(unittest.TestCase):
    def test_lease_excludes_overlap_and_expired_token_cannot_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            first = ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE, clock=clock
            )
            second = ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE, clock=clock
            )
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            lease = first.acquire_lease("flow-one", ttl_seconds=60)
            clock.advance(seconds=30)
            with self.assertRaisesRegex(LeaseConflictError, "flow-one"):
                second.acquire_lease("flow-two", ttl_seconds=60)
            clock.advance(seconds=30)
            replacement = second.acquire_lease("flow-two", ttl_seconds=60)
            clock.advance(seconds=1)
            with self.assertRaises(LeaseLostError):
                first.register_event_date_target(
                    "colt", 2011, registered_at=clock(), lease=lease
                )
            outcome = second.register_event_date_target(
                "colt", 2011, registered_at=clock(), lease=replacement
            )
            self.assertTrue(outcome.applied)

    def test_renew_release_and_invalid_time_or_ttl_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=clock
            ) as repository:
                lease = repository.acquire_lease("flow-one", ttl_seconds=30)
                clock.advance(seconds=20)
                renewed = repository.renew_lease(lease, ttl_seconds=60)
                self.assertEqual(renewed.expires_at, "2026-07-13T20:31:20Z")
                repository.release_lease(renewed)
                with self.assertRaises(LeaseLostError):
                    repository.release_lease(renewed)
                clock.value = datetime(2026, 7, 13, 20, 31)
                with self.assertRaisesRegex(ControlStateError, "timezone"):
                    repository.acquire_lease("flow-two")


class EnsureScheduledAgentTargetTests(unittest.TestCase):
    def test_requires_a_matching_event_date_schedule_row(self):
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3",
                writer=Writer.LOCAL_CONTROL_PLANE, clock=MutableClock(),
            ) as repository:
                lease = repository.acquire_lease("fixture")
                with self.assertRaises(ControlStateError):
                    repository.ensure_scheduled_agent_target(
                        "icml", 2099,
                        next_check_at=NOW + timedelta(days=1),
                        registered_at=NOW, lease=lease,
                    )

    def test_idempotent_and_never_overwrites_an_existing_row(self):
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3",
                writer=Writer.LOCAL_CONTROL_PLANE, clock=MutableClock(),
            ) as repository:
                lease = repository.acquire_lease("fixture")
                repository.register_event_date_target(
                    "icml", 2099, registered_at=NOW, lease=lease
                )

                first = repository.ensure_scheduled_agent_target(
                    "icml", 2099,
                    next_check_at=NOW + timedelta(days=1),
                    registered_at=NOW, lease=lease,
                )
                self.assertEqual(first.status, "scheduled")
                self.assertEqual(first.next_check_at, "2026-07-14T20:30:00Z")

                second = repository.ensure_scheduled_agent_target(
                    "icml", 2099,
                    next_check_at=NOW + timedelta(days=30),
                    registered_at=NOW, lease=lease,
                )
                # ON CONFLICT DO NOTHING: a second call never clobbers.
                self.assertEqual(second.next_check_at, first.next_check_at)


class ContinuousEventDateTests(unittest.TestCase):
    def test_placeholder_satisfies_the_agent_schedule_foreign_key(self):
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3",
                writer=Writer.LOCAL_CONTROL_PLANE, clock=MutableClock(),
            ) as repository:
                lease = repository.acquire_lease("fixture")
                outcome = repository.register_continuous_event_date(
                    "jmlr", 2026, registered_at=NOW, lease=lease
                )
                self.assertTrue(outcome.applied)
                self.assertEqual(outcome.record.status, "scheduled")
                self.assertEqual(outcome.record.provider_name, "continuous_lifecycle")

                # The FK this exists for: agent_schedule now accepts a row.
                agent = repository.ensure_scheduled_agent_target(
                    "jmlr", 2026,
                    next_check_at=NOW, registered_at=NOW, lease=lease,
                )
                self.assertEqual(agent.status, "scheduled")

    def test_idempotent_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3",
                writer=Writer.LOCAL_CONTROL_PLANE, clock=MutableClock(),
            ) as repository:
                lease = repository.acquire_lease("fixture")
                first = repository.register_continuous_event_date(
                    "jmlr", 2026, registered_at=NOW, lease=lease
                )
                second = repository.register_continuous_event_date(
                    "jmlr", 2026,
                    registered_at=NOW + timedelta(days=1), lease=lease,
                )
                self.assertTrue(first.applied)
                self.assertFalse(second.applied)
                self.assertEqual(
                    first.record.estimated_at, second.record.estimated_at
                )


class RecurringCompletionGuardTests(unittest.TestCase):
    def test_recurring_is_only_valid_for_a_success_disposition(self):
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3",
                writer=Writer.LOCAL_CONTROL_PLANE, clock=MutableClock(),
            ) as repository:
                lease = repository.acquire_lease("fixture")
                repository.register_continuous_event_date(
                    "jmlr", 2026, registered_at=NOW, lease=lease
                )
                repository.ensure_scheduled_agent_target(
                    "jmlr", 2026, next_check_at=NOW, registered_at=NOW,
                    lease=lease,
                )
                claimed = repository.claim_due_agent_run(
                    claimed_at=NOW, monthly_run_limit=10,
                    systemic_failure_threshold=3,
                    systemic_failure_window=timedelta(hours=24),
                    systemic_circuit_delay=timedelta(hours=24),
                    lease=lease,
                )

                with self.assertRaisesRegex(AgentScheduleError, "recurring only"):
                    repository.complete_agent_run_attempt(
                        claimed.claim,
                        disposition="not_ready",
                        explanation="fixture",
                        completed_at=NOW,
                        next_check_at=NOW + timedelta(days=1),
                        suggested_retry_at=None,
                        failure_category=None,
                        pause_after_failure=False,
                        lease=lease,
                        recurring=True,
                    )


if __name__ == "__main__":
    unittest.main()
