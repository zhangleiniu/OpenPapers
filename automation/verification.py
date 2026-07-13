"""Phase 2.1 contracts and effect boundaries for evidence verification.

This module deliberately does not parse HTML, follow redirects, validate PDF
content, or mutate conference state. Later Phase 2 slices implement those
behaviors behind the interfaces defined here.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, urlparse

from automation.contracts import (
    ContractName,
    artifact_fingerprint,
    validate_contract,
)
from automation.domain import Permission, SecretBoundaryError, assert_secret_free


SCHEMA_VERSION = 1
MAX_FETCH_BYTES = 100 * 1024 * 1024
MAX_FETCH_TIMEOUT_SECONDS = 120.0
_VERIFICATION_KIND_ORDER = (
    "source_identity",
    "conference_milestone",
    "paper_list",
    "metadata",
    "proceedings",
    "pdf",
)
_CLAIM_KIND_TO_VERIFICATION_KIND = {
    "conference": "source_identity",
    "paper_list": "paper_list",
    "metadata": "metadata",
    "pdf": "pdf",
    "proceedings": "proceedings",
    "other": "source_identity",
}
_SNAPSHOT_HEADER_ALLOWLIST = frozenset({
    "content-length",
    "content-type",
    "etag",
    "last-modified",
    "retry-after",
})


class VerificationError(RuntimeError):
    """Base class for bounded verifier-foundation failures."""


class SourceClassificationError(VerificationError):
    """Raised when a source URL or catalog identity cannot be classified."""


class CrawlPolicyError(VerificationError):
    """Raised before I/O when crawl policy does not grant a request."""

    def __init__(self, decision: "CrawlDecision") -> None:
        self.decision = decision
        super().__init__(
            f"crawl policy {decision.status.value} for "
            f"{decision.domain} ({decision.permission.value})"
        )


class FetchBoundaryError(VerificationError):
    """Raised when an injected fetcher violates the one-request contract."""


class SnapshotConflictError(VerificationError):
    """Raised when immutable snapshot content or metadata would be replaced."""


class SourceTrust(str, Enum):
    OFFICIAL = "official"
    ARCHIVAL = "archival"
    UNTRUSTED = "untrusted"


class CrawlDecisionStatus(str, Enum):
    ALLOWED = "allowed"
    REVIEW_REQUIRED = "review_required"
    DENIED = "denied"
    PERMISSION_MISSING = "permission_missing"
    REQUEST_BUDGET_EXHAUSTED = "request_budget_exhausted"


@dataclass(frozen=True)
class SourceClassification:
    url: str
    domain: str
    trust: SourceTrust
    catalog_domain: str | None


@dataclass(frozen=True)
class CrawlDecision:
    url: str
    domain: str
    permission: Permission
    status: CrawlDecisionStatus
    policy_domain: str | None


@dataclass(frozen=True)
class FetchRequest:
    """One policy-authorized HTTPS request with redirect following disabled."""

    url: str
    permission: Permission
    max_bytes: int
    timeout_seconds: float
    policy_domain: str
    user_agent_contact: str
    max_concurrency: int
    minimum_delay_seconds: float
    jitter_seconds: float
    honor_retry_after: bool
    stop_statuses: tuple[int, ...]
    stop_on_captcha: bool
    api_preferred: bool
    follow_redirects: bool = False

    def __post_init__(self) -> None:
        _https_domain(self.url)
        try:
            permission = Permission(self.permission)
        except ValueError as exc:
            raise FetchBoundaryError(
                f"unknown fetch permission: {self.permission!r}") from exc
        object.__setattr__(self, "permission", permission)
        if self.max_bytes < 1:
            raise FetchBoundaryError("max_bytes must be positive")
        if self.max_bytes > MAX_FETCH_BYTES:
            raise FetchBoundaryError(
                f"max_bytes cannot exceed {MAX_FETCH_BYTES}")
        if self.timeout_seconds <= 0:
            raise FetchBoundaryError("timeout_seconds must be positive")
        if self.timeout_seconds > MAX_FETCH_TIMEOUT_SECONDS:
            raise FetchBoundaryError(
                "timeout_seconds cannot exceed "
                f"{MAX_FETCH_TIMEOUT_SECONDS:g}")
        if not self.policy_domain or not self.user_agent_contact:
            raise FetchBoundaryError(
                "authorized fetches require policy domain and contact")
        if self.max_concurrency < 1:
            raise FetchBoundaryError("max_concurrency must be positive")
        if self.minimum_delay_seconds < 0 or self.jitter_seconds < 0:
            raise FetchBoundaryError("crawl delays cannot be negative")
        if not self.honor_retry_after or not self.stop_on_captcha:
            raise FetchBoundaryError(
                "fetch requests must honor Retry-After and stop on CAPTCHA")
        if 429 not in self.stop_statuses:
            raise FetchBoundaryError("fetch requests must stop on HTTP 429")
        if self.follow_redirects:
            raise FetchBoundaryError(
                "fetchers must not auto-follow redirects; Phase 2.2 gates them")


@dataclass(frozen=True)
class FetchResponse:
    """One response to one exact request; no redirect chain is implied."""

    requested_url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    fetched_at: str

    def __post_init__(self) -> None:
        _https_domain(self.requested_url)
        if not 100 <= self.status_code <= 599:
            raise FetchBoundaryError("HTTP status must be between 100 and 599")
        if not isinstance(self.body, bytes):
            raise FetchBoundaryError("fetch response body must be bytes")
        normalized_headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise FetchBoundaryError("fetch response headers must be strings")
            normalized_headers[key.lower()] = value
        object.__setattr__(self, "headers", MappingProxyType(normalized_headers))
        _parse_datetime(self.fetched_at)


class EvidenceFetcher(Protocol):
    """Transport boundary. Implementations must perform exactly one request."""

    def fetch(self, request: FetchRequest) -> FetchResponse:
        """Fetch one URL without automatically following redirects."""


@dataclass(frozen=True)
class SnapshotProvenance:
    venue_id: str
    year: int
    discovery_id: str
    source_trust: SourceTrust
    permission: Permission
    policy_domain: str


@dataclass(frozen=True)
class SnapshotReference:
    snapshot_id: str
    content_sha256: str
    size_bytes: int
    object_path: Path
    manifest_path: Path


class SnapshotStore(Protocol):
    """Immutable source-snapshot boundary used by later content verifiers."""

    def retain(
        self,
        response: FetchResponse,
        provenance: SnapshotProvenance,
    ) -> SnapshotReference:
        """Retain one response immutably and return stable evidence identity."""


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise VerificationError(f"invalid timezone-aware datetime: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VerificationError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime | str) -> str:
    parsed = _parse_datetime(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VerificationError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _https_domain(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise SourceClassificationError("source URL must be a bounded string")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SourceClassificationError("source URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise SourceClassificationError("source URL cannot contain credentials")
    try:
        assert_secret_free({key: "" for key, _ in parse_qsl(
            parsed.query, keep_blank_values=True)})
    except SecretBoundaryError as exc:
        raise SourceClassificationError(
            "source URL cannot contain credential-shaped query fields") from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise SourceClassificationError("source URL has an invalid port") from exc
    if port not in {None, 443}:
        raise SourceClassificationError("source URL must use the HTTPS port")
    domain = parsed.hostname.lower().rstrip(".")
    if not domain or any(character.isspace() for character in domain):
        raise SourceClassificationError("source URL has an invalid hostname")
    return domain


def _domain_matches(domain: str, configured: str) -> bool:
    return domain == configured or domain.endswith(f".{configured}")


def _catalog_venue(
    catalog: Mapping[str, Any],
    venue_id: str,
) -> Mapping[str, Any]:
    validate_contract(ContractName.VENUE_CATALOG, catalog)
    for venue in catalog["venues"]:
        if venue["venue_id"] == venue_id:
            return venue
    raise SourceClassificationError(f"unknown catalog venue: {venue_id}")


def classify_source(
    catalog: Mapping[str, Any],
    venue_id: str,
    url: str,
) -> SourceClassification:
    """Classify source authority without granting crawl permission."""
    venue = _catalog_venue(catalog, venue_id)
    domain = _https_domain(url)
    official = sorted(
        (candidate for candidate in venue["official_domains"]
         if _domain_matches(domain, candidate)),
        key=len,
        reverse=True,
    )
    archival = sorted(
        (candidate for candidate in venue["archival_domains"]
         if _domain_matches(domain, candidate)),
        key=len,
        reverse=True,
    )
    if official:
        return SourceClassification(
            url, domain, SourceTrust.OFFICIAL, official[0])
    if archival:
        return SourceClassification(
            url, domain, SourceTrust.ARCHIVAL, archival[0])
    return SourceClassification(url, domain, SourceTrust.UNTRUSTED, None)


class CrawlPolicyGate:
    """Resolve crawl policy and authorize an injected fetch before I/O."""

    def __init__(self, policy: Mapping[str, Any]) -> None:
        validate_contract(ContractName.POLICY_CONFIG, policy)
        self._policy = deepcopy(dict(policy))
        self._request_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def _matching_policy(self, domain: str) -> Mapping[str, Any] | None:
        matches = [
            item for item in self._policy["crawl"]["domains"]
            if _domain_matches(domain, item["domain"])
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: len(item["domain"]))

    def decide(
        self,
        url: str,
        permission: Permission | str,
    ) -> CrawlDecision:
        """Return a side-effect-free policy decision for one exact URL."""
        domain = _https_domain(url)
        try:
            required = Permission(permission)
        except ValueError as exc:
            raise VerificationError(
                f"unknown crawl permission: {permission!r}") from exc
        selected = self._matching_policy(domain)
        if selected is None:
            return CrawlDecision(
                url, domain, required,
                CrawlDecisionStatus.REVIEW_REQUIRED, None)
        policy_domain = selected["domain"]
        if selected["classification"] == "review_required":
            status = CrawlDecisionStatus.REVIEW_REQUIRED
        elif selected["classification"] == "denied":
            status = CrawlDecisionStatus.DENIED
        elif required.value not in selected["allowed_permissions"]:
            status = CrawlDecisionStatus.PERMISSION_MISSING
        elif (self._request_counts.get(policy_domain, 0)
              >= selected["max_requests_per_run"]):
            status = CrawlDecisionStatus.REQUEST_BUDGET_EXHAUSTED
        else:
            status = CrawlDecisionStatus.ALLOWED
        return CrawlDecision(url, domain, required, status, policy_domain)

    def fetch(
        self,
        fetcher: EvidenceFetcher,
        *,
        url: str,
        permission: Permission | str,
        max_bytes: int,
        timeout_seconds: float,
    ) -> tuple[FetchResponse, CrawlDecision]:
        """Authorize and perform one non-redirecting injected fetch."""
        with self._lock:
            decision = self.decide(url, permission)
            if decision.status is not CrawlDecisionStatus.ALLOWED:
                raise CrawlPolicyError(decision)
            assert decision.policy_domain is not None
            selected = self._matching_policy(decision.domain)
            assert selected is not None
            self._request_counts[decision.policy_domain] = (
                self._request_counts.get(decision.policy_domain, 0) + 1)

        request = FetchRequest(
            url=url,
            permission=decision.permission,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            policy_domain=decision.policy_domain,
            user_agent_contact=selected["user_agent_contact"],
            max_concurrency=selected["max_concurrency"],
            minimum_delay_seconds=selected["minimum_delay_seconds"],
            jitter_seconds=selected["jitter_seconds"],
            honor_retry_after=selected["honor_retry_after"],
            stop_statuses=tuple(selected["stop_statuses"]),
            stop_on_captcha=selected["stop_on_captcha"],
            api_preferred=selected["api_preferred"],
            follow_redirects=False,
        )
        response = fetcher.fetch(request)
        if response.requested_url != request.url:
            raise FetchBoundaryError(
                "fetcher response URL differs from the authorized request")
        if len(response.body) > request.max_bytes:
            raise FetchBoundaryError(
                "fetcher returned more bytes than the authorized limit")
        return response, decision

    def request_count(self, policy_domain: str) -> int:
        """Return the current per-run count for tests and orchestration."""
        with self._lock:
            return self._request_counts.get(policy_domain, 0)


def build_verification_request(
    discovery: Mapping[str, Any],
    *,
    requested_at: datetime | str,
    claim_ids: Sequence[str] | None = None,
    candidate_milestone_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a strict request referencing selected discovery evidence."""
    validate_contract(ContractName.DISCOVERY_RESULT, discovery)
    claims = {item["claim_id"]: item for item in discovery["claims"]}
    milestones = {
        item["milestone_id"]: item
        for item in discovery.get("candidate_milestones", [])
    }
    selected_claims = list(claims) if claim_ids is None else list(claim_ids)
    selected_milestones = (
        list(milestones)
        if candidate_milestone_ids is None
        else list(candidate_milestone_ids)
    )
    if len(set(selected_claims)) != len(selected_claims):
        raise VerificationError("claim IDs must be unique")
    if len(set(selected_milestones)) != len(selected_milestones):
        raise VerificationError("candidate milestone IDs must be unique")
    unknown_claims = sorted(set(selected_claims) - set(claims))
    unknown_milestones = sorted(set(selected_milestones) - set(milestones))
    if unknown_claims or unknown_milestones:
        raise VerificationError(
            "verification targets are absent from discovery: "
            f"claims={unknown_claims}, milestones={unknown_milestones}")
    if not selected_claims and not selected_milestones:
        raise VerificationError("verification request needs at least one target")

    kinds = {
        _CLAIM_KIND_TO_VERIFICATION_KIND[claims[claim_id].get(
            "claim_kind", "other")]
        for claim_id in selected_claims
    }
    if selected_milestones:
        kinds.add("conference_milestone")
    ordered_kinds = [kind for kind in _VERIFICATION_KIND_ORDER if kind in kinds]
    identity = {
        "schema_version": SCHEMA_VERSION,
        "discovery_id": discovery["discovery_id"],
        "discovery_evidence_fingerprint": discovery["evidence_fingerprint"],
        "venue_id": discovery["venue_id"],
        "year": discovery["year"],
        "claim_ids": sorted(selected_claims),
        "candidate_milestone_ids": sorted(selected_milestones),
        "verification_kinds": ordered_kinds,
    }
    request_fingerprint = artifact_fingerprint(identity)
    request = {
        **identity,
        "request_id": f"verify-request:{request_fingerprint[:32]}",
        "requested_at": _format_datetime(requested_at),
    }
    assert_secret_free(request)
    validate_contract(ContractName.VERIFICATION_REQUEST, request)
    return request


def validate_request_against_discovery(
    request: Mapping[str, Any],
    discovery: Mapping[str, Any],
) -> None:
    """Reject a request whose referenced discovery identity or targets drifted."""
    validate_contract(ContractName.VERIFICATION_REQUEST, request)
    validate_contract(ContractName.DISCOVERY_RESULT, discovery)
    identity_pairs = (
        ("discovery_id", discovery["discovery_id"]),
        ("discovery_evidence_fingerprint", discovery["evidence_fingerprint"]),
        ("venue_id", discovery["venue_id"]),
        ("year", discovery["year"]),
    )
    for field, expected in identity_pairs:
        if request[field] != expected:
            raise VerificationError(
                f"verification request {field} does not match discovery")
    claim_ids = {item["claim_id"] for item in discovery["claims"]}
    milestone_ids = {
        item["milestone_id"]
        for item in discovery.get("candidate_milestones", [])
    }
    if not set(request["claim_ids"]).issubset(claim_ids):
        raise VerificationError(
            "verification request references an unknown discovery claim")
    if not set(request["candidate_milestone_ids"]).issubset(milestone_ids):
        raise VerificationError(
            "verification request references an unknown candidate milestone")


def build_verification_result(
    request: Mapping[str, Any],
    *,
    overall_status: str,
    verified_at: datetime | str,
    source_observations: Sequence[Mapping[str, Any]] = (),
    findings: Sequence[Mapping[str, Any]] = (),
    verified_facets: Mapping[str, Any] | None = None,
    verified_milestones: Sequence[Mapping[str, Any]] = (),
    uncertainties: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a strict verifier result without applying state or actions."""
    validate_contract(ContractName.VERIFICATION_REQUEST, request)
    observations = [deepcopy(dict(item)) for item in source_observations]
    finding_items = [deepcopy(dict(item)) for item in findings]
    milestones = [deepcopy(dict(item)) for item in verified_milestones]
    facets = deepcopy(dict(verified_facets)) if verified_facets is not None else {
        "conference_status": None,
        "paper_list_status": None,
        "metadata_status": None,
        "pdf_status": None,
        "proceedings_status": None,
    }
    source_ids = [item.get("source_id") for item in observations]
    if len(source_ids) != len(set(source_ids)):
        raise VerificationError("source observation IDs must be unique")
    allowed_targets = (
        set(request["claim_ids"]) | set(request["candidate_milestone_ids"])
    )
    for finding in finding_items:
        if finding.get("target_id") not in allowed_targets:
            raise VerificationError(
                "finding target is absent from the verification request")
        if finding.get("verification_kind") not in set(
                request["verification_kinds"]):
            raise VerificationError(
                "finding kind is absent from the verification request")
        if not set(finding.get("source_ids", [])).issubset(set(source_ids)):
            raise VerificationError(
                "finding references an unknown source observation")
    for milestone in milestones:
        if milestone.get("candidate_milestone_id") not in set(
                request["candidate_milestone_ids"]):
            raise VerificationError(
                "verified milestone is absent from the verification request")

    evidence = {
        "request_id": request["request_id"],
        "discovery_id": request["discovery_id"],
        "venue_id": request["venue_id"],
        "year": request["year"],
        "overall_status": overall_status,
        "source_observations": observations,
        "findings": finding_items,
        "verified_facets": facets,
        "verified_milestones": milestones,
        "uncertainties": list(uncertainties),
    }
    fingerprint = artifact_fingerprint(evidence)
    result = {
        "schema_version": SCHEMA_VERSION,
        "verification_id": f"verification:{fingerprint[:32]}",
        **evidence,
        "verified_at": _format_datetime(verified_at),
        "evidence_fingerprint": fingerprint,
    }
    assert_secret_free(result)
    validate_contract(ContractName.VERIFICATION_RESULT, result)
    return result


class FileSnapshotStore:
    """Content-addressed immutable source snapshots for local/fake execution."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @staticmethod
    def _suffix(headers: Mapping[str, str]) -> str:
        content_type = headers.get("content-type", "").split(";", 1)[0].strip()
        return {
            "application/json": ".json",
            "application/pdf": ".pdf",
            "text/html": ".html",
        }.get(content_type, ".bin")

    @staticmethod
    def _write_immutable(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise SnapshotConflictError(
                    f"immutable snapshot conflicts with {path}")
            return
        with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise SnapshotConflictError(
                        f"immutable snapshot conflicts with {path}")
        finally:
            temporary.unlink(missing_ok=True)

    def retain(
        self,
        response: FetchResponse,
        provenance: SnapshotProvenance,
    ) -> SnapshotReference:
        """Retain response bytes and an allowlisted immutable manifest."""
        if not 1900 <= provenance.year <= 2200:
            raise VerificationError("snapshot provenance year is invalid")
        source_trust = SourceTrust(provenance.source_trust)
        permission = Permission(provenance.permission)
        if not provenance.venue_id or not provenance.discovery_id:
            raise VerificationError("snapshot provenance identity is required")
        if not _domain_matches(
                _https_domain(response.requested_url), provenance.policy_domain):
            raise VerificationError(
                "snapshot URL falls outside its authorized policy domain")

        headers = {
            key: value[:1000]
            for key, value in sorted(response.headers.items())
            if key in _SNAPSHOT_HEADER_ALLOWLIST
        }
        content_sha256 = hashlib.sha256(response.body).hexdigest()
        manifest = {
            "snapshot_version": 1,
            "requested_url": response.requested_url,
            "status_code": response.status_code,
            "fetched_at": _format_datetime(response.fetched_at),
            "headers": headers,
            "content_sha256": content_sha256,
            "size_bytes": len(response.body),
            "provenance": {
                "venue_id": provenance.venue_id,
                "year": provenance.year,
                "discovery_id": provenance.discovery_id,
                "source_trust": source_trust.value,
                "permission": permission.value,
                "policy_domain": provenance.policy_domain,
            },
        }
        assert_secret_free(manifest)
        manifest_fingerprint = artifact_fingerprint(manifest)
        snapshot_id = f"snapshot:{manifest_fingerprint[:32]}"
        object_path = (
            self.root / "objects" /
            f"{content_sha256}{self._suffix(headers)}"
        )
        manifest_path = (
            self.root / "manifests" / f"{manifest_fingerprint}.json"
        )
        serialized = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        self._write_immutable(object_path, response.body)
        self._write_immutable(manifest_path, serialized)
        return SnapshotReference(
            snapshot_id=snapshot_id,
            content_sha256=content_sha256,
            size_bytes=len(response.body),
            object_path=object_path.resolve(),
            manifest_path=manifest_path.resolve(),
        )
