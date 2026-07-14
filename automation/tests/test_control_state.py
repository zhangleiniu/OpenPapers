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
    ExecutionQueueError,
    LeaseHandle,
    LeaseConflictError,
    LeaseLostError,
    JobResultConsumptionConflictError,
    SchemaMigrationError,
    StateRevisionConflictError,
    StoredDataError,
    VerificationReplayConflictError,
    _MIGRATION_1,
    _MIGRATION_2,
    _MIGRATION_3,
    _MIGRATION_4,
    _MIGRATION_5,
)
from automation.cases import CaseObservation, case_event_payload, observe_case
from automation.contracts import artifact_fingerprint
from automation.domain import ActionType, OwnershipError, Writer
from automation.lifecycle import ActionIntent, QueueExistingScraperPayload
from automation.verification import VerificationError, build_verification_result


FIXTURES = Path(__file__).with_name("fixtures")
PHASE4_FIXTURES = FIXTURES / "phase4"
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


def job_result_bundle():
    return tuple(
        load_json(PHASE4_FIXTURES / name)
        for name in (
            "scrape-job.v2.json",
            "job-manifest.v1.json",
            "job-result.v2.json",
        )
    )


def scraper_action(
    result,
    *,
    action_id="action:" + "f" * 32,
    readiness="pdf_ready",
    extra_evidence=("source:icml:pdf-test", "snapshot:icml:pdf-test"),
):
    return ActionIntent(
        action_id=action_id,
        action_type=ActionType.QUEUE_EXISTING_SCRAPER,
        venue_id=result["venue_id"],
        year=result["year"],
        evidence_ids=(result["verification_id"], *extra_evidence),
        payload=QueueExistingScraperPayload(
            readiness=readiness,
            scraper_module="scrapers.icml",
            scraper_class="ICMLScraper",
        ),
    )


def canonical_json(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class SchemaAndBoundaryTests(unittest.TestCase):
    def test_empty_database_migrates_and_reopens_at_explicit_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control" / "state.sqlite3"
            with ControlStateRepository(path) as repository:
                self.assertEqual(repository.schema_version, CONTROL_SCHEMA_VERSION)
                versions = repository._connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
                self.assertEqual(
                    [row[0] for row in versions], [1, 2, 3, 4, 5, 6, 7]
                )

            with ControlStateRepository(path) as reopened:
                self.assertEqual(reopened.schema_version, CONTROL_SCHEMA_VERSION)
                self.assertEqual(reopened.replay_verifications(), ())

    def test_valid_version_one_database_migrates_without_losing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            for statement in _MIGRATION_1:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
                ("2026-07-13T20:30:00Z",),
            )
            state = conference_state()
            state_json = canonical_json(state)
            values = (
                state["venue_id"],
                state["year"],
                1,
                artifact_fingerprint(state),
                "2026-07-13T20:30:00Z",
                state_json,
            )
            connection.execute(
                "INSERT INTO conference_state_history VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )
            connection.execute(
                "INSERT INTO conference_state_current VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
            connection.close()

            with ControlStateRepository(path, clock=MutableClock()) as repository:
                self.assertEqual(repository.schema_version, 7)
                self.assertEqual(
                    repository.get_conference_state("icml", 2026).state, state
                )
                self.assertEqual(repository.list_cases(include_closed=True), ())
                versions = repository._connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                self.assertEqual(
                    [row[0] for row in versions], [1, 2, 3, 4, 5, 6, 7]
                )

    def test_valid_version_two_database_migrates_without_losing_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            for statement in (*_MIGRATION_1, *_MIGRATION_2):
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (
                    (1, "2026-07-13T20:30:00Z"),
                    (2, "2026-07-13T21:30:00Z"),
                ),
            )
            observation = CaseObservation(
                event_id="case-event:icml:2026:no-pdf:1",
                venue_id="icml",
                year=2026,
                blocker="no_pdf",
                summary="The public list exists but archival PDFs are not available.",
                evidence_ids=("evidence:icml:2026:list",),
                observed_at="2026-07-13T13:00:00Z",
            )
            event = case_event_payload(observation)
            state = observe_case(None, observation).state
            state_values = (
                state["case_id"],
                state["venue_id"],
                state["year"],
                state["blocker"],
                state["status"],
                1,
                artifact_fingerprint(state),
                "2026-07-13T21:30:00Z",
                canonical_json(state),
            )
            connection.execute(
                "INSERT INTO case_state_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                state_values,
            )
            connection.execute(
                "INSERT INTO case_state_current VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                state_values,
            )
            connection.execute(
                """
                INSERT INTO case_event_history (
                    event_id, case_id, event_kind, event_at, event_fingerprint,
                    previous_revision, resulting_revision, revision_applied,
                    meaningful_change, reactivated, event_json
                ) VALUES (?, ?, ?, ?, ?, 0, 1, 1, 1, 0, ?)
                """,
                (
                    event["event_id"],
                    event["case_id"],
                    event["event_kind"],
                    event["at"],
                    artifact_fingerprint(event),
                    canonical_json(event),
                ),
            )
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
            connection.close()

            with ControlStateRepository(path, clock=MutableClock()) as repository:
                self.assertEqual(repository.schema_version, 7)
                self.assertEqual(repository.get_case(state["case_id"]).state, state)
                self.assertEqual(repository.get_notification("missing"), None)
                versions = repository._connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                self.assertEqual(
                    [row[0] for row in versions], [1, 2, 3, 4, 5, 6, 7]
                )

    def test_valid_version_three_database_migrates_additive_result_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            for statement in (*_MIGRATION_1, *_MIGRATION_2, *_MIGRATION_3):
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (
                    (1, "2026-07-13T20:30:00Z"),
                    (2, "2026-07-13T21:30:00Z"),
                    (3, "2026-07-13T22:30:00Z"),
                ),
            )
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
            connection.close()

            with ControlStateRepository(path, clock=MutableClock()) as repository:
                self.assertEqual(repository.schema_version, 7)
                self.assertEqual(repository.replay_job_result_consumptions(), ())
                versions = repository._connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                self.assertEqual(
                    [row[0] for row in versions], [1, 2, 3, 4, 5, 6, 7]
                )

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
            connection.execute(
                f"PRAGMA user_version = {CONTROL_SCHEMA_VERSION + 1}"
            )
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
            connection = sqlite3.connect(malformed)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            connection.close()
            self.assertEqual(tables, {"schema_migrations"})

    def test_control_plane_roles_are_distinct_from_mac_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self.assertRaises(OwnershipError):
                ControlStateRepository(path, writer=Writer.MAC_WORKER)
            self.assertFalse(path.exists())

    def test_local_owner_is_persisted_and_mismatched_reopen_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.sqlite3"
            with ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE, clock=MutableClock()
            ) as repository:
                self.assertEqual(
                    repository.control_owner, Writer.LOCAL_CONTROL_PLANE
                )
            with self.assertRaisesRegex(OwnershipError, "owned by"):
                ControlStateRepository(path, writer=Writer.CLOUD_CONTROL_PLANE)
            with ControlStateRepository(
                path, writer=Writer.LOCAL_CONTROL_PLANE, clock=MutableClock()
            ) as reopened:
                self.assertEqual(reopened.schema_version, 7)

    def test_valid_version_five_local_database_migrates_additive_plan_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local-v5.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            for statement in (
                *_MIGRATION_1,
                *_MIGRATION_2,
                *_MIGRATION_3,
                *_MIGRATION_4,
                *_MIGRATION_5,
            ):
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                tuple(
                    (version, f"2026-07-13T20:3{version}:00Z")
                    for version in range(1, 6)
                ),
            )
            connection.execute(
                "INSERT INTO control_ownership VALUES (1, ?, ?)",
                ("local_control_plane", "2026-07-13T20:35:00Z"),
            )
            connection.execute("PRAGMA user_version = 5")
            connection.commit()
            connection.close()

            with ControlStateRepository(
                path,
                writer=Writer.LOCAL_CONTROL_PLANE,
                clock=MutableClock(),
            ) as repository:
                self.assertEqual(repository.schema_version, 7)
                self.assertEqual(
                    repository.control_owner, Writer.LOCAL_CONTROL_PLANE
                )
                columns = {
                    row[1]
                    for row in repository._connection.execute(
                        "PRAGMA table_info(scheduler_wakeup_plan)"
                    )
                }
                self.assertIn("planned_at", columns)

    def test_legacy_database_cannot_be_claimed_local_or_lose_cloud_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-v4.sqlite3"
            connection = sqlite3.connect(path)
            for statement in (
                *_MIGRATION_1, *_MIGRATION_2, *_MIGRATION_3, *_MIGRATION_4
            ):
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                tuple(
                    (version, f"2026-07-13T20:3{version}:00Z")
                    for version in range(1, 5)
                ),
            )
            connection.execute("PRAGMA user_version = 4")
            connection.commit()
            connection.close()

            before = path.read_bytes()
            with self.assertRaisesRegex(OwnershipError, "cloud-owned"):
                ControlStateRepository(path, writer=Writer.LOCAL_CONTROL_PLANE)
            self.assertEqual(path.read_bytes(), before)
            with ControlStateRepository(path) as cloud:
                self.assertEqual(cloud.control_owner, Writer.CLOUD_CONTROL_PLANE)

    def test_missing_ownership_row_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with ControlStateRepository(path):
                pass
            connection = sqlite3.connect(path)
            connection.execute("DELETE FROM control_ownership")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SchemaMigrationError, "ownership"):
                ControlStateRepository(path)

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


class JobResultConsumptionTests(unittest.TestCase):
    def consume(self, store, lease, *, generations=(7, 11), consumed_at=NOW):
        job, manifest, result = job_result_bundle()
        return store.consume_job_result(
            job,
            manifest,
            result,
            manifest_name=f"manifests/{job['job_id']}.json",
            manifest_generation=generations[0],
            result_name=f"job-results/{job['job_id']}.json",
            result_generation=generations[1],
            lease=lease,
            consumed_at=consumed_at,
        )

    def test_exact_replay_is_one_logical_consumption_and_survives_reopen(self):
        job, _, _ = job_result_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with ControlStateRepository(path, clock=MutableClock()) as store:
                lease = store.acquire_lease("result-consumer")
                first = self.consume(store, lease)
                replay = self.consume(
                    store, lease, consumed_at=NOW + timedelta(seconds=30)
                )
                self.assertTrue(first.applied)
                self.assertFalse(replay.applied)
                self.assertEqual(first.record, replay.record)
                self.assertEqual(first.record.consumed_at, "2026-07-13T20:30:00Z")

            with ControlStateRepository(path) as reopened:
                records = reopened.replay_job_result_consumptions()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].job_id, job["job_id"])
                self.assertEqual(
                    reopened.get_job_result_consumption(job["job_id"]), records[0]
                )
                records[0].manifest["artifacts"].clear()
                self.assertEqual(
                    len(reopened.get_job_result_consumption(
                        job["job_id"]
                    ).manifest["artifacts"]),
                    1,
                )

    def test_lease_generation_name_and_conflicting_replay_fail_closed(self):
        job, manifest, result = job_result_bundle()
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=clock
            ) as store:
                lease = store.acquire_lease("result-consumer", ttl_seconds=10)
                with self.assertRaisesRegex(ControlStateError, "manifest object"):
                    store.consume_job_result(
                        job,
                        manifest,
                        result,
                        manifest_name="manifests/wrong.json",
                        manifest_generation=1,
                        result_name=f"job-results/{job['job_id']}.json",
                        result_generation=1,
                        lease=lease,
                        consumed_at=NOW,
                    )
                with self.assertRaisesRegex(ControlStateError, "positive"):
                    self.consume(store, lease, generations=(0, 1))
                self.assertTrue(self.consume(store, lease).applied)
                with self.assertRaises(JobResultConsumptionConflictError):
                    self.consume(store, lease, generations=(7, 12))
                clock.advance(seconds=10)
                with self.assertRaises(LeaseLostError):
                    self.consume(store, lease)
                self.assertEqual(len(store.replay_job_result_consumptions()), 1)

    def test_stored_corruption_is_rejected_on_get_and_replay(self):
        job, _, _ = job_result_bundle()
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("result-consumer")
                self.consume(store, lease)
                store._connection.execute(
                    "UPDATE job_result_consumption "
                    "SET result_payload_fingerprint = ? WHERE job_id = ?",
                    ("f" * 64, job["job_id"]),
                )
                with self.assertRaisesRegex(StoredDataError, "fingerprint"):
                    store.get_job_result_consumption(job["job_id"])
                with self.assertRaisesRegex(StoredDataError, "fingerprint"):
                    store.replay_job_result_consumptions()


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


class ExecutionJobTests(unittest.TestCase):
    def _local_repository(self, path, *, clock=None):
        return ControlStateRepository(
            path, writer=Writer.LOCAL_CONTROL_PLANE, clock=clock or MutableClock()
        )

    def test_retain_is_idempotent_and_conflicting_replay_fails_closed(self):
        _, _, result = verification_bundle()
        action = scraper_action(result)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self._local_repository(path) as repo:
                lease = repo.acquire_lease("dispatch-owner")
                repo.accept_verification(
                    *verification_bundle(), lease=lease, received_at=NOW
                )
                first = repo.retain_existing_scraper_action(
                    action,
                    source_verification_id=result["verification_id"],
                    lease=lease,
                    enqueued_at=NOW,
                )
                self.assertTrue(first.applied)
                self.assertEqual(first.record.state, "pending")
                self.assertEqual(first.record.current_attempt_number, 0)
                self.assertEqual(first.record.action_id, action.action_id)

                replay = repo.retain_existing_scraper_action(
                    action,
                    source_verification_id=result["verification_id"],
                    lease=lease,
                    enqueued_at=NOW,
                )
                self.assertFalse(replay.applied)
                self.assertEqual(replay.record.job_id, first.record.job_id)

                conflicting = scraper_action(
                    result,
                    action_id=action.action_id,
                    extra_evidence=("source:icml:pdf-other", "snapshot:icml:pdf-other"),
                )
                with self.assertRaisesRegex(ExecutionQueueError, "different job"):
                    repo.retain_existing_scraper_action(
                        conflicting,
                        source_verification_id=result["verification_id"],
                        lease=lease,
                        enqueued_at=NOW,
                    )

            with self._local_repository(path) as reopened:
                stored = reopened.get_execution_job(first.record.job_id)
                self.assertEqual(stored.action_id, action.action_id)
                self.assertEqual(stored.job, first.record.job)

    def test_retain_rejects_unknown_verification_and_venue_year_mismatch(self):
        _, _, result = verification_bundle()
        action = scraper_action(result)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self._local_repository(path) as repo:
                lease = repo.acquire_lease("dispatch-owner")
                with self.assertRaisesRegex(ExecutionQueueError, "not retained"):
                    repo.retain_existing_scraper_action(
                        action,
                        source_verification_id=result["verification_id"],
                        lease=lease,
                        enqueued_at=NOW,
                    )
                repo.accept_verification(
                    *verification_bundle(), lease=lease, received_at=NOW
                )
                unrelated_action = scraper_action(
                    result, action_id="action:" + "a" * 32
                )
                with self.assertRaisesRegex(ExecutionQueueError, "does not cite"):
                    repo.retain_existing_scraper_action(
                        unrelated_action,
                        source_verification_id="verification:" + "b" * 32,
                        lease=lease,
                        enqueued_at=NOW,
                    )
                mismatched_action = ActionIntent(
                    action_id="action:" + "c" * 32,
                    action_type=ActionType.QUEUE_EXISTING_SCRAPER,
                    venue_id="aistats",
                    year=result["year"],
                    evidence_ids=(result["verification_id"], "source:x", "snapshot:x"),
                    payload=QueueExistingScraperPayload(
                        readiness="pdf_ready",
                        scraper_module="scrapers.aistats",
                        scraper_class="AISTATSScraper",
                    ),
                )
                with self.assertRaisesRegex(ExecutionQueueError, "venue/year"):
                    repo.retain_existing_scraper_action(
                        mismatched_action,
                        source_verification_id=result["verification_id"],
                        lease=lease,
                        enqueued_at=NOW,
                    )

    def test_retain_rejects_non_scraper_and_non_pdf_ready_actions(self):
        _, _, result = verification_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self._local_repository(path) as repo:
                lease = repo.acquire_lease("dispatch-owner")
                repo.accept_verification(
                    *verification_bundle(), lease=lease, received_at=NOW
                )
                not_ready = scraper_action(result, readiness="pdf_partial")
                with self.assertRaisesRegex(ExecutionQueueError, "pdf_ready"):
                    repo.retain_existing_scraper_action(
                        not_ready,
                        source_verification_id=result["verification_id"],
                        lease=lease,
                        enqueued_at=NOW,
                    )

    def test_retain_requires_local_ownership(self):
        _, _, result = verification_bundle()
        action = scraper_action(result)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with ControlStateRepository(path, clock=MutableClock()) as repo:
                lease = repo.acquire_lease("cloud-owner")
                repo.accept_verification(
                    *verification_bundle(), lease=lease, received_at=NOW
                )
                with self.assertRaises(OwnershipError):
                    repo.retain_existing_scraper_action(
                        action,
                        source_verification_id=result["verification_id"],
                        lease=lease,
                        enqueued_at=NOW,
                    )

    def test_claim_selects_oldest_pending_and_returns_none_when_empty(self):
        _, _, result = verification_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self._local_repository(path) as repo:
                lease = repo.acquire_lease("dispatch-owner")
                self.assertIsNone(
                    repo.claim_next_execution_job(lease=lease, claimed_at=NOW)
                )
                repo.accept_verification(
                    *verification_bundle(), lease=lease, received_at=NOW
                )
                later = scraper_action(result, action_id="action:" + "1" * 32)
                earlier = scraper_action(result, action_id="action:" + "2" * 32)
                repo.retain_existing_scraper_action(
                    later,
                    source_verification_id=result["verification_id"],
                    lease=lease,
                    enqueued_at=NOW + timedelta(seconds=10),
                )
                repo.retain_existing_scraper_action(
                    earlier,
                    source_verification_id=result["verification_id"],
                    lease=lease,
                    enqueued_at=NOW,
                )
                claim = repo.claim_next_execution_job(lease=lease, claimed_at=NOW)
                stored = repo.get_execution_job(claim.job_id)
                self.assertEqual(stored.action_id, earlier.action_id)
                self.assertEqual(stored.state, "in_flight")
                self.assertEqual(claim.attempt_number, 1)

    def test_complete_retry_then_completed_and_rejects_stale_or_mismatched_claims(self):
        _, _, result = verification_bundle()
        action = scraper_action(result)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self._local_repository(path) as repo:
                lease = repo.acquire_lease("dispatch-owner")
                repo.accept_verification(
                    *verification_bundle(), lease=lease, received_at=NOW
                )
                repo.retain_existing_scraper_action(
                    action,
                    source_verification_id=result["verification_id"],
                    lease=lease,
                    enqueued_at=NOW,
                )
                claim = repo.claim_next_execution_job(lease=lease, claimed_at=NOW)
                forged_claim = claim.__class__(
                    job_id=claim.job_id,
                    attempt_number=claim.attempt_number,
                    claim_token="not-the-real-token",
                    started_at=claim.started_at,
                    job=claim.job,
                )

                with self.assertRaisesRegex(ExecutionQueueError, "stale"):
                    repo.complete_execution_attempt(
                        forged_claim,
                        disposition="retry",
                        status="retry",
                        failure_class="transient",
                        reason_code="process_failed",
                        result_job_id=None,
                        published=False,
                        retry_permitted=True,
                        paper_count=None,
                        valid_pdf_count=None,
                        lease=lease,
                        completed_at=NOW,
                    )

                completion = repo.complete_execution_attempt(
                    claim,
                    disposition="retry",
                    status="retry",
                    failure_class="transient",
                    reason_code="process_failed",
                    result_job_id=None,
                    published=False,
                    retry_permitted=True,
                    paper_count=None,
                    valid_pdf_count=None,
                    lease=lease,
                    completed_at=NOW,
                )
                self.assertEqual(completion.record.state, "pending")
                self.assertEqual(completion.record.current_attempt_number, 1)
                self.assertEqual(completion.attempt.disposition, "retry")

                with self.assertRaisesRegex(ExecutionQueueError, "does not match"):
                    repo.complete_execution_attempt(
                        claim,
                        disposition="retry",
                        status="retry",
                        failure_class="transient",
                        reason_code="process_failed",
                        result_job_id=None,
                        published=False,
                        retry_permitted=True,
                        paper_count=None,
                        valid_pdf_count=None,
                        lease=lease,
                        completed_at=NOW,
                    )

                second_claim = repo.claim_next_execution_job(
                    lease=lease, claimed_at=NOW
                )
                self.assertEqual(second_claim.attempt_number, 2)
                with self.assertRaisesRegex(
                    ExecutionQueueError, "does not match the supplied disposition"
                ):
                    repo.complete_execution_attempt(
                        second_claim,
                        disposition="completed",
                        status="ready",
                        failure_class=None,
                        reason_code="validated_ready",
                        result_job_id="job:" + "0" * 64,
                        published=True,
                        retry_permitted=True,
                        paper_count=3,
                        valid_pdf_count=3,
                        lease=lease,
                        completed_at=NOW,
                    )
                final = repo.complete_execution_attempt(
                    second_claim,
                    disposition="completed",
                    status="ready",
                    failure_class=None,
                    reason_code="validated_ready",
                    result_job_id="job:" + "0" * 64,
                    published=True,
                    retry_permitted=False,
                    paper_count=3,
                    valid_pdf_count=3,
                    lease=lease,
                    completed_at=NOW,
                )
                self.assertEqual(final.record.state, "completed")
                self.assertIsNone(
                    repo.claim_next_execution_job(lease=lease, claimed_at=NOW)
                )

    def test_stored_corruption_fails_closed_on_read(self):
        _, _, result = verification_bundle()
        action = scraper_action(result)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self._local_repository(path) as repo:
                lease = repo.acquire_lease("dispatch-owner")
                repo.accept_verification(
                    *verification_bundle(), lease=lease, received_at=NOW
                )
                outcome = repo.retain_existing_scraper_action(
                    action,
                    source_verification_id=result["verification_id"],
                    lease=lease,
                    enqueued_at=NOW,
                )
                repo._connection.execute(
                    "UPDATE execution_job SET job_fingerprint = ? WHERE job_id = ?",
                    ("0" * 64, outcome.record.job_id),
                )
                with self.assertRaisesRegex(StoredDataError, "fingerprint"):
                    repo.get_execution_job(outcome.record.job_id)


if __name__ == "__main__":
    unittest.main()
