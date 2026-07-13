import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.notifications import (
    FailureCategory,
    TransportFailure,
    TransportReceipt,
)
from automation.resend_notifications import recipient_fingerprint
from automation.run_notification_canary import main


class FakeTransport:
    instances = []

    def __init__(self, *, api_key, email_from, email_to):
        self.api_key_present = bool(api_key)
        self.email_from_present = bool(email_from)
        self.email_to_fingerprint = recipient_fingerprint(email_to)
        self.calls = []
        self.request_count = 0
        self.__class__.instances.append(self)

    def send(self, intent, *, idempotency_key):
        self.calls.append((intent, idempotency_key))
        self.request_count += 1
        return TransportReceipt("fake-provider-receipt:accepted")


class RetryableFailureTransport(FakeTransport):
    def send(self, intent, *, idempotency_key):
        self.calls.append((intent, idempotency_key))
        self.request_count += 1
        raise TransportFailure(FailureCategory.RATE_LIMITED)


class NotificationCanaryCommandTests(unittest.TestCase):
    def setUp(self):
        FakeTransport.instances = []
        self.environment = {
            "RESEND_KEY": "ignored-test-key",
            "OPENPAPERS_CANARY_EMAIL_FROM": "sender@example.org",
            "OPENPAPERS_CANARY_EMAIL_TO": "approved@example.org",
        }
        self.approval = recipient_fingerprint(
            self.environment["OPENPAPERS_CANARY_EMAIL_TO"]
        )

    def test_command_refuses_without_live_before_constructing_transport(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, self.environment, clear=True
        ), patch("sys.stderr"), patch("builtins.print"):
            with self.assertRaises(SystemExit) as caught:
                main(
                    [
                        "--output-root", directory,
                        "--approved-recipient-sha256", self.approval,
                    ],
                    transport_factory=FakeTransport,
                )
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(FakeTransport.instances, [])

    def test_recipient_mismatch_refuses_before_transport_or_output(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, self.environment, clear=True
        ), patch("sys.stderr"):
            root = Path(directory) / "canary"
            with self.assertRaises(SystemExit) as caught:
                main(
                    [
                        "--live",
                        "--output-root", str(root),
                        "--approved-recipient-sha256", "0" * 64,
                    ],
                    transport_factory=FakeTransport,
                )
            self.assertFalse(root.exists())
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(FakeTransport.instances, [])

    def test_one_synthetic_digest_delivers_once_and_replay_is_suppressed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, self.environment, clear=True
        ), patch("builtins.print") as printed:
            root = Path(directory) / "canary"
            args = [
                "--live",
                "--output-root", str(root),
                "--approved-recipient-sha256", self.approval,
            ]
            self.assertEqual(
                main(args, transport_factory=FakeTransport), 0
            )
            self.assertEqual(
                main(args, transport_factory=FakeTransport), 0
            )

            marker = json.loads(
                (root / "canary-request.v1.json").read_text(encoding="utf-8")
            )
            result = json.loads(
                (root / "canary-result.v1.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(FakeTransport.instances), 2)
        self.assertEqual([item.request_count for item in FakeTransport.instances], [1, 0])
        delivered_intent = FakeTransport.instances[0].calls[0][0]
        self.assertIn("P3.S SYNTHETIC", delivered_intent.subject)
        self.assertIn("WEEKLY (1)", delivered_intent.body)
        self.assertIn("MONTHLY (1)", delivered_intent.body)
        self.assertIn("DORMANT (1)", delivered_intent.body)
        self.assertEqual(marker["recipient_sha256"], self.approval)
        self.assertNotIn("approved@example.org", json.dumps(marker) + json.dumps(result))
        self.assertEqual(result["delivery"]["status"], "delivered")
        self.assertEqual(result["delivery"]["attempt_count"], 1)
        self.assertEqual(result["delivery"]["external_request_count"], 1)
        self.assertRegex(result["delivery"]["receipt_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(result["fatigue"]["due_count"], 3)
        self.assertEqual(result["fatigue"]["group_count"], 3)
        self.assertTrue(result["drills"]["retryable_failure_recorded"])
        self.assertTrue(result["drills"]["case_state_untouched"])
        self.assertTrue(result["drills"]["rollback_root_removed"])
        self.assertIn('"synthetic_only": true', printed.call_args.args[0])

    def test_unmarked_nonempty_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, self.environment, clear=True
        ), patch("sys.stderr"):
            root = Path(directory) / "shadow-output"
            root.mkdir()
            (root / "foreign.sqlite3").write_text("not a canary")
            with self.assertRaises(SystemExit) as caught:
                main(
                    [
                        "--live",
                        "--output-root", str(root),
                        "--approved-recipient-sha256", self.approval,
                    ],
                    transport_factory=FakeTransport,
                )
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(FakeTransport.instances, [])

    def test_retryable_root_cannot_make_a_second_canary_request(self):
        RetryableFailureTransport.instances = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, self.environment, clear=True
        ), patch("sys.stderr"), patch("builtins.print"):
            root = Path(directory) / "canary"
            args = [
                "--live",
                "--output-root", str(root),
                "--approved-recipient-sha256", self.approval,
            ]
            self.assertEqual(
                main(args, transport_factory=RetryableFailureTransport), 3
            )
            with self.assertRaises(SystemExit) as caught:
                main(args, transport_factory=FakeTransport)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(len(RetryableFailureTransport.instances), 1)
        self.assertEqual(RetryableFailureTransport.instances[0].request_count, 1)
        self.assertEqual(FakeTransport.instances, [])

    def test_canary_has_no_p3_4_or_deployment_integration_import(self):
        module = (
            Path(__file__).resolve().parents[1] / "notification_canary.py"
        )
        tree = ast.parse(module.read_text(encoding="utf-8"))
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
        self.assertTrue(
            {
                "automation.notification_integration",
                "automation.prefect_flows",
                "automation.monitor",
                "google.cloud",
                "prefect",
            }.isdisjoint(imports)
        )


if __name__ == "__main__":
    unittest.main()
