"""Marker-gated, effect-limited scheduler adapter for P4.LS host shadowing."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path

from automation.local_scheduler import run_scheduler_wakeup
from automation.local_service.service import (
    LOCAL_SERVICE_LABEL,
    LocalEffectOutcome,
    LocalEffectStatus,
)


ISOLATED_SHADOW_MARKER = ".isolated-shadow.v1.json"
_MARKER_PAYLOAD = {
    "schema_version": 1,
    "label": LOCAL_SERVICE_LABEL,
    "mode": "isolated_shadow",
}
_MAX_MARKER_BYTES = 1024


class IsolatedShadowError(ValueError):
    """Raised when an isolated shadow root cannot be trusted."""


def isolated_shadow_marker_path(internal_root: Path) -> Path:
    if not isinstance(internal_root, Path) or not internal_root.is_absolute():
        raise ValueError("internal root must be an absolute Path")
    if Path(os.path.normpath(internal_root)) != internal_root:
        raise ValueError("internal root must be normalized")
    return internal_root / ISOLATED_SHADOW_MARKER


def _validate_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise IsolatedShadowError("isolated shadow directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        or not os.access(path, os.R_OK | os.W_OK | os.X_OK)
    ):
        raise IsolatedShadowError("isolated shadow directory is unsafe")


def _validate_private_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise IsolatedShadowError("isolated shadow marker is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise IsolatedShadowError("isolated shadow marker is unsafe")


def validate_isolated_shadow_root(internal_root: Path) -> None:
    """Require the exact private marker before isolated state may be opened."""
    marker = isolated_shadow_marker_path(internal_root)
    _validate_private_directory(internal_root)
    _validate_private_directory(internal_root / "control")
    _validate_private_regular_file(marker)
    try:
        encoded = marker.read_bytes()
    except OSError as exc:
        raise IsolatedShadowError("isolated shadow marker is unavailable") from exc
    if not encoded or len(encoded) > _MAX_MARKER_BYTES:
        raise IsolatedShadowError("isolated shadow marker is invalid")
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IsolatedShadowError("isolated shadow marker is invalid") from exc
    if payload != _MARKER_PAYLOAD:
        raise IsolatedShadowError("isolated shadow marker is invalid")


def initialize_isolated_shadow_root(internal_root: Path) -> Path:
    """Create the exact private marker, accepting only byte-equivalent replay."""
    marker = isolated_shadow_marker_path(internal_root)
    _validate_private_directory(internal_root)
    _validate_private_directory(internal_root / "control")
    encoded = (
        json.dumps(_MARKER_PAYLOAD, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        validate_isolated_shadow_root(internal_root)
        return marker
    except OSError as exc:
        raise IsolatedShadowError("isolated shadow marker creation failed") from exc
    try:
        with os.fdopen(descriptor, "wb") as file_obj:
            file_obj.write(encoded)
            file_obj.flush()
            os.fsync(file_obj.fileno())
    except OSError as exc:
        try:
            marker.unlink()
        except OSError:
            pass
        raise IsolatedShadowError("isolated shadow marker creation failed") from exc
    validate_isolated_shadow_root(internal_root)
    return marker


class IsolatedSchedulerShadowEffect:
    """Run only bounded due selection against marker-bound isolated state."""

    def run(
        self,
        *,
        state_path: Path,
        execution_root: Path,
        scheduled_for: datetime,
        observed_at: datetime,
    ) -> LocalEffectOutcome:
        del execution_root
        state = Path(state_path)
        internal_root = state.parent.parent
        if state != internal_root / "control" / "state.sqlite3":
            raise IsolatedShadowError("isolated shadow state path is invalid")
        validate_isolated_shadow_root(internal_root)
        outcome = run_scheduler_wakeup(
            state,
            scheduled_for=scheduled_for,
            clock=lambda: observed_at,
        )
        selection_count = len(outcome.selections)
        return LocalEffectOutcome(
            status=(
                LocalEffectStatus.NO_DUE_WORK
                if selection_count == 0
                else LocalEffectStatus.COMPLETED
            ),
            selection_count=selection_count,
        )
