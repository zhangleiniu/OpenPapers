"""Bounded Resend transport for provider-idempotent plain-text email."""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
from email.utils import getaddresses, parseaddr
from typing import Callable, Protocol

from automation.notifications import (
    FailureCategory,
    NotificationIntent,
    TransportFailure,
    TransportReceipt,
    validate_notification_intent,
    validate_receipt_id,
)


RESEND_HOST = "api.resend.com"
RESEND_PATH = "/emails"
MAX_RESPONSE_BYTES = 65_536
DEFAULT_TIMEOUT_SECONDS = 15.0


class ResendNotificationError(ValueError):
    """Raised when Resend transport configuration is unsafe."""


class _Response(Protocol):
    status: int

    def read(self, amount: int | None = None) -> bytes: ...


class _Connection(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    def getresponse(self) -> _Response: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, float], _Connection]


def _connection_factory(host: str, timeout: float) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(host, port=443, timeout=timeout)


def _plain_recipient(value: str) -> str:
    if not isinstance(value, str):
        raise ResendNotificationError("recipient must be text")
    candidate = value.strip()
    if not candidate or any(c in value for c in "\r\n"):
        raise ResendNotificationError("recipient is invalid")
    addresses = getaddresses([candidate])
    if len(addresses) != 1 or addresses[0][0] or addresses[0][1] != candidate:
        raise ResendNotificationError("recipient must be one plain address")
    if candidate.count("@") != 1 or any(c in candidate for c in ",;"):
        raise ResendNotificationError("recipient is invalid")
    local, domain = candidate.rsplit("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ResendNotificationError("recipient is invalid")
    return candidate


def _sender(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n"):
        raise ResendNotificationError("sender is invalid")
    candidate = value.strip()
    addresses = getaddresses([candidate])
    display, address = parseaddr(candidate)
    if len(addresses) != 1 or addresses[0] != (display, address):
        raise ResendNotificationError("sender must contain one address")
    if address.count("@") != 1 or any(c in address for c in ",;"):
        raise ResendNotificationError("sender is invalid")
    local, domain = address.rsplit("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ResendNotificationError("sender is invalid")
    return candidate


def recipient_fingerprint(value: str) -> str:
    """Return an address-free approval identity for one plain recipient."""
    normalized = _plain_recipient(value).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _failure_for_status(status: int) -> FailureCategory:
    if status == 401:
        return FailureCategory.AUTHENTICATION
    if status == 429:
        return FailureCategory.RATE_LIMITED
    if status >= 500:
        return FailureCategory.UNAVAILABLE
    if status in {400, 413, 422}:
        return FailureCategory.PAYLOAD_INVALID
    if status in {404, 405, 409}:
        return FailureCategory.PROTOCOL_ERROR
    return FailureCategory.REJECTED


class ResendNotificationTransport:
    """Make at most one non-redirecting Resend API request per instance."""

    def __init__(
        self,
        *,
        api_key: str,
        email_from: str,
        email_to: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        connection_factory: ConnectionFactory = _connection_factory,
    ) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key
            or api_key != api_key.strip()
            or any(c in api_key for c in "\r\n")
        ):
            raise ResendNotificationError("Resend API key is missing or invalid")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= 30
        ):
            raise ResendNotificationError("timeout must be 1-30 seconds")
        self._api_key = api_key
        self._email_from = _sender(email_from)
        self._email_to = _plain_recipient(email_to)
        self._timeout_seconds = float(timeout_seconds)
        self._connection_factory = connection_factory
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    def send(
        self,
        intent: NotificationIntent,
        *,
        idempotency_key: str,
    ) -> TransportReceipt:
        validate_notification_intent(intent)
        if idempotency_key != intent.notification_id or not 1 <= len(idempotency_key) <= 256:
            raise TransportFailure(FailureCategory.PROTOCOL_ERROR)
        if self._request_count != 0:
            raise TransportFailure(FailureCategory.PROTOCOL_ERROR)

        payload = json.dumps(
            {
                "from": self._email_from,
                "to": [self._email_to],
                "subject": intent.subject,
                "text": intent.body,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Length": str(len(payload)),
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "User-Agent": "OpenPapers-Agent-Run/1.0",
        }

        connection: _Connection | None = None
        try:
            connection = self._connection_factory(
                RESEND_HOST, self._timeout_seconds
            )
            self._request_count += 1
            connection.request("POST", RESEND_PATH, body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
        except TransportFailure:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise TransportFailure(FailureCategory.TIMEOUT) from exc
        except OSError as exc:
            raise TransportFailure(FailureCategory.UNAVAILABLE) from exc
        except Exception as exc:
            raise TransportFailure(FailureCategory.PROTOCOL_ERROR) from exc
        finally:
            if connection is not None:
                connection.close()

        if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
            raise TransportFailure(FailureCategory.PROTOCOL_ERROR)
        if not isinstance(response.status, int) or isinstance(response.status, bool):
            raise TransportFailure(FailureCategory.PROTOCOL_ERROR)
        if not 200 <= response.status < 300:
            raise TransportFailure(_failure_for_status(response.status))
        try:
            decoded = json.loads(body)
            receipt_id = decoded["id"]
            if set(decoded) != {"id"}:
                raise ValueError("unexpected response fields")
            validate_receipt_id(receipt_id)
        except Exception as exc:
            raise TransportFailure(FailureCategory.PROTOCOL_ERROR) from exc
        return TransportReceipt(receipt_id=receipt_id)
