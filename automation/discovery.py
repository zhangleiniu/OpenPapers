"""Provider-neutral, shadow-only discovery with evidence and cost controls."""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlparse

from automation.contracts import (
    ContractName,
    ContractValidationError,
    artifact_fingerprint,
    validate_contract,
)
from automation.domain import assert_secret_free


PROMPT_VERSION = "v1"
SCHEMA_VERSION = 1


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


class DiscoveryStorageError(DiscoveryError):
    """Raised when a retained artifact, cache, or ledger is corrupt."""


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


class BudgetExceeded(DiscoveryError):
    """Raised before a provider call that would exceed configured policy."""


def safe_error_summary(error: DiscoveryError) -> str:
    """Return bounded operator diagnostics that cannot contain provider text."""
    if isinstance(error, BudgetExceeded):
        return "budget_exceeded"
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


class DiscoveryProvider(Protocol):
    """Provider-neutral boundary for one grounded discovery attempt."""

    name: str
    model: str
    prompt_version: str
    attempt_cost: int

    def discover(self, request: DiscoveryRequest) -> ProviderResponse:
        """Return structured observations grounded in public search sources."""


@dataclass(frozen=True)
class BudgetLimits:
    """Runtime limits loaded from the versioned discovery policy."""

    max_calls_per_day: int
    max_calls_per_venue_per_day: int
    max_concurrency: int
    max_second_provider_calls_per_day: int

    @classmethod
    def from_policy(cls, policy: Mapping[str, Any]) -> "BudgetLimits":
        """Build limits from a validated policy configuration."""
        validate_contract(ContractName.POLICY_CONFIG, policy)
        return cls(**policy["discovery_budget"])


@dataclass(frozen=True)
class StoredDiscovery:
    """One retained result returned by a provider or the cache."""

    result: Mapping[str, Any]
    artifact_path: Path
    cache_hit: bool
    provider_role: str


@dataclass(frozen=True)
class DiscoveryOutcome:
    """Primary observation and optional exception-path second observation."""

    primary: StoredDiscovery
    secondary: StoredDiscovery | None
    escalation_requested: bool
    escalation_skipped_reason: str | None


class EscalationPolicy(Protocol):
    """Policy boundary for deciding whether independent discovery is useful."""

    def should_escalate(self, result: Mapping[str, Any]) -> bool:
        """Return whether a second provider should independently observe."""


@dataclass(frozen=True)
class LowConfidenceEscalation:
    """Escalate low-confidence results or results reporting conflicts."""

    threshold: float = 0.65

    def should_escalate(self, result: Mapping[str, Any]) -> bool:
        uncertainties = " ".join(result["uncertainties"]).lower()
        return (
            result["confidence"] < self.threshold
            or "conflict" in uncertainties
            or "contradict" in uncertainties
        )


def utc_now() -> datetime:
    """Return an aware UTC clock value."""
    return datetime.now(timezone.utc)


def format_datetime(value: datetime) -> str:
    """Format an aware datetime as canonical UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    """Parse an ISO datetime and normalize it to UTC."""
    if not isinstance(value, str):
        raise DiscoveryStorageError("stored datetime must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiscoveryStorageError(f"invalid stored datetime: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DiscoveryStorageError("stored datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


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


def discovery_request_fingerprint(
    request: DiscoveryRequest,
    provider: DiscoveryProvider,
) -> str:
    """Fingerprint the stable inputs available before a provider call."""
    return artifact_fingerprint({
        "schema_version": SCHEMA_VERSION,
        "prompt_version": provider.prompt_version,
        "provider": provider.name,
        "model": provider.model,
        "venue_id": request.venue_id,
        "year": request.year,
        "lifecycle_kind": request.lifecycle_kind,
    })


def _safe_https_url(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise DiscoveryValidationError(f"{field} must be a bounded HTTPS URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None:
        raise DiscoveryValidationError(f"{field} must be a public HTTPS URL")
    if parsed.password is not None:
        raise DiscoveryValidationError(f"{field} cannot contain credentials")
    return value


def _normalized_domain(value: str | None, uri: str) -> str:
    domain = value or urlparse(uri).hostname or ""
    return domain.lower().rstrip(".")


def _domain_matches(candidate: str, configured: Sequence[str]) -> bool:
    return any(
        candidate == domain or candidate.endswith(f".{domain}")
        for domain in configured
    )


def _validate_source_type(
    source_type: str,
    source: GroundingSource,
    request: DiscoveryRequest,
) -> None:
    domain = _normalized_domain(source.domain, source.uri)
    if source_type == "official" and not _domain_matches(
            domain, request.official_domains):
        raise DiscoveryValidationError(
            f"official evidence domain {domain!r} is not registered for "
            f"{request.venue_id}",
            category="source_class_mismatch",
        )
    if source_type == "archival" and not _domain_matches(
            domain, request.archival_domains):
        raise DiscoveryValidationError(
            f"archival evidence domain {domain!r} is not registered for "
            f"{request.venue_id}",
            category="source_class_mismatch",
        )


_BODY_FIELDS = {
    "venue_id",
    "year",
    "conference_status",
    "paper_list_status",
    "metadata_status",
    "pdf_status",
    "proceedings_status",
    "claims",
    "candidate_milestones",
    "confidence",
    "uncertainties",
}
_CLAIM_FIELDS = {
    "venue_id",
    "year",
    "claim_kind",
    "statement",
    "evidence_urls",
    "source_type",
    "published_at",
}
_MILESTONE_FIELDS = {
    "venue_id",
    "year",
    "milestone_type",
    "scope",
    "date",
    "evidence_urls",
    "source_type",
}
_STATUS_CLAIM_KINDS = {
    "conference_status": "conference",
    "paper_list_status": "paper_list",
    "metadata_status": "metadata",
    "pdf_status": "pdf",
    "proceedings_status": "proceedings",
}


def normalize_provider_response(
    request: DiscoveryRequest,
    provider: DiscoveryProvider,
    response: ProviderResponse,
    checked_at: datetime,
) -> dict[str, Any]:
    """Validate grounded provider output and build discovery-result v1."""
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("checked_at must include a timezone")
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", provider.name) is None:
        raise DiscoveryValidationError(
            "provider name must be a stable lowercase identifier")
    if not provider.model or len(provider.model) > 200:
        raise DiscoveryValidationError("provider model identifier is invalid")
    if re.fullmatch(r"v[1-9][0-9]*", provider.prompt_version) is None:
        raise DiscoveryValidationError("provider prompt version is invalid")
    body = deepcopy(dict(response.body))
    if set(body) != _BODY_FIELDS:
        missing = sorted(_BODY_FIELDS - set(body))
        unknown = sorted(set(body) - _BODY_FIELDS)
        raise DiscoveryValidationError(
            f"provider body fields differ; missing={missing}, unknown={unknown}",
            category="body_shape_mismatch",
        )
    if body["venue_id"] != request.venue_id or body["year"] != request.year:
        raise DiscoveryValidationError(
            "provider result venue/year does not match the request",
            category="venue_year_mismatch",
        )
    if request.lifecycle_kind == "continuous":
        body["conference_status"] = "unknown"
        body["candidate_milestones"] = []
    if not response.grounding_sources:
        raise DiscoveryValidationError("provider returned no grounding sources")

    sources: dict[str, GroundingSource] = {}
    for index, source in enumerate(response.grounding_sources):
        uri = _safe_https_url(source.uri, field=f"grounding_sources[{index}].uri")
        if source.title is not None and len(source.title) > 500:
            raise DiscoveryValidationError("grounding source title is too long")
        if source.domain is not None and len(source.domain) > 253:
            raise DiscoveryValidationError("grounding source domain is too long")
        if source.provider_uri is not None:
            _safe_https_url(
                source.provider_uri,
                field=f"grounding_sources[{index}].provider_uri",
            )
        sources[uri] = source

    normalized_claims: list[dict[str, Any]] = []
    claims = body["claims"]
    if not isinstance(claims, list):
        raise DiscoveryValidationError("claims must be a list")
    for index, raw_claim in enumerate(claims):
        if not isinstance(raw_claim, Mapping) or set(raw_claim) != _CLAIM_FIELDS:
            raise DiscoveryValidationError(
                f"claim {index} has missing or unknown fields")
        if (raw_claim["venue_id"] != request.venue_id
                or raw_claim["year"] != request.year):
            raise DiscoveryValidationError(
                f"claim {index} venue/year does not match the request")
        evidence_urls = raw_claim["evidence_urls"]
        if not isinstance(evidence_urls, list) or not evidence_urls:
            raise DiscoveryValidationError(
                f"claim {index} has no supporting evidence URL")
        for url_index, url in enumerate(evidence_urls):
            url = _safe_https_url(
                url, field=f"claims[{index}].evidence_urls[{url_index}]")
            if url not in sources:
                raise DiscoveryValidationError(
                    f"claim {index} cites a URL absent from grounding metadata",
                    category="unsupported_claim_evidence",
                )
            _validate_source_type(raw_claim["source_type"], sources[url], request)
        normalized_claims.append({
            "claim_id": f"claim:{request.venue_id}:{request.year}:{index + 1:03d}",
            "claim_kind": raw_claim["claim_kind"],
            "statement": raw_claim["statement"],
            "evidence_urls": list(dict.fromkeys(evidence_urls)),
            "source_type": raw_claim["source_type"],
            "published_at": raw_claim["published_at"],
        })

    normalized_milestones: list[dict[str, Any]] = []
    milestones = body["candidate_milestones"]
    if not isinstance(milestones, list):
        raise DiscoveryValidationError("candidate_milestones must be a list")
    for index, raw_milestone in enumerate(milestones):
        if (not isinstance(raw_milestone, Mapping)
                or set(raw_milestone) != _MILESTONE_FIELDS):
            raise DiscoveryValidationError(
                f"candidate milestone {index} has missing or unknown fields")
        if (raw_milestone["venue_id"] != request.venue_id
                or raw_milestone["year"] != request.year):
            raise DiscoveryValidationError(
                f"candidate milestone {index} venue/year does not match request")
        try:
            candidate_date = date.fromisoformat(raw_milestone["date"])
        except (TypeError, ValueError) as exc:
            raise DiscoveryValidationError(
                f"candidate milestone {index} has an invalid date") from exc
        allowed_years = (
            {request.year}
            if raw_milestone["milestone_type"] in {
                "conference_start", "conference_end"
            }
            else {request.year - 1, request.year}
        )
        if candidate_date.year not in allowed_years:
            raise DiscoveryValidationError(
                f"candidate milestone {index} date is outside the lifecycle",
                category="milestone_year_mismatch",
            )
        evidence_urls = raw_milestone["evidence_urls"]
        if not isinstance(evidence_urls, list) or not evidence_urls:
            raise DiscoveryValidationError(
                f"candidate milestone {index} has no evidence URL")
        for url_index, url in enumerate(evidence_urls):
            url = _safe_https_url(
                url,
                field=(f"candidate_milestones[{index}]"
                       f".evidence_urls[{url_index}]"),
            )
            if url not in sources:
                raise DiscoveryValidationError(
                    f"candidate milestone {index} cites unsupported evidence",
                    category="unsupported_milestone_evidence",
                )
            _validate_source_type(
                raw_milestone["source_type"], sources[url], request)
        expected_scope = (
            "main_track"
            if raw_milestone["milestone_type"] == "acceptance_notification"
            else "conference"
        )
        if raw_milestone["scope"] != expected_scope:
            raise DiscoveryValidationError(
                f"candidate milestone {index} has scope "
                f"{raw_milestone['scope']!r}; expected {expected_scope!r}",
                category="milestone_scope_mismatch",
            )
        normalized_milestones.append({
            "milestone_id": (
                f"milestone:{request.venue_id}:{request.year}:{index + 1:03d}"),
            "milestone_type": raw_milestone["milestone_type"],
            "scope": raw_milestone["scope"],
            "date": raw_milestone["date"],
            "evidence_urls": list(dict.fromkeys(evidence_urls)),
            "source_type": raw_milestone["source_type"],
        })

    supported_kinds = {claim["claim_kind"] for claim in normalized_claims}
    for status_field, claim_kind in _STATUS_CLAIM_KINDS.items():
        if body[status_field] != "unknown" and claim_kind not in supported_kinds:
            raise DiscoveryValidationError(
                f"{status_field}={body[status_field]!r} has no "
                f"{claim_kind!r} supporting claim",
                category="unsupported_status",
            )
    if (isinstance(body["uncertainties"], list)
            and body["uncertainties"]
            and isinstance(body["confidence"], (int, float))
            and body["confidence"] >= 1):
        raise DiscoveryValidationError(
            "confidence cannot be 1 when uncertainties are reported",
            category="confidence_inconsistent",
        )

    conference_end_dates = [
        date.fromisoformat(milestone["date"])
        for milestone in normalized_milestones
        if milestone["milestone_type"] == "conference_end"
        and milestone["scope"] == "conference"
    ]
    if conference_end_dates and max(conference_end_dates) < checked_at.date():
        body["conference_status"] = "ended"

    evidence = {
        "schema_version": SCHEMA_VERSION,
        "venue_id": request.venue_id,
        "year": request.year,
        "provider": provider.name,
        "model": provider.model,
        "prompt_version": provider.prompt_version,
        "conference_status": body["conference_status"],
        "paper_list_status": body["paper_list_status"],
        "metadata_status": body["metadata_status"],
        "pdf_status": body["pdf_status"],
        "proceedings_status": body["proceedings_status"],
        "claims": normalized_claims,
        "candidate_milestones": normalized_milestones,
        "confidence": body["confidence"],
        "uncertainties": body["uncertainties"],
        "grounding_sources": [asdict(source) for source in sources.values()],
        "search_queries": list(response.search_queries),
    }
    evidence_fingerprint = artifact_fingerprint(evidence)
    result = {
        "schema_version": SCHEMA_VERSION,
        "discovery_id": (
            f"discovery:{request.venue_id}:{request.year}:"
            f"{evidence_fingerprint[:16]}"),
        "venue_id": request.venue_id,
        "year": request.year,
        "checked_at": format_datetime(checked_at),
        "provider": provider.name,
        "model": provider.model,
        "prompt_version": provider.prompt_version,
        "conference_status": body["conference_status"],
        "paper_list_status": body["paper_list_status"],
        "metadata_status": body["metadata_status"],
        "pdf_status": body["pdf_status"],
        "proceedings_status": body["proceedings_status"],
        "claims": normalized_claims,
        "candidate_milestones": normalized_milestones,
        "confidence": body["confidence"],
        "uncertainties": body["uncertainties"],
        "evidence_fingerprint": evidence_fingerprint,
    }
    assert_secret_free(result)
    validate_contract(ContractName.DISCOVERY_RESULT, result)
    return result


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_object(path: Path, *, default: Mapping[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return deepcopy(dict(default))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryStorageError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DiscoveryStorageError(f"{path} must contain a JSON object")
    return payload


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    try:
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DiscoveryStorageError(f"cannot write {path}: {exc}") from exc


class ArtifactStore:
    """Immutable discovery evidence plus an atomic, expiring cache index."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._index_path = self.root / "cache" / "index.v1.json"
        self._lock_path = self.root / ".artifact.lock"

    def lookup(
        self,
        request_fingerprint: str,
        now: datetime,
        max_age: timedelta,
    ) -> StoredDiscovery | None:
        """Return a fresh, valid cached artifact or ``None``."""
        with _exclusive_lock(self._lock_path):
            index = _read_object(
                self._index_path, default={"version": 1, "entries": {}})
            if index.get("version") != 1 or not isinstance(
                    index.get("entries"), dict):
                raise DiscoveryStorageError("discovery cache index is invalid")
            entry = index["entries"].get(request_fingerprint)
            if entry is None:
                return None
            if not isinstance(entry, dict):
                raise DiscoveryStorageError("discovery cache entry is invalid")
            cached_at = parse_datetime(entry.get("cached_at", ""))
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("now must include a timezone")
            resolved_now = now.astimezone(timezone.utc)
            if cached_at > resolved_now or resolved_now - cached_at > max_age:
                return None
            relative = entry.get("artifact_path")
            if not isinstance(relative, str):
                raise DiscoveryStorageError("cache artifact path is invalid")
            artifact_path = (self.root / relative).resolve()
            try:
                artifact_path.relative_to(self.root.resolve())
            except ValueError as exc:
                raise DiscoveryStorageError(
                    "cache artifact path escapes discovery root") from exc
            artifact = _read_object(artifact_path, default={})
            result = artifact.get("result")
            if not isinstance(result, dict):
                raise DiscoveryStorageError("cached artifact has no result")
            validate_contract(ContractName.DISCOVERY_RESULT, result)
            return StoredDiscovery(
                result=result,
                artifact_path=artifact_path,
                cache_hit=True,
                provider_role=entry.get("provider_role", "primary"),
            )

    def retain(
        self,
        request_fingerprint: str,
        result: Mapping[str, Any],
        response: ProviderResponse,
        provider_role: str,
        cached_at: datetime,
    ) -> StoredDiscovery:
        """Write a new immutable artifact or reuse identical retained evidence."""
        validate_contract(ContractName.DISCOVERY_RESULT, result)
        evidence_fingerprint = result["evidence_fingerprint"]
        relative = Path("artifacts") / result["provider"] / result["venue_id"] / (
            f"{result['year']}-{evidence_fingerprint}.json")
        artifact_path = self.root / relative
        artifact = {
            "artifact_version": 1,
            "request_fingerprint": request_fingerprint,
            "provider_role": provider_role,
            "result": deepcopy(dict(result)),
            "grounding": {
                "sources": [asdict(source) for source in response.grounding_sources],
                "search_queries": list(response.search_queries),
            },
        }
        assert_secret_free(artifact)
        with _exclusive_lock(self._lock_path):
            retained_result = deepcopy(dict(result))
            if artifact_path.exists():
                existing = _read_object(artifact_path, default={})
                existing_result = existing.get("result")
                if (not isinstance(existing_result, dict)
                        or existing_result.get("evidence_fingerprint")
                        != evidence_fingerprint):
                    raise DiscoveryStorageError(
                        "immutable discovery artifact conflicts with existing data")
                validate_contract(ContractName.DISCOVERY_RESULT, existing_result)
                retained_result = existing_result
            else:
                _atomic_write(artifact_path, artifact)

            index = _read_object(
                self._index_path, default={"version": 1, "entries": {}})
            if index.get("version") != 1 or not isinstance(
                    index.get("entries"), dict):
                raise DiscoveryStorageError("discovery cache index is invalid")
            index["entries"][request_fingerprint] = {
                "artifact_path": str(relative),
                "cached_at": format_datetime(cached_at),
                "provider_role": provider_role,
                "evidence_fingerprint": evidence_fingerprint,
            }
            _atomic_write(self._index_path, index)
        return StoredDiscovery(
            result=retained_result,
            artifact_path=artifact_path.resolve(),
            cache_hit=False,
            provider_role=provider_role,
        )

    def retain_error(
        self,
        request: DiscoveryRequest,
        provider: DiscoveryProvider,
        provider_role: str,
        occurred_at: datetime,
        error: DiscoveryError,
    ) -> Path:
        """Retain a secret-free error fingerprint without provider text."""
        fingerprint = artifact_fingerprint({
            "venue_id": request.venue_id,
            "year": request.year,
            "provider": provider.name,
            "model": provider.model,
            "provider_role": provider_role,
            "error_type": type(error).__name__,
        })
        payload = {
            "artifact_version": 1,
            "venue_id": request.venue_id,
            "year": request.year,
            "provider": provider.name,
            "model": provider.model,
            "provider_role": provider_role,
            "occurred_at": format_datetime(occurred_at),
            "error_type": type(error).__name__,
            "error_category": safe_error_summary(error),
            "error_fingerprint": fingerprint,
        }
        if isinstance(error, ProviderError) and error.diagnostics:
            payload["diagnostics"] = deepcopy(error.diagnostics)
        assert_secret_free(payload)
        path = self.root / "errors" / request.venue_id / (
            f"{request.year}-{format_datetime(occurred_at).replace(':', '')}-"
            f"{fingerprint[:16]}.json")
        with _exclusive_lock(self._lock_path):
            if not path.exists():
                _atomic_write(path, payload)
        return path.resolve()


class JsonBudgetLedger:
    """Process-safe daily attempt reservations in a local JSON ledger."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def reserve(
        self,
        request: DiscoveryRequest,
        provider_role: str,
        limits: BudgetLimits,
        at: datetime,
    ) -> str:
        """Reserve one remote attempt before I/O, or fail without calling."""
        return self.reserve_many(
            request, provider_role, limits, at, count=1)[0]

    def reserve_many(
        self,
        request: DiscoveryRequest,
        provider_role: str,
        limits: BudgetLimits,
        at: datetime,
        *,
        count: int,
    ) -> tuple[str, ...]:
        """Atomically reserve all remote calls required by one provider run."""
        if provider_role not in {"primary", "secondary"}:
            raise ValueError(f"unknown provider role: {provider_role}")
        if count < 1:
            raise ValueError("reservation count must be positive")
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("budget reservation time must include a timezone")
        resolved_at = at.astimezone(timezone.utc)
        day = resolved_at.date().isoformat()
        with _exclusive_lock(self._lock_path):
            ledger = _read_object(
                self.path, default={"version": 1, "attempts": []})
            if ledger.get("version") != 1 or not isinstance(
                    ledger.get("attempts"), list):
                raise DiscoveryStorageError("discovery budget ledger is invalid")
            today = [
                attempt for attempt in ledger["attempts"]
                if isinstance(attempt, dict) and attempt.get("day") == day
            ]
            if len(today) + count > limits.max_calls_per_day:
                raise BudgetExceeded("global daily discovery budget exhausted")
            venue_attempts = [
                attempt for attempt in today
                if attempt.get("venue_id") == request.venue_id
            ]
            if (len(venue_attempts) + count
                    > limits.max_calls_per_venue_per_day):
                raise BudgetExceeded(
                    f"daily discovery budget exhausted for {request.venue_id}")
            if provider_role == "secondary":
                secondary = [
                    attempt for attempt in today
                    if attempt.get("provider_role") == "secondary"
                ]
                if (len(secondary) + count
                        > limits.max_second_provider_calls_per_day):
                    raise BudgetExceeded(
                        "daily second-provider discovery budget exhausted")
            attempt_ids = tuple(
                f"attempt:{uuid.uuid4().hex}" for _ in range(count))
            for attempt_id in attempt_ids:
                ledger["attempts"].append({
                    "attempt_id": attempt_id,
                    "day": day,
                    "at": format_datetime(resolved_at),
                    "venue_id": request.venue_id,
                    "provider_role": provider_role,
                })
            _atomic_write(self.path, ledger)
        return attempt_ids

    def attempts_for_day(self, day: date) -> list[dict[str, Any]]:
        """Return a defensive copy of reservations for tests and operations."""
        with _exclusive_lock(self._lock_path):
            ledger = _read_object(
                self.path, default={"version": 1, "attempts": []})
            if ledger.get("version") != 1 or not isinstance(
                    ledger.get("attempts"), list):
                raise DiscoveryStorageError("discovery budget ledger is invalid")
            return deepcopy([
                attempt for attempt in ledger["attempts"]
                if isinstance(attempt, dict)
                and attempt.get("day") == day.isoformat()
            ])


class DiscoveryService:
    """Coordinate cache, budgets, provider calls, validation, and escalation."""

    def __init__(
        self,
        primary_provider: DiscoveryProvider,
        artifact_store: ArtifactStore,
        budget_ledger: JsonBudgetLedger | None,
        limits: BudgetLimits | None,
        *,
        secondary_provider: DiscoveryProvider | None = None,
        escalation_policy: EscalationPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
        cache_max_age: timedelta = timedelta(hours=24),
        max_retries: int = 0,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if cache_max_age < timedelta(0):
            raise ValueError("cache_max_age cannot be negative")
        if budget_ledger is not None and limits is None:
            raise ValueError("budget limits are required with a budget ledger")
        self.primary_provider = primary_provider
        self.secondary_provider = secondary_provider
        self.artifact_store = artifact_store
        self.budget_ledger = budget_ledger
        self.limits = limits
        self.escalation_policy = escalation_policy or LowConfidenceEscalation()
        self.clock = clock
        self.cache_max_age = cache_max_age
        self.max_retries = max_retries
        self._semaphore = (
            threading.BoundedSemaphore(limits.max_concurrency)
            if limits is not None else nullcontext()
        )

    def _discover_one(
        self,
        request: DiscoveryRequest,
        provider: DiscoveryProvider,
        provider_role: str,
        *,
        force: bool,
    ) -> StoredDiscovery:
        request_fingerprint = discovery_request_fingerprint(request, provider)
        now = self.clock()
        if not force:
            cached = self.artifact_store.lookup(
                request_fingerprint, now, self.cache_max_age)
            if cached is not None:
                return StoredDiscovery(
                    result=cached.result,
                    artifact_path=cached.artifact_path,
                    cache_hit=True,
                    provider_role=provider_role,
                )

        last_error: RetryableProviderError | None = None
        for attempt_number in range(self.max_retries + 1):
            attempt_at = self.clock()
            attempt_cost = getattr(provider, "attempt_cost", 1)
            if not isinstance(attempt_cost, int) or attempt_cost < 1:
                raise DiscoveryValidationError(
                    "provider attempt_cost must be a positive integer")
            if self.budget_ledger is not None:
                assert self.limits is not None
                self.budget_ledger.reserve_many(
                    request,
                    provider_role,
                    self.limits,
                    attempt_at,
                    count=attempt_cost,
                )
            try:
                with self._semaphore:
                    response = provider.discover(request)
            except RetryableProviderError as exc:
                self.artifact_store.retain_error(
                    request, provider, provider_role, attempt_at, exc)
                last_error = exc
                if attempt_number < self.max_retries:
                    continue
                raise
            except ProviderError as exc:
                self.artifact_store.retain_error(
                    request, provider, provider_role, attempt_at, exc)
                raise
            try:
                result = normalize_provider_response(
                    request, provider, response, self.clock())
            except DiscoveryValidationError as exc:
                self.artifact_store.retain_error(
                    request, provider, provider_role, attempt_at, exc)
                raise
            except ContractValidationError as exc:
                wrapped = DiscoveryValidationError(
                    "provider output failed the discovery-result contract",
                    category="contract_rejected",
                )
                self.artifact_store.retain_error(
                    request, provider, provider_role, attempt_at, wrapped)
                raise wrapped from exc
            return self.artifact_store.retain(
                request_fingerprint,
                result,
                response,
                provider_role,
                self.clock(),
            )
        if last_error is not None:  # pragma: no cover - loop always raises
            raise last_error
        raise DiscoveryError("provider attempt loop produced no result")

    def discover(
        self,
        request: DiscoveryRequest,
        *,
        force: bool = False,
    ) -> DiscoveryOutcome:
        """Run shadow discovery without state transitions or executable action."""
        primary = self._discover_one(
            request, self.primary_provider, "primary", force=force)
        escalate = self.escalation_policy.should_escalate(primary.result)
        if not escalate:
            return DiscoveryOutcome(primary, None, False, None)
        if self.secondary_provider is None:
            return DiscoveryOutcome(
                primary, None, True, "second_provider_not_configured")
        try:
            secondary = self._discover_one(
                request, self.secondary_provider, "secondary", force=force)
        except BudgetExceeded:
            return DiscoveryOutcome(
                primary, None, True, "second_provider_budget_exhausted")
        return DiscoveryOutcome(primary, secondary, True, None)
