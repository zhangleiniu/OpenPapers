import unittest
from datetime import datetime, timezone

from automation.domain import Permission
from automation.live_fetch import LiveFetchError, LiveHttpFetcher
from automation.verification import FetchRequest


NOW = datetime(2026, 7, 13, 21, 30, tzinfo=timezone.utc)


def request(
    url="https://example.org/path?q=1",
    *,
    max_bytes=1024,
    minimum_delay=0.0,
    jitter=0.0,
):
    return FetchRequest(
        url=url,
        permission=Permission.METADATA_FETCH,
        max_bytes=max_bytes,
        timeout_seconds=10.0,
        policy_domain="example.org",
        user_agent_contact="https://github.com/zhangleiniu/OpenPapers",
        max_concurrency=1,
        minimum_delay_seconds=minimum_delay,
        jitter_seconds=jitter,
        honor_retry_after=True,
        stop_statuses=(403, 429),
        stop_on_captcha=True,
        api_preferred=False,
    )


class FakeResponse:
    def __init__(self, *, status=200, headers=(), body=b"ok"):
        self.status = status
        self._headers = list(headers)
        self._body = body

    def getheaders(self):
        return list(self._headers)

    def read(self, amount=None):
        return self._body if amount is None else self._body[:amount]


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, method, url, body=None, headers=None):
        self.requests.append((method, url, body, dict(headers or {})))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.value = 10.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class LiveHttpFetcherTests(unittest.TestCase):
    def test_rejects_private_mixed_empty_and_ip_literal_before_connection(self):
        connection_calls = []

        def factory(*args):
            connection_calls.append(args)
            return FakeConnection(FakeResponse())

        for addresses in (
            (),
            ("127.0.0.1",),
            ("93.184.216.34", "10.0.0.1"),
            ("not-an-address",),
        ):
            with self.subTest(addresses=addresses):
                fetcher = LiveHttpFetcher(
                    resolver=lambda _host, _port, values=addresses: values,
                    connection_factory=factory,
                )
                with self.assertRaises(LiveFetchError):
                    fetcher.fetch(request())
        fetcher = LiveHttpFetcher(
            resolver=lambda _host, _port: ("93.184.216.34",),
            connection_factory=factory,
        )
        with self.assertRaises(LiveFetchError):
            fetcher.fetch(request("https://93.184.216.34/"))
        self.assertEqual(connection_calls, [])

    def test_pins_public_address_and_sends_one_secret_free_bounded_get(self):
        response = FakeResponse(
            status=302,
            headers=(
                ("Location", "https://www.example.org/final"),
                ("Content-Type", "text/html"),
                ("Set-Cookie", "secret=not-retained"),
            ),
            body=b"redirect",
        )
        connection = FakeConnection(response)
        factory_calls = []

        def factory(hostname, address, timeout):
            factory_calls.append((hostname, address, timeout))
            return connection

        fetcher = LiveHttpFetcher(
            resolver=lambda host, port: (
                "2606:2800:220:1:248:1893:25c8:1946",
                "93.184.216.34",
            ),
            connection_factory=factory,
            now=lambda: NOW,
        )
        result = fetcher.fetch(request())

        self.assertEqual(
            factory_calls, [("example.org", "93.184.216.34", 10.0)]
        )
        self.assertEqual(len(connection.requests), 1)
        method, target, body, headers = connection.requests[0]
        self.assertEqual((method, target, body), ("GET", "/path?q=1", None))
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertIn("github.com/zhangleiniu/OpenPapers", headers["User-Agent"])
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)
        self.assertTrue(connection.closed)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.redirect_hop.target_url, "https://www.example.org/final")
        self.assertNotIn("set-cookie", result.headers)
        self.assertEqual(result.fetched_at, "2026-07-13T21:30:00Z")

    def test_enforces_declared_and_streamed_byte_limits(self):
        for response in (
            FakeResponse(headers=(("Content-Length", "1025"),), body=b"x"),
            FakeResponse(body=b"x" * 1025),
            FakeResponse(headers=(("Content-Length", "invalid"),), body=b"x"),
        ):
            with self.subTest(headers=response.getheaders()):
                fetcher = LiveHttpFetcher(
                    resolver=lambda _host, _port: ("93.184.216.34",),
                    connection_factory=lambda *_args, item=response: FakeConnection(item),
                )
                with self.assertRaises(LiveFetchError):
                    fetcher.fetch(request(max_bytes=1024))

    def test_serial_delay_and_captcha_stop_are_enforced(self):
        clock = Clock()
        responses = [
            FakeResponse(body=b"first"),
            FakeResponse(
                headers=(("Content-Type", "text/html"),),
                body=b"<div class='g-recaptcha'>challenge</div>",
            ),
        ]
        fetcher = LiveHttpFetcher(
            resolver=lambda _host, _port: ("93.184.216.34",),
            connection_factory=lambda *_args: FakeConnection(responses.pop(0)),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            jitter=lambda low, high: high,
        )
        fetcher.fetch(request(minimum_delay=2.0, jitter=1.0))
        with self.assertRaisesRegex(LiveFetchError, "CAPTCHA"):
            fetcher.fetch(request(minimum_delay=2.0, jitter=1.0))
        self.assertEqual(clock.sleeps, [3.0])

    def test_duplicate_retained_headers_and_unsafe_contact_fail_closed(self):
        duplicate = FakeResponse(headers=(
            ("Content-Type", "text/html"),
            ("content-type", "text/plain"),
        ))
        fetcher = LiveHttpFetcher(
            resolver=lambda _host, _port: ("93.184.216.34",),
            connection_factory=lambda *_args: FakeConnection(duplicate),
        )
        with self.assertRaisesRegex(LiveFetchError, "duplicate"):
            fetcher.fetch(request())
        unsafe = request()
        object.__setattr__(unsafe, "user_agent_contact", "safe\r\nCookie: secret")
        with self.assertRaisesRegex(LiveFetchError, "User-Agent"):
            fetcher.fetch(unsafe)


if __name__ == "__main__":
    unittest.main()
