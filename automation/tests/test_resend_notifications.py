import json
import socket
import unittest

from automation.notifications import (
    FailureCategory,
    TransportFailure,
    build_immediate_notification,
)
from automation.resend_notifications import (
    ResendNotificationError,
    ResendNotificationTransport,
    recipient_fingerprint,
    recipient_fingerprints,
)


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = (
            json.dumps(payload or {"id": "provider-receipt:accepted"}).encode()
        )

    def read(self, amount=None):
        if amount is None:
            return self.payload
        return self.payload[:amount]


class FakeConnection:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.requests = []
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))
        if self.error is not None:
            raise self.error

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def intent():
    return build_immediate_notification(
        event_id="synthetic-event:p3s:transport",
        occurred_at="2026-07-13T23:30:00Z",
        venue_id="p3s-test",
        year=2099,
        summary="Synthetic notification transport test.",
        evidence_ids=("synthetic-evidence:p3s:transport",),
        run_ids=("synthetic-run:p3s:transport",),
    )


class ResendTransportTests(unittest.TestCase):
    def test_one_bounded_plain_text_request_uses_stable_idempotency(self):
        connection = FakeConnection()
        factory_calls = []

        def factory(host, timeout):
            factory_calls.append((host, timeout))
            return connection

        transport = ResendNotificationTransport(
            api_key="ignored-test-key",
            email_from="OpenPapers Test <sender@example.org>",
            email_to="approved@example.org",
            connection_factory=factory,
        )
        notification = intent()
        receipt = transport.send(
            notification, idempotency_key=notification.notification_id
        )

        self.assertEqual(receipt.receipt_id, "provider-receipt:accepted")
        self.assertEqual(transport.request_count, 1)
        self.assertEqual(factory_calls, [("api.resend.com", 15.0)])
        self.assertEqual(len(connection.requests), 1)
        method, path, body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/emails"))
        self.assertEqual(headers["Idempotency-Key"], notification.notification_id)
        self.assertEqual(headers["Authorization"], "Bearer ignored-test-key")
        self.assertEqual(headers["User-Agent"], "OpenPapers-Agent-Run/1.0")
        payload = json.loads(body)
        self.assertEqual(payload["to"], ["approved@example.org"])
        self.assertEqual(payload["text"], notification.body)
        self.assertNotIn("html", payload)
        self.assertTrue(connection.closed)

    def test_one_request_accepts_a_bounded_unique_recipient_allowlist(self):
        connection = FakeConnection()
        transport = ResendNotificationTransport(
            api_key="ignored-test-key",
            email_from="sender@example.org",
            email_to=("Second@example.org", "first@example.org"),
            connection_factory=lambda host, timeout: connection,
        )

        notification = intent()
        transport.send(notification, idempotency_key=notification.notification_id)

        payload = json.loads(connection.requests[0][2])
        self.assertEqual(payload["to"], ["first@example.org", "Second@example.org"])
        self.assertEqual(
            recipient_fingerprints(("Second@example.org", "first@example.org")),
            tuple(sorted((
                recipient_fingerprint("first@example.org"),
                recipient_fingerprint("second@example.org"),
            ))),
        )

        for recipients in ((), ("same@example.org", "SAME@example.org"),
                           tuple(f"user{i}@example.org" for i in range(11))):
            with self.subTest(recipients=recipients), self.assertRaises(
                ResendNotificationError
            ):
                ResendNotificationTransport(
                    api_key="key", email_from="sender@example.org",
                    email_to=recipients,
                )

    def test_configuration_and_success_response_fail_closed(self):
        for kwargs, pattern in (
            ({"api_key": "", "email_from": "a@example.org",
              "email_to": "b@example.org"}, "API key"),
            ({"api_key": "key", "email_from": "bad\n@example.org",
              "email_to": "b@example.org"}, "sender"),
            ({"api_key": "key", "email_from": "a@example.org",
              "email_to": "a@example.org,b@example.org"}, "recipient"),
        ):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                ResendNotificationError, pattern
            ):
                ResendNotificationTransport(**kwargs)

        for response, pattern in (
            (FakeResponse(payload={"unexpected": "shape"}), "receipt"),
            (FakeResponse(payload={"id": "bad receipt"}), "receipt"),
            (FakeResponse(payload={"id": "x" * 70_000}), "bound"),
        ):
            with self.subTest(pattern=pattern):
                transport = ResendNotificationTransport(
                    api_key="key",
                    email_from="a@example.org",
                    email_to="b@example.org",
                    connection_factory=lambda host, timeout: FakeConnection(response),
                )
                with self.assertRaisesRegex(TransportFailure, "protocol_error"):
                    transport.send(intent(), idempotency_key=intent().notification_id)

    def test_http_and_network_errors_map_to_bounded_categories(self):
        cases = (
            (FakeResponse(status=401), None, FailureCategory.AUTHENTICATION),
            (FakeResponse(status=422), None, FailureCategory.PAYLOAD_INVALID),
            (FakeResponse(status=429), None, FailureCategory.RATE_LIMITED),
            (FakeResponse(status=503), None, FailureCategory.UNAVAILABLE),
            (None, socket.timeout("secret raw timeout"), FailureCategory.TIMEOUT),
            (None, OSError("secret raw network"), FailureCategory.UNAVAILABLE),
        )
        for response, error, expected in cases:
            with self.subTest(expected=expected):
                transport = ResendNotificationTransport(
                    api_key="key",
                    email_from="a@example.org",
                    email_to="b@example.org",
                    connection_factory=lambda host, timeout: FakeConnection(
                        response, error
                    ),
                )
                with self.assertRaises(TransportFailure) as caught:
                    transport.send(intent(), idempotency_key=intent().notification_id)
                self.assertEqual(caught.exception.category, expected)
                self.assertNotIn("secret", str(caught.exception))

    def test_recipient_fingerprint_is_normalized_and_address_free(self):
        first = recipient_fingerprint("Approved@Example.org")
        second = recipient_fingerprint(" approved@example.ORG ")
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotIn("approved", first)


if __name__ == "__main__":
    unittest.main()
