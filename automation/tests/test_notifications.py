import unittest
from datetime import datetime, timezone

from automation.notifications import (
    FailureCategory,
    NotificationError,
    NotificationKind,
    TransportFailure,
    build_immediate_notification,
    classify_transport_failure,
    notification_intent_from_payload,
    redact_text,
    validate_notification_intent,
    validate_receipt_id,
)


NOW = datetime(2026, 7, 13, 20, 30, tzinfo=timezone.utc)


class NotificationContractTests(unittest.TestCase):
    def test_immediate_intent_is_stable_valid_and_round_trips(self):
        arguments = {
            "event_id": "event:icml:2026:ready",
            "occurred_at": NOW,
            "venue_id": "icml",
            "year": 2026,
            "summary": "Proceedings are ready",
            "evidence_ids": ("evidence:icml:2026:index",),
            "run_ids": ("run:icml:2026:001",),
        }
        first = build_immediate_notification(**arguments)
        second = build_immediate_notification(**arguments)
        self.assertEqual(first, second)
        self.assertEqual(first.kind, NotificationKind.IMMEDIATE)
        validate_notification_intent(first)
        self.assertEqual(notification_intent_from_payload(first.to_payload()), first)

    def test_redaction_removes_common_secret_shapes(self):
        value = redact_text(
            "Authorization: Bearer abc123 token=secret "
            "https://example.test/path?api_key=value&safe=yes"
        )
        self.assertNotIn("abc123", value)
        self.assertNotIn("secret", value)
        self.assertNotIn("api_key=value", value)
        self.assertIn("https://example.test/path", value)

    def test_mutated_identity_and_invalid_receipt_fail_closed(self):
        intent = build_immediate_notification(
            event_id="event:icml:2026:ready",
            occurred_at=NOW,
            venue_id="icml",
            year=2026,
            summary="Proceedings are ready",
            evidence_ids=("evidence:icml:2026:index",),
        )
        payload = intent.to_payload()
        payload["notification_id"] = "notification:immediate:wrong"
        with self.assertRaisesRegex(NotificationError, "stable sources"):
            validate_notification_intent(payload)
        with self.assertRaises(NotificationError):
            validate_receipt_id("bad receipt")

    def test_transport_failures_have_bounded_retry_policy(self):
        retryable = classify_transport_failure(
            TransportFailure(FailureCategory.TIMEOUT)
        )
        permanent = classify_transport_failure(
            TransportFailure(FailureCategory.PROTOCOL_ERROR)
        )
        unknown = classify_transport_failure(RuntimeError("private details"))
        self.assertTrue(retryable.retryable)
        self.assertFalse(permanent.retryable)
        self.assertEqual(unknown.category, FailureCategory.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
