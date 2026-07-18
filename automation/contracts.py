"""Versioned JSON contract loading and validation for automation artifacts."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for


SCHEMA_ROOT = Path(__file__).with_name("schemas")


class ContractName(str, Enum):
    """Stable names for artifacts crossing automation component boundaries."""

    DISCOVERY_RESULT = "discovery_result"
    NOTIFICATION_INTENT = "notification_intent"
    VENUE_CATALOG = "venue_catalog"
    POLICY_CONFIG = "policy_config"


_SCHEMA_FILES = {
    ContractName.DISCOVERY_RESULT: "discovery-result.json",
    ContractName.NOTIFICATION_INTENT: "notification-intent.json",
    ContractName.VENUE_CATALOG: "venue-catalog.json",
    ContractName.POLICY_CONFIG: "policy-config.json",
}

_SUPPORTED_SCHEMA_VERSIONS = {contract: {1} for contract in ContractName}


class ContractValidationError(ValueError):
    """Raised when a versioned automation artifact violates its contract."""


def _contract_name(name: ContractName | str) -> ContractName:
    try:
        return ContractName(name)
    except ValueError as exc:
        raise ContractValidationError(f"unknown contract: {name!r}") from exc


@lru_cache(maxsize=None)
def _load_schema_cached(contract: ContractName, version: int) -> dict[str, Any]:
    if version not in _SUPPORTED_SCHEMA_VERSIONS[contract]:
        raise ContractValidationError(
            f"unsupported {contract.value} schema version: {version}")
    path = SCHEMA_ROOT / f"v{version}" / _SCHEMA_FILES[contract]
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator_for(schema).check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        raise ContractValidationError(
            f"cannot load {contract.value} schema v{version}: {exc}") from exc
    return schema


def load_schema(name: ContractName | str, version: int = 1) -> dict[str, Any]:
    """Load, self-check, and defensively copy one allowlisted schema."""
    contract = _contract_name(name)
    return json.loads(json.dumps(_load_schema_cached(contract, version)))


def _validation_path(error: ValidationError) -> str:
    parts = [str(part) for part in error.absolute_path]
    return ".".join(parts) if parts else "<root>"


def validate_contract(
    name: ContractName | str,
    payload: Mapping[str, Any],
) -> None:
    """Strictly validate an artifact using its declared schema version."""
    contract = _contract_name(name)
    version = payload.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ContractValidationError(
            f"{contract.value} requires an integer schema_version")
    schema = load_schema(contract, version)
    validator_class = validator_for(schema)
    validator = validator_class(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(payload),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        error = errors[0]
        raise ContractValidationError(
            f"{contract.value} v{version} invalid at "
            f"{_validation_path(error)}: {error.message}")


def artifact_fingerprint(payload: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 for a JSON-compatible artifact."""
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(
            f"artifact is not canonical JSON: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
