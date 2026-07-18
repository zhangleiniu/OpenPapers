"""Strict loaders for versioned automation catalog and policy configuration."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from automation.contracts import (
    ContractName,
    ContractValidationError,
    validate_contract,
)


CONFIG_ROOT = Path(__file__).with_name("config")
DEFAULT_VENUE_CATALOG = CONFIG_ROOT / "venue_catalog.v1.json"
DEFAULT_POLICY_CONFIG = CONFIG_ROOT / "policies.v1.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot load {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractValidationError(f"{path} must contain a JSON object")
    return payload


def load_venue_catalog(
    path: Path = DEFAULT_VENUE_CATALOG,
) -> dict[str, Any]:
    """Load the catalog and reject ambiguous stable IDs or aliases."""
    payload = _load_json(Path(path))
    validate_contract(ContractName.VENUE_CATALOG, payload)
    venue_ids: set[str] = set()
    aliases: dict[str, str] = {}
    for venue in payload["venues"]:
        venue_id = venue["venue_id"]
        if venue_id in venue_ids:
            raise ContractValidationError(
                f"duplicate venue_id in catalog: {venue_id}")
        venue_ids.add(venue_id)
        if venue_id not in venue["aliases"]:
            raise ContractValidationError(
                f"venue {venue_id} must include its stable ID as an alias")
        for alias in venue["aliases"]:
            previous = aliases.get(alias)
            if previous is not None and previous != venue_id:
                raise ContractValidationError(
                    f"alias {alias!r} belongs to both {previous} and {venue_id}")
            aliases[alias] = venue_id
    return deepcopy(payload)


def load_policy_config(
    path: Path = DEFAULT_POLICY_CONFIG,
) -> dict[str, Any]:
    """Load policy defaults and validate cross-field safety constraints."""
    payload = _load_json(Path(path))
    validate_contract(ContractName.POLICY_CONFIG, payload)
    discovery = payload["discovery_budget"]
    if discovery["max_calls_per_venue_per_day"] > discovery["max_calls_per_day"]:
        raise ContractValidationError(
            "per-venue discovery budget cannot exceed the global budget")
    if discovery["max_second_provider_calls_per_day"] > discovery["max_calls_per_day"]:
        raise ContractValidationError(
            "second-provider budget cannot exceed the global discovery budget")

    return deepcopy(payload)
