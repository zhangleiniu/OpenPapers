import ast
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.cases import derive_case_id, validate_case_state
from automation.configuration import load_policy_config
from automation.control_state import (
    ControlStateRepository,
    LeaseLostError,
    NotificationIntentConflictError,
    StoredDataError,
)
from automation.notifications import (
    FailureCategory,
    NotificationError,
    TransportFailure,
    TransportReceipt,
    build_digest_notification,
    build_immediate_notification,
    classify_transport_failure,
    deliver_notification,
    notification_intent_from_payload,
    validate_notification_intent,
)
from automation.reminders import build_case_digest


MODULE = Path(__file__).resolve().parents[1] / "notifications.py"
NOW = datetime(2026, 7, 13, 22, 30, tzinfo=timezone.utc)


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, *, seconds):
        self.value += timedelta(seconds=seconds)


class FakeTransport:
    def __init__(self, outcomes=(), *, on_send=None):
        self.outcomes = list(outcomes)
        self.on_send = on_send
        self.calls = []

    def send(self, intent, *, idempotency_key):
        self.calls.append((intent, idempotency_key))
        if self.on_send is not None:
            self.on_send()
        outcome = (
            self.outcomes.pop(0)
            if self.outcomes
            else TransportReceipt(f"fake-receipt:{len(self.calls)}")
        )
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def immediate_intent(*, summary="Archival PDFs are unavailable."):
    return build_immediate_notification(
        event_id="case-event:icml:2026:no-pdf:1",
        occurred_at=NOW,
        venue_id="icml",
        year=2026,
        summary=summary,
        evidence_ids=("verification:icml:2026:list",),
        run_ids=("run:verification:2026-07-13",),
    )


def case_state(*, venue_id, blocker, age_days):
    meaningful = NOW - timedelta(days=age_days)
    state = {
        "schema_version": 1,
        "case_id": derive_case_id(venue_id, 2026, blocker),
        "venue_id": venue_id,
        "year": 2026,
        "blocker": blocker,
        "status": "open",
        "summary": f"Unresolved {blocker} for {venue_id} 2026.",
        "evidence_ids": [f"evidence:{venue_id}:2026:{blocker}"],
        "first_observed_at": timestamp(meaningful),
        "last_checked_at": timestamp(meaningful),
        "last_meaningful_change_at": timestamp(meaningful),
        "snoozed_until": None,
        "resolution": None,
    }
    validate_case_state(state)
    return state


class IntentAndRedactionTests(unittest.TestCase):
    def test_immediate_intent_is_stable_redacted_and_keeps_safe_references(self):
        secret_summary = (
            "PDF check failed password=hunter2; Authorization: Bearer abc.def "
            "api_key=topsecret at "
            "https://example.org/list?year=2026&X-Amz-Signature=signed-value "
            "-----BEGIN PRIVATE KEY-----\nprivate-material\n"
            "-----END PRIVATE KEY-----"
        )
        first = immediate_intent(summary=secret_summary)
        second = immediate_intent(summary=secret_summary)
        self.assertEqual(first, second)
        self.assertEqual(first.kind.value, "immediate")
        self.assertEqual(first.source_ids, ("case-event:icml:2026:no-pdf:1",))
        self.assertIn("verification:icml:2026:list", first.body)
        self.assertIn("run:verification:2026-07-13", first.body)
        self.assertIn("[REDACTED]", first.body)
        for secret in (
            "hunter2",
            "abc.def",
            "topsecret",
            "signed-value",
            "private-material",
        ):
            self.assertNotIn(secret, first.subject + first.body)
        validate_notification_intent(first)
        self.assertEqual(
            notification_intent_from_payload(first.to_payload()), first
        )

    def test_digest_intent_keeps_one_grouped_message_and_stable_slots(self):
        digest = build_case_digest(
            [
                case_state(venue_id="icml", blocker="no_pdf", age_days=7),
                case_state(
                    venue_id="aistats",
                    blocker="unsupported_scraper",
                    age_days=30,
                ),
            ],
            load_policy_config(),
            NOW,
        )
        first = build_digest_notification(
            digest, run_ids=("run:reminders:2026-07-13",)
        )
        second = build_digest_notification(
            digest, run_ids=("run:reminders:2026-07-13",)
        )
        self.assertEqual(first, second)
        self.assertEqual(first.kind.value, "digest")
        self.assertEqual(len(first.source_ids), 2)
        self.assertEqual(len(set(first.source_ids)), 2)
        self.assertIn("WEEKLY (1)", first.body)
        self.assertIn("MONTHLY (1)", first.body)
        self.assertIn("Due cases: 2", first.body)
        self.assertEqual(len(first.evidence_ids), 2)

        forged_item = replace(
            digest.groups[0].items[0],
            venue_id="icml\npassword=leaked",
        )
        forged_group = replace(digest.groups[0], items=(forged_item,))
        with self.assertRaisesRegex(NotificationError, "venue_id"):
            build_digest_notification(
                replace(digest, groups=(forged_group, *digest.groups[1:]))
            )

    def test_invalid_or_unredacted_intent_fails_closed(self):
        intent = immediate_intent()
        with self.assertRaisesRegex(NotificationError, "notification_id"):
            validate_notification_intent(
                replace(intent, notification_id="notification:immediate:wrong")
            )
        with self.assertRaisesRegex(NotificationError, "NotificationKind"):
            validate_notification_intent(replace(intent, kind="immediate"))
        with self.assertRaisesRegex(NotificationError, "unredacted"):
            validate_notification_intent(
                replace(intent, body=intent.body + "\npassword=leaked")
            )
        with self.assertRaisesRegex(NotificationError, "credential-shaped"):
            build_immediate_notification(
                event_id="event:token:leaked",
                occurred_at=NOW,
                venue_id="icml",
                year=2026,
                summary="Safe summary.",
                evidence_ids=("verification:icml:2026:list",),
            )
        empty = build_case_digest([], load_policy_config(), NOW)
        with self.assertRaisesRegex(NotificationError, "at least one"):
            build_digest_notification(empty)

    def test_retry_classification_is_typed_and_unknown_text_is_not_exposed(self):
        for category in (
            FailureCategory.TIMEOUT,
            FailureCategory.RATE_LIMITED,
            FailureCategory.UNAVAILABLE,
        ):
            with self.subTest(category=category):
                self.assertTrue(
                    classify_transport_failure(TransportFailure(category)).retryable
                )
        for category in (
            FailureCategory.AUTHENTICATION,
            FailureCategory.INVALID_RECIPIENT,
            FailureCategory.REJECTED,
            FailureCategory.PAYLOAD_INVALID,
            FailureCategory.PROTOCOL_ERROR,
        ):
            with self.subTest(category=category):
                self.assertFalse(
                    classify_transport_failure(TransportFailure(category)).retryable
                )
        unknown = classify_transport_failure(
            RuntimeError("password=must-never-be-persisted")
        )
        self.assertEqual(unknown.category, FailureCategory.UNKNOWN)
        self.assertTrue(unknown.retryable)


class PersistentDeliveryTests(unittest.TestCase):
    def test_delivered_replay_survives_reopen_without_a_second_fake_call(self):
        intent = immediate_intent()
        transport = FakeTransport([TransportReceipt("fake-receipt:accepted")])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            with ControlStateRepository(path, clock=clock) as store:
                lease = store.acquire_lease("notification-flow")
                first = deliver_notification(
                    intent,
                    repository=store,
                    lease=lease,
                    transport=transport,
                    now=NOW,
                )
                replay = deliver_notification(
                    intent,
                    repository=store,
                    lease=lease,
                    transport=transport,
                    now=NOW,
                )
                self.assertTrue(first.attempted)
                self.assertEqual(first.status, "delivered")
                self.assertFalse(replay.attempted)
                self.assertEqual(replay.receipt_id, "fake-receipt:accepted")
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(
                    transport.calls[0][1], intent.notification_id
                )
                self.assertEqual(
                    [item.outcome for item in store.notification_attempt_history(
                        intent.notification_id
                    )],
                    ["delivered"],
                )

            with ControlStateRepository(path) as reopened:
                record = reopened.get_notification(intent.notification_id)
                self.assertEqual(record.status, "delivered")
                self.assertEqual(record.intent, intent)

    def test_retryable_failure_records_only_category_then_succeeds(self):
        intent = immediate_intent()
        transport = FakeTransport(
            [
                TransportFailure(FailureCategory.UNKNOWN),
                TransportReceipt("fake-receipt:retry-success"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("notification-flow")
                failed = deliver_notification(
                    intent,
                    repository=store,
                    lease=lease,
                    transport=transport,
                    now=NOW,
                )
                self.assertEqual(failed.status, "retryable")
                self.assertEqual(failed.failure_category, FailureCategory.UNKNOWN)
                record = store.get_notification(intent.notification_id)
                self.assertEqual(record.last_failure_category, "unknown")
                self.assertNotIn(
                    "authorization",
                    str(record) + str(store.notification_attempt_history(
                        intent.notification_id
                    )),
                )

                delivered = deliver_notification(
                    intent,
                    repository=store,
                    lease=lease,
                    transport=transport,
                    now=NOW + timedelta(seconds=1),
                )
                self.assertEqual(delivered.status, "delivered")
                self.assertEqual(delivered.attempt_number, 2)
                self.assertEqual(
                    [item.outcome for item in store.notification_attempt_history(
                        intent.notification_id
                    )],
                    ["retryable", "delivered"],
                )

    def test_untyped_transport_bug_propagates_and_leaves_in_flight(self):
        intent = immediate_intent()
        transport = FakeTransport(
            [RuntimeError("password=must-never-be-persisted")]
        )
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("notification-flow")
                with self.assertRaisesRegex(RuntimeError, "must-never"):
                    deliver_notification(
                        intent,
                        repository=store,
                        lease=lease,
                        transport=transport,
                        now=NOW,
                    )
                record = store.get_notification(intent.notification_id)
                self.assertEqual(record.status, "in_flight")
                self.assertNotIn("must-never", str(record))

    def test_permanent_and_in_flight_states_suppress_duplicate_calls(self):
        intent = immediate_intent()
        permanent = FakeTransport(
            [TransportFailure(FailureCategory.INVALID_RECIPIENT)]
        )
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("notification-flow")
                failed = deliver_notification(
                    intent,
                    repository=store,
                    lease=lease,
                    transport=permanent,
                    now=NOW,
                )
                replay = deliver_notification(
                    intent,
                    repository=store,
                    lease=lease,
                    transport=permanent,
                    now=NOW + timedelta(seconds=1),
                )
                self.assertEqual(failed.status, "permanent_failure")
                self.assertFalse(replay.attempted)
                self.assertEqual(len(permanent.calls), 1)

            invalid_receipt_path = Path(directory) / "invalid-receipt.sqlite3"
            with ControlStateRepository(
                invalid_receipt_path, clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("notification-flow")
                protocol_failure = deliver_notification(
                    intent,
                    repository=store,
                    lease=lease,
                    transport=FakeTransport([object()]),
                    now=NOW,
                )
                self.assertEqual(protocol_failure.status, "permanent_failure")
                self.assertEqual(
                    protocol_failure.failure_category,
                    FailureCategory.PROTOCOL_ERROR,
                )

            other_path = Path(directory) / "in-flight.sqlite3"
            with ControlStateRepository(other_path, clock=MutableClock()) as store:
                lease = store.acquire_lease("notification-flow")
                claim = store.prepare_notification_delivery(
                    intent, lease=lease, started_at=NOW
                )
                self.assertEqual(claim.outcome, "in_flight")
                unused = FakeTransport()
                replay = deliver_notification(
                    intent,
                    repository=store,
                    lease=lease,
                    transport=unused,
                    now=NOW + timedelta(seconds=1),
                )
                self.assertEqual(replay.status, "in_flight")
                self.assertFalse(replay.attempted)
                self.assertEqual(unused.calls, [])

    def test_same_source_cannot_change_meaning(self):
        first = immediate_intent(summary="First immutable meaning.")
        conflicting = immediate_intent(summary="Different immutable meaning.")
        self.assertEqual(first.notification_id, conflicting.notification_id)
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("notification-flow")
                deliver_notification(
                    first,
                    repository=store,
                    lease=lease,
                    transport=FakeTransport(),
                    now=NOW,
                )
                with self.assertRaises(NotificationIntentConflictError):
                    store.prepare_notification_delivery(
                        conflicting,
                        lease=lease,
                        started_at=NOW + timedelta(seconds=1),
                    )

    def test_digest_source_slot_cannot_move_to_a_different_intent(self):
        policy = load_policy_config()
        first_digest = build_case_digest(
            [case_state(venue_id="icml", blocker="no_pdf", age_days=7)],
            policy,
            NOW,
        )
        expanded_digest = build_case_digest(
            [
                case_state(venue_id="icml", blocker="no_pdf", age_days=7),
                case_state(
                    venue_id="aistats",
                    blocker="unsupported_scraper",
                    age_days=30,
                ),
            ],
            policy,
            NOW,
        )
        first = build_digest_notification(first_digest)
        expanded = build_digest_notification(expanded_digest)
        self.assertNotEqual(first.notification_id, expanded.notification_id)
        self.assertTrue(set(first.source_ids) < set(expanded.source_ids))
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("notification-flow")
                deliver_notification(
                    first,
                    repository=store,
                    lease=lease,
                    transport=FakeTransport(),
                    now=NOW,
                )
                with self.assertRaisesRegex(
                    NotificationIntentConflictError, "source"
                ):
                    store.prepare_notification_delivery(
                        expanded,
                        lease=lease,
                        started_at=NOW + timedelta(seconds=1),
                    )

    def test_stored_notification_corruption_fails_closed(self):
        intent = immediate_intent()
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("notification-flow")
                deliver_notification(
                    intent,
                    repository=store,
                    lease=lease,
                    transport=FakeTransport(),
                    now=NOW,
                )
                store._connection.execute(
                    "UPDATE notification_intent SET attempt_count = 2 "
                    "WHERE notification_id = ?",
                    (intent.notification_id,),
                )
                with self.assertRaisesRegex(StoredDataError, "attempt count"):
                    store.get_notification(intent.notification_id)

    def test_lease_loss_after_fake_acceptance_leaves_closed_in_flight_claim(self):
        intent = immediate_intent()
        clock = MutableClock()
        transport = FakeTransport(on_send=lambda: clock.advance(seconds=2))
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=clock
            ) as store:
                lease = store.acquire_lease("notification-flow", ttl_seconds=1)
                with self.assertRaises(LeaseLostError):
                    deliver_notification(
                        intent,
                        repository=store,
                        lease=lease,
                        transport=transport,
                        now=NOW,
                    )
                self.assertEqual(
                    store.get_notification(intent.notification_id).status,
                    "in_flight",
                )
                replacement = store.acquire_lease("recovery-flow")
                replay_transport = FakeTransport()
                replay = deliver_notification(
                    intent,
                    repository=store,
                    lease=replacement,
                    transport=replay_transport,
                    now=clock.value,
                )
                self.assertFalse(replay.attempted)
                self.assertEqual(replay.status, "in_flight")
                self.assertEqual(replay_transport.calls, [])


class ScopeBoundaryTests(unittest.TestCase):
    def test_module_has_no_real_transport_or_p3_4_integration_dependency(self):
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
            {
                "email",
                "smtplib",
                "requests",
                "urllib3",
                "httpx",
                "prefect",
                "google",
            }.isdisjoint(roots)
        )
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        self.assertTrue(
            {
                "ControlStateRepository",
                "CaseObservation",
                "ActionIntent",
                "reduce_verification_record",
            }.isdisjoint(imported_names)
        )


if __name__ == "__main__":
    unittest.main()
