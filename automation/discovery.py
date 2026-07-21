"""Shared request/error contract for provider-neutral discovery calls.

This module used to also host a full grounded-citation discovery pipeline
(budget ledger, immutable artifact store, two-provider escalation) behind
``automation/run_discovery.py``. That pipeline was never wired into
scheduling — the production event-date estimator
(``automation/providers/gemini.py::GeminiEventDateProvider``) only ever
needed the plain request/error/response shapes below — and was removed on
2026-07-18 along with ``run_discovery.py``. What remains here is exactly
the contract ``automation/event_dates.py`` and ``automation/providers/
gemini.py`` depend on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from automation.contracts import ContractName, validate_contract


class DiscoveryError(RuntimeError):
    """Base class for discovery failures."""


class DiscoveryValidationError(DiscoveryError):
    """Raised when provider output is unsupported or internally inconsistent."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "validation_rejected",
    ) -> None:
        super().__init__(message)
        self.category = category


class ProviderError(DiscoveryError):
    """Raised when a discovery provider cannot return a usable response."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_error",
        status_code: int | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.diagnostics = dict(diagnostics or {})


class RetryableProviderError(ProviderError):
    """Raised for a transient provider failure that may be retried."""


def safe_error_summary(error: DiscoveryError) -> str:
    """Return bounded operator diagnostics that cannot contain provider text."""
    if isinstance(error, ProviderError):
        suffix = (
            f":http_{error.status_code}"
            if error.status_code is not None else ""
        )
        return f"{error.category}{suffix}"
    if isinstance(error, DiscoveryValidationError):
        return error.category
    return type(error).__name__


@dataclass(frozen=True)
class DiscoveryRequest:
    """A single venue-year discovery request resolved from the venue catalog."""

    venue_id: str
    year: int
    display_name: str
    official_domains: tuple[str, ...]
    archival_domains: tuple[str, ...]
    lifecycle_kind: str


@dataclass(frozen=True)
class GroundingSource:
    """Allowlisted public evidence metadata returned by a search provider."""

    uri: str
    title: str | None = None
    domain: str | None = None
    provider_uri: str | None = None


@dataclass(frozen=True)
class ProviderResponse:
    """Provider body plus the search evidence that grounded it."""

    body: Mapping[str, Any]
    grounding_sources: tuple[GroundingSource, ...]
    search_queries: tuple[str, ...] = ()


def request_from_catalog(
    catalog: Mapping[str, Any],
    venue_id: str,
    year: int,
) -> DiscoveryRequest:
    """Resolve one exact venue-year request from a validated catalog."""
    validate_contract(ContractName.VENUE_CATALOG, catalog)
    for venue in catalog["venues"]:
        if venue["venue_id"] == venue_id:
            return DiscoveryRequest(
                venue_id=venue_id,
                year=year,
                display_name=venue["display_name"],
                official_domains=tuple(venue["official_domains"]),
                archival_domains=tuple(venue["archival_domains"]),
                lifecycle_kind=venue["lifecycle"]["kind"],
            )
    raise DiscoveryValidationError(f"unknown venue_id: {venue_id}")
