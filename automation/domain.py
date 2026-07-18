"""Shared ownership and secret-boundary vocabulary for active automation."""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Sequence


class Writer(str, Enum):
    CLOUD_CONTROL_PLANE = "cloud_control_plane"
    LOCAL_CONTROL_PLANE = "local_control_plane"
    MAC_WORKER = "mac_worker"


class ArtifactKind(str, Enum):
    CONTROL_STATE = "control_state"


class DomainError(ValueError):
    """Base class for rejected automation-domain operations."""


class OwnershipError(DomainError):
    """Raised when a writer attempts to mutate state it does not own."""


class SecretBoundaryError(DomainError):
    """Raised when an artifact contains a credential-shaped key."""


_CONTROL_STATE_WRITERS = frozenset({
    Writer.CLOUD_CONTROL_PLANE,
    Writer.LOCAL_CONTROL_PLANE,
})
_SECRET_KEYS = frozenset({
    "api_key", "apikey", "authorization", "client_secret", "cookie",
    "credential", "password", "passwd", "private_key", "secret", "token",
})
_SECRET_KEY_SUFFIXES = (
    "_api_key", "_apikey", "_cookie", "_cookies", "_credential",
    "_credentials", "_password", "_passwd", "_private_key", "_secret",
    "_token", "_tokens",
)


def assert_writer_allowed(
    writer: Writer | str,
    artifact: ArtifactKind | str,
) -> None:
    """Allow only a control-plane role to own the control-state database."""
    try:
        resolved_writer = Writer(writer)
        resolved_artifact = ArtifactKind(artifact)
    except ValueError as exc:
        raise OwnershipError(
            f"unknown writer or artifact: {writer!r}, {artifact!r}"
        ) from exc
    if (
        resolved_artifact is not ArtifactKind.CONTROL_STATE
        or resolved_writer not in _CONTROL_STATE_WRITERS
    ):
        raise OwnershipError(
            f"{resolved_writer.value} cannot write {resolved_artifact.value}"
        )


def _normalized_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def assert_secret_free(payload: Any, path: Sequence[str] = ()) -> None:
    """Reject credential-shaped keys anywhere in an automation artifact."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            normalized = _normalized_key(key)
            if (
                normalized in _SECRET_KEYS
                or normalized.startswith("authorization_")
                or normalized.endswith(_SECRET_KEY_SUFFIXES)
            ):
                location = ".".join((*path, str(key)))
                raise SecretBoundaryError(
                    f"credential-shaped field is forbidden at {location}"
                )
            assert_secret_free(value, (*path, str(key)))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            assert_secret_free(value, (*path, str(index)))
