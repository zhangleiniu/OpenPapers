import ast
import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.control_state import (
    CONTROL_SCHEMA_VERSION,
    ControlStateError,
    ControlStateRepository,
    LeaseHandle,
    LeaseConflictError,
    LeaseLostError,
    SchemaMigrationError,
    StateRevisionConflictError,
    StoredDataError,
    VerificationReplayConflictError,
)
from automation.domain import OwnershipError, Writer
from automation.verification import VerificationError, build_verification_result


FIXTURES = Path(__file__).with_name("fixtures")
MODULE = Path(__file__).resolve().parents[1] / "control_state.py"
NOW = datetime(2026, 7, 13, 20, 30, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, *, seconds):
        self.value += timedelta(seconds=seconds)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verification_bundle():
    discovery = load_json(FIXTURES / "phase0" / "discovery-result.v1.json")
    request = load_json(FIXTURES / "phase2" / "verification-request.v2.json")
    result = load_json(FIXTURES / "phase2" / "verification-result.v2.json")
    return discovery, request, result


def conference_state():
    return load_json(FIXTURES / "phase0" / "conference-state.v1.json")


class SchemaAndBoundaryTests(unittest.TestCase):
    def test_empty_database_migrates_and_reopens_at_explicit_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control" / "state.sqlite3"
            with ControlStateRepository(path) as repository:
                self.assertEqual(repository.schema_version, CONTROL_SCHEMA_VERSION)
                versions = repository._connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
                self.assertEqual([row[0] for row in versions], [1])

            with ControlStateRepository(path) as reopened:
                self.assertEqual(reopened.schema_version, 1)
                self.assertEqual(reopened.replay_verifications(), ())

    def test_unrecognized_and_future_databases_fail_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            populated = Path(directory) / "monitor.sqlite3"
            connection = sqlite3.connect(populated)
            connection.execute("CREATE TABLE source_state (venue TEXT)")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SchemaMigrationError, "populated unversioned"):
                ControlStateRepository(populated)
            connection = sqlite3.connect(populated)
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            connection.close()
            self.assertEqual(tables, {"source_state"})

            future = Path(directory) / "future.sqlite3"
            connection = sqlite3.connect(future)
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SchemaMigrationError, "newer"):
                ControlStateRepository(future)

            malformed = Path(directory) / "malformed.sqlite3"
            connection = sqlite3.connect(malformed)
            connection.execute(
                "CREATE TABLE schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_migrations VALUES (1, ?)",
                ("2026-07-13T20:30:00Z",),
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SchemaMigrationError, "missing tables"):
                ControlStateRepository(malformed)

    def test_only_cloud_control_plane_can_construct_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self.assertRaises(OwnershipError):
                ControlStateRepository(path, writer=Writer.MAC_WORKER)
            self.assertFalse(path.exists())

    def test_module_has_no_reducer_router_network_or_orchestration_import(self):
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
        self.assertTrue(
            {"requests", "urllib3", "prefect", "google"}.isdisjoint(roots)
        )
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        self.assertTrue(
            {"apply_transition", "ActionType", "compute_next_check"}.isdisjoint(
                imported_names
            )
        )


class LeaseTests(unittest.TestCase):
    def test_lease_excludes_overlap_and_expired_token_cannot_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            first = ControlStateRepository(path, clock=clock)
            second = ControlStateRepository(path, clock=clock)
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
                first.store_conference_state(
                    conference_state(),
                    expected_revision=0,
                    lease=lease,
                    stored_at=NOW + timedelta(seconds=61),
                )
            outcome = second.store_conference_state(
                conference_state(),
                expected_revision=0,
                lease=replacement,
                stored_at=NOW + timedelta(seconds=61),
            )
            self.assertTrue(outcome.applied)

    def test_renew_release_and_invalid_time_or_ttl_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=clock
            ) as store:
                lease = store.acquire_lease("flow-one", ttl_seconds=30)
                clock.advance(seconds=20)
                renewed = store.renew_lease(lease, ttl_seconds=60)
                self.assertEqual(renewed.token, lease.token)
                self.assertEqual(renewed.expires_at, "2026-07-13T20:31:20Z")
                store.release_lease(renewed)
                with self.assertRaises(LeaseLostError):
                    store.release_lease(renewed)
                clock.advance(seconds=1)
                replacement = store.acquire_lease("flow-two")
                self.assertNotEqual(replacement.token, lease.token)
                clock.value = datetime(2026, 7, 13, 20, 31)
                with self.assertRaisesRegex(ControlStateError, "timezone"):
                    store.renew_lease(replacement)
                clock.value = NOW
                with self.assertRaisesRegex(ControlStateError, "between"):
                    store.renew_lease(replacement, ttl_seconds=0)


class VerificationHistoryTests(unittest.TestCase):
    def test_bundle_survives_reopen_and_timestamp_replay_preserves_first(self):
        discovery, request, result = verification_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with ControlStateRepository(path, clock=MutableClock()) as store:
                lease = store.acquire_lease("flow-one")
                self.assertTrue(store.accept_verification(
                    discovery,
                    request,
                    result,
                    lease=lease,
                    received_at=NOW,
                ))
                replay = deepcopy(result)
                replay["verified_at"] = "2026-07-14T17:31:00Z"
                self.assertFalse(store.accept_verification(
                    discovery,
                    request,
                    replay,
                    lease=lease,
                    received_at=NOW + timedelta(seconds=1),
                ))

            with ControlStateRepository(path) as reopened:
                records = reopened.replay_verifications()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].sequence, 1)
                self.assertEqual(records[0].received_at, "2026-07-13T20:30:00Z")
                self.assertEqual(records[0].result, result)
                records[0].result["uncertainties"].append("caller mutation")
                self.assertEqual(
                    reopened.replay_verifications()[0].result["uncertainties"], []
                )

    def test_invalid_bundle_rolls_back_and_conflicting_identity_is_rejected(self):
        discovery, request, result = verification_bundle()
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("flow-one")
                invalid = deepcopy(result)
                invalid["overall_status"] = "rejected"
                with self.assertRaises(VerificationError):
                    store.accept_verification(
                        discovery,
                        request,
                        invalid,
                        lease=lease,
                        received_at=NOW,
                    )
                self.assertEqual(store.replay_verifications(), ())
                with self.assertRaises(LeaseLostError):
                    store.accept_verification(
                        discovery,
                        request,
                        result,
                        lease=LeaseHandle("missing", "not-current", "2099-01-01T00:00:00Z"),
                        received_at=NOW,
                    )
                self.assertEqual(store.replay_verifications(), ())

                self.assertTrue(store.accept_verification(
                    discovery,
                    request,
                    result,
                    lease=lease,
                    received_at=NOW,
                ))
                store._connection.execute(
                    "UPDATE verification_history SET evidence_fingerprint = ?",
                    ("f" * 64,),
                )
                with self.assertRaises(VerificationReplayConflictError):
                    store.accept_verification(
                        discovery,
                        request,
                        result,
                        lease=lease,
                        received_at=NOW + timedelta(seconds=1),
                    )

    def test_replay_is_ordered_filterable_and_revalidates_stored_artifacts(self):
        discovery, request, result = verification_bundle()
        review = build_verification_result(
            request,
            discovery,
            overall_status="review_required",
            verified_at="2026-07-13T17:32:00Z",
            uncertainties=["Fixture requires review."],
        )
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("flow-one")
                for offset, item in enumerate((result, review)):
                    store.accept_verification(
                        discovery,
                        request,
                        item,
                        lease=lease,
                        received_at=NOW + timedelta(seconds=offset),
                    )
                records = store.replay_verifications(venue_id="icml", year=2026)
                self.assertEqual([item.sequence for item in records], [1, 2])
                self.assertEqual(
                    [item.result["verification_id"] for item in records],
                    [result["verification_id"], review["verification_id"]],
                )
                with self.assertRaisesRegex(ControlStateError, "venue and year"):
                    store.replay_verifications(venue_id="icml")
                store._connection.execute(
                    "UPDATE verification_history SET result_fingerprint = ? "
                    "WHERE sequence = 2",
                    ("0" * 64,),
                )
                with self.assertRaisesRegex(StoredDataError, "fingerprint"):
                    store.replay_verifications()


class ConferenceStateTests(unittest.TestCase):
    def test_state_revisions_are_optimistic_immutable_and_replayable(self):
        initial = conference_state()
        updated = deepcopy(initial)
        updated["facets"]["paper_list_status"] = "released"
        updated["updated_at"] = "2026-07-13T20:31:00Z"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with ControlStateRepository(path, clock=MutableClock()) as store:
                lease = store.acquire_lease("flow-one")
                first = store.store_conference_state(
                    initial,
                    expected_revision=0,
                    lease=lease,
                    stored_at=NOW,
                )
                duplicate = store.store_conference_state(
                    initial,
                    expected_revision=0,
                    lease=lease,
                    stored_at=NOW + timedelta(seconds=1),
                )
                second = store.store_conference_state(
                    updated,
                    expected_revision=1,
                    lease=lease,
                    stored_at=NOW + timedelta(seconds=2),
                )
                self.assertTrue(first.applied)
                self.assertFalse(duplicate.applied)
                self.assertTrue(second.applied)
                self.assertEqual(second.record.revision, 2)
                with self.assertRaises(StateRevisionConflictError):
                    store.store_conference_state(
                        initial,
                        expected_revision=1,
                        lease=lease,
                        stored_at=NOW + timedelta(seconds=3),
                    )

            with ControlStateRepository(path) as reopened:
                current = reopened.get_conference_state("icml", 2026)
                self.assertEqual(current.revision, 2)
                self.assertEqual(current.state, updated)
                history = reopened.conference_state_history("icml", 2026)
                self.assertEqual([item.revision for item in history], [1, 2])
                self.assertEqual(history[0].state, initial)

    def test_compound_state_write_rolls_back_and_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("flow-one")
                store._connection.execute("""
                    CREATE TRIGGER reject_current_insert
                    BEFORE INSERT ON conference_state_current
                    BEGIN
                        SELECT RAISE(ABORT, 'forced rollback');
                    END
                """)
                with self.assertRaisesRegex(ControlStateError, "transaction failed"):
                    store.store_conference_state(
                        conference_state(),
                        expected_revision=0,
                        lease=lease,
                        stored_at=NOW,
                    )
                count = store._connection.execute(
                    "SELECT COUNT(*) FROM conference_state_history"
                ).fetchone()[0]
                self.assertEqual(count, 0)
                store._connection.execute("DROP TRIGGER reject_current_insert")
                store.store_conference_state(
                    conference_state(),
                    expected_revision=0,
                    lease=lease,
                    stored_at=NOW,
                )
                store._connection.execute(
                    "UPDATE conference_state_current SET state_fingerprint = ?",
                    ("0" * 64,),
                )
                with self.assertRaisesRegex(StoredDataError, "fingerprint"):
                    store.get_conference_state("icml", 2026)


if __name__ == "__main__":
    unittest.main()
