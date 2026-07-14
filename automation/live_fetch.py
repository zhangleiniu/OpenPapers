"""Bounded HTTPS transport for reviewed evidence verification effects.

The deterministic verifier modules intentionally contain no network client.
This adapter implements their one-request ``EvidenceFetcher`` boundary with
transport-level DNS/SSRF checks and a hostname-verified, IP-pinned TLS
connection. P2.S constructs it manually and P2.7 may construct it behind a
separate production crawl policy; neither path is imported by the deployed
monitor or installed local service.
"""

from __future__ import annotations

import http.client
import ipaddress
import random
import socket
import ssl
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlsplit

from automation.verification import (
    FetchBoundaryError,
    FetchRequest,
    FetchResponse,
)


_SAFE_RESPONSE_HEADERS = frozenset({
    "content-length",
    "content-type",
    "etag",
    "last-modified",
    "location",
    "retry-after",
})
_CAPTCHA_MARKERS = (
    b"captcha",
    b"cf-chl-captcha",
    b"g-recaptcha",
    b"hcaptcha",
)


class LiveFetchError(FetchBoundaryError):
    """A live request was rejected before returning retained evidence."""

    def __init__(self, message: str, *, category: str = "transport_failure") -> None:
        super().__init__(message)
        self.category = category


class _HttpResponse(Protocol):
    status: int

    def getheaders(self) -> list[tuple[str, str]]: ...

    def read(self, amount: int | None = None) -> bytes: ...


class _HttpsConnection(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    def getresponse(self) -> _HttpResponse: ...

    def close(self) -> None: ...


Resolver = Callable[[str, int], Sequence[str]]
ConnectionFactory = Callable[[str, str, float], _HttpsConnection]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise LiveFetchError("live DNS resolution failed") from exc
    return tuple(record[4][0] for record in records)


def _validated_public_addresses(addresses: Sequence[str]) -> tuple[str, ...]:
    if not addresses:
        raise LiveFetchError("live DNS resolution returned no addresses")
    parsed: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise LiveFetchError("live DNS returned an invalid address") from exc
        if not address.is_global:
            raise LiveFetchError("live DNS returned a non-global address")
        parsed.add(address)
    return tuple(
        str(address)
        for address in sorted(parsed, key=lambda item: (item.version, item.packed))
    )


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    """Connect to one reviewed IP while verifying the requested hostname."""

    def __init__(self, hostname: str, address: str, timeout: float) -> None:
        super().__init__(
            hostname,
            port=443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_address = address

    def connect(self) -> None:
        if self._tunnel_host is not None:
            raise LiveFetchError("HTTP proxy tunnels are outside the live boundary")
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except BaseException:
            raw_socket.close()
            raise


def _connection_factory(
    hostname: str,
    address: str,
    timeout: float,
) -> _PinnedHttpsConnection:
    return _PinnedHttpsConnection(hostname, address, timeout)


def _target(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise LiveFetchError("live URL must use HTTPS")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise LiveFetchError("live URL host must be a DNS name, not an IP literal")
    try:
        port = parsed.port
    except ValueError as exc:
        raise LiveFetchError("live URL has an invalid port") from exc
    if port not in {None, 443}:
        raise LiveFetchError("live URL must use the HTTPS port")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return parsed.hostname.lower().rstrip("."), path


def _safe_headers(response: _HttpResponse) -> dict[str, str]:
    retained: dict[str, str] = {}
    for name, value in response.getheaders():
        normalized = name.lower()
        if normalized not in _SAFE_RESPONSE_HEADERS:
            continue
        if normalized in retained:
            raise LiveFetchError("live response has a duplicate retained header")
        if "\r" in value or "\n" in value or len(value) > 8192:
            raise LiveFetchError("live response has an unsafe retained header")
        retained[normalized] = value
    return retained


def _looks_like_captcha(headers: dict[str, str], body: bytes) -> bool:
    content_type = headers.get("content-type", "").lower()
    if "html" not in content_type:
        return False
    lowered = body[:1_000_000].lower()
    return any(marker in lowered for marker in _CAPTCHA_MARKERS)


class LiveHttpFetcher:
    """Perform policy-shaped public HTTPS GETs for an explicit shadow run."""

    def __init__(
        self,
        *,
        resolver: Resolver = _resolve_public_addresses,
        connection_factory: ConnectionFactory = _connection_factory,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._resolver = resolver
        self._connection_factory = connection_factory
        self._monotonic = monotonic
        self._sleep = sleep
        self._jitter = jitter
        self._now = now
        self._lock = threading.Lock()
        self._last_started: dict[str, float] = {}

    def _wait(self, request: FetchRequest) -> None:
        now = self._monotonic()
        previous = self._last_started.get(request.policy_domain)
        delay = request.minimum_delay_seconds
        if request.jitter_seconds:
            delay += self._jitter(0.0, request.jitter_seconds)
        if previous is not None:
            remaining = delay - (now - previous)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_started[request.policy_domain] = now

    def fetch(self, request: FetchRequest) -> FetchResponse:
        """Fetch exactly one response without redirects, retries, or secrets."""
        if request.follow_redirects:
            raise LiveFetchError("live fetch cannot follow redirects")
        hostname, target = _target(request.url)
        addresses = _validated_public_addresses(
            self._resolver(hostname, 443)
        )
        address = addresses[0]
        contact = request.user_agent_contact
        if any(character in contact for character in "\r\n"):
            raise LiveFetchError("live User-Agent contact is unsafe")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": f"OpenPapers-EvidenceVerifier/1.0 (+{contact})",
        }

        # A single lock is intentionally stricter than per-domain concurrency
        # during the bounded manual sample.
        with self._lock:
            self._wait(request)
            try:
                connection = self._connection_factory(
                    hostname, address, request.timeout_seconds
                )
                try:
                    connection.request("GET", target, body=None, headers=headers)
                    response = connection.getresponse()
                    retained_headers = _safe_headers(response)
                    content_length = retained_headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except ValueError as exc:
                            raise LiveFetchError(
                                "live response has invalid Content-Length"
                            ) from exc
                        if declared_length < 0:
                            raise LiveFetchError(
                                "live response has invalid Content-Length"
                            )
                        if declared_length > request.max_bytes:
                            raise LiveFetchError(
                                "live response exceeds the authorized byte limit"
                            )
                    body = response.read(request.max_bytes + 1)
                finally:
                    connection.close()
            except LiveFetchError:
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                raise LiveFetchError("live HTTPS request failed") from exc

        if len(body) > request.max_bytes:
            raise LiveFetchError("live response exceeds the authorized byte limit")
        if request.stop_on_captcha and _looks_like_captcha(retained_headers, body):
            raise LiveFetchError(
                "live response appears to be a CAPTCHA", category="captcha"
            )
        observed = self._now()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise LiveFetchError("live fetch clock must be timezone-aware")
        return FetchResponse(
            requested_url=request.url,
            status_code=response.status,
            headers=retained_headers,
            body=body,
            fetched_at=(
                observed.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            ),
        )
