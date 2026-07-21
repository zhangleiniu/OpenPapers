"""Marker-gated production monitor effect for the P4.LC local cutover."""

from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sqlite3
import ssl
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from automation.source_change_hints import (
    prepare_source_change_hint_journal,
    record_source_change_hints,
)
from automation.local_service.service import (
    LocalEffectOutcome,
    LocalEffectStatus,
)


PRODUCTION_CONFIG = ".production-config.v1.json"
PRODUCTION_SECRETS = ".production-secrets.v1.json"
_MAX_FILE_BYTES = 16_384
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GENERATION = re.compile(r"^[1-9][0-9]{0,30}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_EMAIL = re.compile(r"^[^\s@]{1,128}@[^\s@]{1,190}$")
_SECRET_KEYS = (
    "OPENREVIEW_USERNAME",
    "OPENREVIEW_PASSWORD",
)
_RETIRED_SHADOW_MARKER = ".isolated-shadow.v1.json"
_MONITOR_TIMEZONE = ZoneInfo("America/Chicago")
_MONITOR_HOUR = 8


class ProductionControlError(ValueError):
    """Raised when production state or configuration cannot be trusted."""


@dataclass(frozen=True)
class ProductionConfiguration:
    registry_sha256: str
    backup_sha256: str
    remote_state_generation: str
    expected_source_count: int
    smtp_host: str
    smtp_port: int
    smtp_username: str
    email_from: str
    email_to: str


@dataclass(frozen=True)
class ProductionSecrets:
    openreview_username: str
    openreview_password: str
    smtp_password: str


class SourceMonitor(Protocol):
    def __call__(
        self, registry_path: Path, state_path: Path
    ) -> Sequence[Mapping[str, Any]]:
        """Return deterministic source events after updating monitor state."""


class SourceNotifier(Protocol):
    def send(
        self,
        event: Mapping[str, Any],
        *,
        configuration: ProductionConfiguration,
        password: str,
    ) -> None:
        """Deliver one changed/error event or raise before completion."""


def _private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProductionControlError("production directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        # Group read/traverse is allowed (deliberate, 2026-07-18/19 — this
        # host's staff group is a trusted small set of accounts, not the
        # public); group write and any "other" access are still forbidden.
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO)
        or not os.access(path, os.R_OK | os.W_OK | os.X_OK)
    ):
        raise ProductionControlError("production directory is unsafe")


def _private_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProductionControlError("production file is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        # See _private_directory: group read is a deliberate, trusted
        # exception; group write and "other" access remain forbidden.
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO)
        or metadata.st_size < 2
        or metadata.st_size > _MAX_FILE_BYTES
    ):
        raise ProductionControlError("production file is unsafe")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProductionControlError("production file is unavailable") from exc


def _json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_private_file(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionControlError("production file is invalid") from exc
    if not isinstance(payload, dict):
        raise ProductionControlError("production file is invalid")
    return payload


def _bounded_text(value: Any, *, field: str, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ProductionControlError(f"{field} is invalid")
    return value


def _configuration(payload: Mapping[str, Any]) -> ProductionConfiguration:
    expected = {
        "schema_version",
        "registry_sha256",
        "backup_sha256",
        "remote_state_generation",
        "expected_source_count",
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "email_from",
        "email_to",
    }
    if set(payload) != expected or payload.get("schema_version") != 1:
        raise ProductionControlError("production configuration is invalid")
    registry = payload["registry_sha256"]
    backup = payload["backup_sha256"]
    generation = payload["remote_state_generation"]
    count = payload["expected_source_count"]
    port = payload["smtp_port"]
    if not isinstance(registry, str) or not _SHA256.fullmatch(registry):
        raise ProductionControlError("registry fingerprint is invalid")
    if not isinstance(backup, str) or not _SHA256.fullmatch(backup):
        raise ProductionControlError("backup fingerprint is invalid")
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
        raise ProductionControlError("remote state generation is invalid")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ProductionControlError("expected source count is invalid")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ProductionControlError("SMTP port is invalid")
    host = _bounded_text(payload["smtp_host"], field="SMTP host", maximum=253)
    username = _bounded_text(
        payload["smtp_username"], field="SMTP username", maximum=254
    )
    sender = _bounded_text(payload["email_from"], field="email sender", maximum=320)
    recipient = _bounded_text(
        payload["email_to"], field="email recipient", maximum=320
    )
    if not _HOST.fullmatch(host) or not _EMAIL.fullmatch(sender) or not _EMAIL.fullmatch(
        recipient
    ):
        raise ProductionControlError("production notification address is invalid")
    return ProductionConfiguration(
        registry_sha256=registry,
        backup_sha256=backup,
        remote_state_generation=generation,
        expected_source_count=count,
        smtp_host=host.lower(),
        smtp_port=port,
        smtp_username=username,
        email_from=sender,
        email_to=recipient,
    )


def _secrets(payload: Mapping[str, Any]) -> ProductionSecrets:
    expected = {
        "schema_version",
        "openreview_username",
        "openreview_password",
        "smtp_password",
    }
    if set(payload) != expected or payload.get("schema_version") != 1:
        raise ProductionControlError("production secrets are invalid")
    return ProductionSecrets(
        openreview_username=_bounded_text(
            payload["openreview_username"], field="OpenReview username"
        ),
        openreview_password=_bounded_text(
            payload["openreview_password"], field="OpenReview password"
        ),
        smtp_password=_bounded_text(payload["smtp_password"], field="SMTP password"),
    )


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_production_root(
    internal_root: Path,
) -> tuple[ProductionConfiguration, ProductionSecrets]:
    """Validate exact private production files before any mutable state opens."""
    root = Path(internal_root)
    _private_directory(root)
    _private_directory(root / "control")
    _private_directory(root / "monitor")
    if (root / _RETIRED_SHADOW_MARKER).exists():
        raise ProductionControlError("production and shadow markers cannot coexist")
    configuration_bytes = _private_file(root / PRODUCTION_CONFIG)
    try:
        configuration_payload = json.loads(configuration_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionControlError("production configuration is invalid") from exc
    if not isinstance(configuration_payload, dict):
        raise ProductionControlError("production configuration is invalid")
    configuration = _configuration(configuration_payload)
    secrets = _secrets(_json_file(root / PRODUCTION_SECRETS))
    return configuration, secrets


def initialize_production_root(
    internal_root: Path,
    configuration: Mapping[str, Any],
    secrets: Mapping[str, Any],
) -> tuple[Path, Path]:
    """Create exact production files, accepting only byte-equivalent replay."""
    root = Path(internal_root)
    _private_directory(root)
    _private_directory(root / "control")
    _private_directory(root / "monitor")
    if (root / _RETIRED_SHADOW_MARKER).exists():
        raise ProductionControlError("production and shadow markers cannot coexist")
    _configuration(configuration)
    _secrets(secrets)
    config_bytes = _canonical(configuration)
    secret_bytes = _canonical(secrets)
    files = (
        (root / PRODUCTION_CONFIG, config_bytes),
        (root / PRODUCTION_SECRETS, secret_bytes),
    )
    for path, encoded in files:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            if _private_file(path) != encoded:
                raise ProductionControlError("production file conflicts with replay")
            continue
        except OSError as exc:
            raise ProductionControlError("production file creation failed") from exc
        with os.fdopen(descriptor, "wb") as file_obj:
            file_obj.write(encoded)
            file_obj.flush()
            os.fsync(file_obj.fileno())
    validate_production_root(root)
    return files[0][0], files[1][0]


@contextmanager
def _openreview_environment(secrets: ProductionSecrets):
    previous = {key: os.environ.get(key) for key in _SECRET_KEYS}
    os.environ["OPENREVIEW_USERNAME"] = secrets.openreview_username
    os.environ["OPENREVIEW_PASSWORD"] = secrets.openreview_password
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _default_monitor(registry_path: Path, state_path: Path):
    from automation.monitor import run

    return run(registry_path=registry_path, state_path=state_path, write_state=True)


class SmtpSourceNotifier:
    """One-request TLS SMTP adapter for existing monitor change/error events."""

    def send(
        self,
        event: Mapping[str, Any],
        *,
        configuration: ProductionConfiguration,
        password: str,
    ) -> None:
        event_name = (
            "openpapers.source.error"
            if event.get("status") == "error"
            else "openpapers.source.changed"
        )
        message = EmailMessage()
        message["Subject"] = f"OpenPapers: {event_name}"
        message["From"] = configuration.email_from
        message["To"] = configuration.email_to
        snapshot_name = Path(str(event.get("snapshot_path", ""))).name
        message.set_content(
            "\n".join(
                (
                    f"Event: {event_name}",
                    f"Venue: {event.get('venue', '')}",
                    f"Year: {event.get('year', '')}",
                    f"Source: {event.get('source_key', '')}",
                    f"Status: {event.get('status', '')}",
                    f"Count: {event.get('item_count', '')}",
                    f"Detail: {event.get('detail', '')}",
                    f"Snapshot: {snapshot_name}",
                )
            )
        )
        with smtplib.SMTP_SSL(
            configuration.smtp_host,
            configuration.smtp_port,
            context=ssl.create_default_context(),
            timeout=30,
        ) as client:
            client.login(configuration.smtp_username, password)
            client.send_message(message)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


WAKE_ALERT_THRESHOLD = 3
WAKE_ALERT_REPEAT_EVERY = 24


def consecutive_wake_failures(records: Sequence[Mapping[str, Any]]) -> int:
    """Count trailing failed records in oldest-to-newest run records."""
    count = 0
    for record in reversed(records):
        if record.get("status") != "failed":
            break
        count += 1
    return count


def should_alert_wake_failures(consecutive: int) -> bool:
    """Alert when failures first look systemic, then once per further day.

    The service wakes hourly, so 3 consecutive failures ≈ 3 hours broken;
    after that one reminder per further 24 failures (~daily) while broken.
    """
    if consecutive < WAKE_ALERT_THRESHOLD:
        return False
    return (
        consecutive == WAKE_ALERT_THRESHOLD
        or (consecutive - WAKE_ALERT_THRESHOLD) % WAKE_ALERT_REPEAT_EVERY == 0
    )


def send_wake_failure_alert(
    internal_root: Path,
    *,
    consecutive: int,
    latest_record: Mapping[str, Any],
    notifier: SourceNotifier | None = None,
) -> None:
    """Send one bounded email describing consecutive production wake failures.

    Uses the same validated configuration and TLS SMTP path as the monitor's
    change/error events. The caller decides whether an alert is due; this
    function only composes and sends. It raises on failure so the caller can
    swallow it — an alert problem must never change the service outcome.
    """
    configuration, secrets = validate_production_root(Path(internal_root))
    event = {
        "status": "error",
        "venue": "(service)",
        "year": "",
        "source_key": "local-control wake",
        "item_count": consecutive,
        "detail": (
            f"{consecutive} consecutive failed wakes; latest "
            f"scheduled_for={latest_record.get('scheduled_for', '')} "
            f"category={latest_record.get('failure_category', 'unknown')}"
        ),
        "snapshot_path": "",
    }
    (notifier or SmtpSourceNotifier()).send(
        event, configuration=configuration, password=secrets.smtp_password
    )


def _registry_source_count(registry_path: Path) -> int:
    from automation.monitor import load_registry

    return sum(len(entry["sources"]) for entry in load_registry(registry_path))


def _validate_monitor_state(path: Path, expected_source_count: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProductionControlError("restored monitor state is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        # Group read allowed (2026-07-19, matching _private_file above);
        # group write and any "other" access remain forbidden.
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO)
    ):
        raise ProductionControlError("restored monitor state is unsafe")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(source_state)")
            }
            count = connection.execute("SELECT COUNT(*) FROM source_state").fetchone()
    except sqlite3.Error as exc:
        raise ProductionControlError("restored monitor state is invalid") from exc
    required = {
        "venue",
        "year",
        "source_key",
        "checked_at",
        "status",
        "content_hash",
        "item_count",
        "detail",
        "snapshot_path",
    }
    if (
        integrity != ("ok",)
        or not required.issubset(columns)
        or count != (expected_source_count,)
    ):
        raise ProductionControlError("restored monitor state is incomplete")


class ProductionMonitorEffect:
    """Run one exactly claimed deterministic monitor wakeup."""

    def __init__(
        self,
        *,
        repository_root: Path,
        monitor: SourceMonitor = _default_monitor,
        notifier: SourceNotifier | None = None,
    ) -> None:
        self._repository_root = Path(repository_root)
        self._monitor = monitor
        self._notifier = notifier or SmtpSourceNotifier()

    def run(
        self,
        *,
        state_path: Path,
        execution_root: Path,
        scheduled_for: datetime,
        observed_at: datetime,
    ) -> LocalEffectOutcome:
        del execution_root, scheduled_for
        state = Path(state_path)
        internal_root = state.parent.parent
        if state != internal_root / "control" / "state.sqlite3":
            raise ProductionControlError("production control state path is invalid")
        configuration, secrets = validate_production_root(internal_root)
        registry_path = self._repository_root / "automation" / "conferences.json"
        try:
            registry_bytes = registry_path.read_bytes()
        except OSError as exc:
            raise ProductionControlError("production registry is unavailable") from exc
        if _fingerprint(registry_bytes) != configuration.registry_sha256:
            raise ProductionControlError("production registry fingerprint changed")
        if _registry_source_count(registry_path) != configuration.expected_source_count:
            raise ProductionControlError("production registry source count changed")

        journal_path = internal_root / "monitor" / "production-wakeups.sqlite3"
        monitor_state = internal_root / "monitor" / "state.sqlite3"
        _validate_monitor_state(monitor_state, configuration.expected_source_count)
        local_now = observed_at.astimezone(_MONITOR_TIMEZONE)
        monitor_due = local_now.hour >= _MONITOR_HOUR
        run_date = local_now.date().isoformat()
        journal_existed = journal_path.exists()
        if journal_existed:
            metadata = journal_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or journal_path.is_symlink()
                or metadata.st_uid != os.geteuid()
                # Group read allowed (2026-07-19, matching _private_file
                # above); group write and any "other" access remain
                # forbidden.
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO)
            ):
                raise ProductionControlError("production wakeup journal is unsafe")
        with sqlite3.connect(journal_path) as journal:
            journal.execute(
                "CREATE TABLE IF NOT EXISTS production_wakeup ("
                "run_date TEXT PRIMARY KEY, status TEXT NOT NULL "
                "CHECK (status IN ('active', 'completed')), started_at TEXT NOT NULL, "
                "completed_at TEXT)"
            )
            prepare_source_change_hint_journal(journal)
            active = journal.execute(
                "SELECT run_date FROM production_wakeup WHERE status = 'active'"
            ).fetchone()
            if active is not None:
                raise ProductionControlError("production wakeup is ambiguous")
            row = journal.execute(
                "SELECT status FROM production_wakeup WHERE run_date = ?", (run_date,)
            ).fetchone()
            if row is not None:
                if row[0] == "completed":
                    monitor_due = False
                else:
                    raise ProductionControlError("production wakeup is ambiguous")
            if monitor_due:
                journal.execute(
                    "INSERT INTO production_wakeup VALUES (?, 'active', ?, NULL)",
                    (run_date, _timestamp(observed_at)),
                )
                journal.commit()
        if not journal_existed:
            journal_path.chmod(0o600)

        if monitor_due:
            with _openreview_environment(secrets):
                events = tuple(self._monitor(registry_path, monitor_state))
            if len(events) != configuration.expected_source_count:
                raise ProductionControlError(
                    "production monitor returned an incomplete set"
                )
            for event in events:
                if event.get("status") == "error" or event.get("changed") is True:
                    self._notifier.send(
                        event,
                        configuration=configuration,
                        password=secrets.smtp_password,
                    )
            if any(event.get("status") == "error" for event in events):
                raise ProductionControlError("production monitor reported source errors")
            record_source_change_hints(
                journal_path, events, observed_at=observed_at
            )

        selection_count = 0
        if monitor_due:
            with sqlite3.connect(journal_path) as journal:
                cursor = journal.execute(
                    "UPDATE production_wakeup SET status = 'completed', "
                    "completed_at = ? WHERE run_date = ? AND status = 'active'",
                    (_timestamp(observed_at), run_date),
                )
                if cursor.rowcount != 1:
                    raise ProductionControlError("production wakeup claim was lost")
                journal.commit()
        return LocalEffectOutcome(
            LocalEffectStatus.COMPLETED
            if selection_count
            else LocalEffectStatus.NO_DUE_WORK,
            selection_count,
        )
