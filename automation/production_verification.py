"""P2.7: guarded automatic deterministic verification (fixture/fake-only).

The concrete effect in this module is production-capable but deliberately
uninstalled.  It converts a separately reviewed, non-shadow crawl-policy
artifact into the existing runtime ``CrawlPolicyGate`` contract, coordinates
the accepted HTML/PDF verifiers, and places a restart-durable cooldown in
front of every policy-authorized source request.  Tests inject fake fetchers;
no test or import in this module initiates a network request by itself.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from automation.configuration import load_policy_config, load_venue_catalog
from automation.contracts import (
    ContractName,
    artifact_fingerprint,
    validate_contract,
)
from automation.discovery import (
    _atomic_write,
    _exclusive_lock,
    _read_object,
    format_datetime,
    parse_datetime,
)
from automation.domain import Permission, assert_secret_free
from automation.html_verification import (
    COLT_PMLR_VOLUME_PROFILE,
    HtmlEvidence,
    HtmlVerificationError,
    HtmlVerificationProfile,
    RedirectChainError,
    analyze_html,
    extract_pmlr_pdf_urls,
    fetch_html_evidence,
    verify_html_evidence,
)
from automation.grounding_resolution import is_known_colt_pmlr_volume
from automation.live_fetch import LiveHttpFetcher
from automation.local_control_plane import VerificationBundle
from automation.pdf_verification import (
    PdfRedirectError,
    build_pdf_sample_plan,
    fetch_pdf_evidence,
    verify_pdf_evidence,
)
from automation.verification import (
    CrawlPolicyError,
    CrawlPolicyGate,
    EvidenceFetcher,
    FetchBoundaryError,
    FetchRequest,
    FetchResponse,
    FileSnapshotStore,
    build_verification_request,
    validate_verification_result,
)


PRODUCTION_CRAWL_POLICY_PATH = (
    Path(__file__).with_name("config") / "production_crawl_policy.v1.json"
)
HEALTH_LEDGER_VERSION = 1
MAX_HEALTH_ENTRIES = 2_000
MAX_RETRY_AFTER_SECONDS = 7 * 24 * 60 * 60
_HTML_KINDS = frozenset({
    "source_identity",
    "conference_milestone",
    "paper_list",
    "metadata",
    "proceedings",
})
_REVIEW_KEYS = frozenset({
    "schema_version", "shadow_only", "reviewed_at", "review_basis", "domains",
})
_DOMAIN_KEYS = frozenset({
    "domain", "reviewed_at", "catalog_roles", "classification", "robots_review",
    "source_terms_review", "runtime", "retention",
})
_ROBOTS_KEYS = frozenset({"url", "outcome", "notes"})
_TERMS_KEYS = frozenset({"urls", "outcome", "notes"})
_RUNTIME_KEYS = frozenset({
    "allowed_permissions", "user_agent_contact", "max_concurrency",
    "minimum_delay_seconds", "jitter_seconds", "max_requests_per_run",
    "honor_retry_after", "stop_statuses", "stop_on_captcha", "api_preferred",
    "redirect_handling", "cache_policy", "resume_policy",
})
_RETENTION_KEYS = frozenset({
    "store_metadata_snapshot", "store_pdf_internal_copy",
    "redistribute_metadata", "redistribute_pdf",
})
_CATALOG_ROLES = frozenset({"official", "archival", "grounding_redirect"})
_PERMISSIONS = frozenset(permission.value for permission in Permission)
_ROBOTS_OUTCOMES = frozenset({
    "allow_with_constraints", "not_found", "agent_not_covered", "disallow",
})
_TERMS_OUTCOMES = frozenset({
    "restrictive", "licensed_with_attribution", "no_automated_access_terms_found",
    "copyright_retained", "public_access_with_limits",
    "not_applicable_while_denied",
})


class AutomaticVerificationError(RuntimeError):
    """Base class for P2.7 configuration and durable-guard failures."""


class ProductionCrawlPolicyError(AutomaticVerificationError):
    """The reviewed production crawl-policy artifact is incomplete or stale."""


class AutomaticVerificationLedgerError(AutomaticVerificationError):
    """The automatic-verification health ledger is unsafe or corrupt."""


class AutomaticVerificationRefused(FetchBoundaryError):
    """A durable source cooldown refused a request before transport I/O."""

    def __init__(self, message: str, *, reason: str, retry_at: datetime) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_at = retry_at


class AutomaticVerificationFetchError(FetchBoundaryError):
    """A typed transport or server stop retained in the health ledger."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        status_code: int | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retry_at = retry_at


def _utc(value: datetime, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _read_review(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionCrawlPolicyError(
            "cannot read the production crawl-policy review"
        ) from exc
    if not isinstance(payload, dict):
        raise ProductionCrawlPolicyError(
            "production crawl-policy review must be an object"
        )
    return payload


def _review_date(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ProductionCrawlPolicyError(f"{field} must use YYYY-MM-DD")
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ProductionCrawlPolicyError(
            f"{field} must use YYYY-MM-DD"
        ) from exc


def _public_https_url(value: Any, *, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ProductionCrawlPolicyError(f"{field} must be a bounded URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise ProductionCrawlPolicyError(
            f"{field} must be a public credential-free HTTPS URL"
        )


def _expected_roles(catalog: Mapping[str, Any]) -> dict[str, set[str]]:
    roles: dict[str, set[str]] = {}
    for venue in catalog["venues"]:
        for domain in venue["official_domains"]:
            roles.setdefault(domain, set()).add("official")
        for domain in venue["archival_domains"]:
            roles.setdefault(domain, set()).add("archival")
    roles["vertexaisearch.cloud.google.com"] = {"grounding_redirect"}
    return roles


def _runtime_entry(item: Mapping[str, Any]) -> dict[str, Any]:
    runtime = item["runtime"]
    return {
        "domain": item["domain"],
        "classification": item["classification"],
        "allowed_permissions": list(runtime["allowed_permissions"]),
        "max_concurrency": runtime["max_concurrency"],
        "minimum_delay_seconds": runtime["minimum_delay_seconds"],
        "jitter_seconds": runtime["jitter_seconds"],
        "max_requests_per_run": runtime["max_requests_per_run"],
        "honor_retry_after": runtime["honor_retry_after"],
        "stop_statuses": list(runtime["stop_statuses"]),
        "stop_on_captcha": runtime["stop_on_captcha"],
        "api_preferred": runtime["api_preferred"],
        "user_agent_contact": runtime["user_agent_contact"],
    }


def load_production_crawl_policy(
    path: Path = PRODUCTION_CRAWL_POLICY_PATH,
    *,
    observed_at: datetime,
    base_policy: Mapping[str, Any] | None = None,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate dated review evidence and return the narrow runtime policy."""
    observed = _utc(observed_at, field="observed_at")
    review = _read_review(Path(path))
    if set(review) != _REVIEW_KEYS:
        raise ProductionCrawlPolicyError(
            "production crawl-policy review has missing or unknown fields"
        )
    if review["schema_version"] != 1 or review["shadow_only"] is not False:
        raise ProductionCrawlPolicyError(
            "production crawl-policy review must be non-shadow version 1"
        )
    if not isinstance(review["review_basis"], str) or not review["review_basis"]:
        raise ProductionCrawlPolicyError("production review basis is required")

    resolved_policy = deepcopy(dict(
        base_policy if base_policy is not None else load_policy_config()
    ))
    validate_contract(ContractName.POLICY_CONFIG, resolved_policy)
    automatic = resolved_policy.get("automatic_verification")
    if not isinstance(automatic, Mapping):
        raise ProductionCrawlPolicyError(
            "policy is missing automatic_verification configuration"
        )
    reviewed = _review_date(review["reviewed_at"], field="reviewed_at")
    age = observed.date() - reviewed.date()
    if age.days < 0 or age.days > automatic["max_policy_review_age_days"]:
        raise ProductionCrawlPolicyError(
            "production crawl-policy review is future-dated or stale"
        )

    resolved_catalog = deepcopy(dict(
        catalog if catalog is not None else load_venue_catalog()
    ))
    validate_contract(ContractName.VENUE_CATALOG, resolved_catalog)
    expected_roles = _expected_roles(resolved_catalog)
    domains = review["domains"]
    if not isinstance(domains, list) or not domains:
        raise ProductionCrawlPolicyError("production review domains are required")
    seen: set[str] = set()
    runtime_domains: list[dict[str, Any]] = []
    for item in domains:
        if not isinstance(item, dict) or set(item) != _DOMAIN_KEYS:
            raise ProductionCrawlPolicyError(
                "production domain review has missing or unknown fields"
            )
        domain = item["domain"]
        if not isinstance(domain, str) or domain in seen:
            raise ProductionCrawlPolicyError(
                "production review domains must be unique strings"
            )
        seen.add(domain)
        if _review_date(
            item["reviewed_at"], field=f"{domain} reviewed_at"
        ) != reviewed:
            raise ProductionCrawlPolicyError(
                f"production review date disagrees for {domain}"
            )
        roles = item["catalog_roles"]
        if (
            not isinstance(roles, list)
            or not roles
            or len(roles) != len(set(roles))
            or not set(roles).issubset(_CATALOG_ROLES)
            or set(roles) != expected_roles.get(domain)
        ):
            raise ProductionCrawlPolicyError(
                f"production trust roles are invalid for {domain}"
            )
        classification = item["classification"]
        if classification not in {"approved", "review_required", "denied"}:
            raise ProductionCrawlPolicyError(
                f"production classification is invalid for {domain}"
            )
        robots = item["robots_review"]
        terms = item["source_terms_review"]
        runtime = item["runtime"]
        retention = item["retention"]
        if not isinstance(robots, dict) or set(robots) != _ROBOTS_KEYS:
            raise ProductionCrawlPolicyError(f"robots review is invalid for {domain}")
        if not isinstance(terms, dict) or set(terms) != _TERMS_KEYS:
            raise ProductionCrawlPolicyError(f"terms review is invalid for {domain}")
        if not isinstance(runtime, dict) or set(runtime) != _RUNTIME_KEYS:
            raise ProductionCrawlPolicyError(f"runtime review is invalid for {domain}")
        if not isinstance(retention, dict) or set(retention) != _RETENTION_KEYS:
            raise ProductionCrawlPolicyError(f"retention review is invalid for {domain}")
        _public_https_url(robots["url"], field=f"{domain} robots URL")
        if robots["outcome"] not in _ROBOTS_OUTCOMES:
            raise ProductionCrawlPolicyError(f"robots outcome is invalid for {domain}")
        if not isinstance(robots["notes"], str) or not robots["notes"]:
            raise ProductionCrawlPolicyError(f"robots notes are missing for {domain}")
        if not isinstance(terms["urls"], list) or not terms["urls"]:
            raise ProductionCrawlPolicyError(f"terms URLs are missing for {domain}")
        for index, url in enumerate(terms["urls"]):
            _public_https_url(url, field=f"{domain} terms URL {index}")
        if terms["outcome"] not in _TERMS_OUTCOMES:
            raise ProductionCrawlPolicyError(f"terms outcome is invalid for {domain}")
        if not isinstance(terms["notes"], str) or not terms["notes"]:
            raise ProductionCrawlPolicyError(f"terms notes are missing for {domain}")
        permissions = runtime["allowed_permissions"]
        if (
            not isinstance(permissions, list)
            or len(permissions) != len(set(permissions))
            or not set(permissions).issubset(_PERMISSIONS)
        ):
            raise ProductionCrawlPolicyError(f"permissions are invalid for {domain}")
        if classification != "approved" and permissions:
            raise ProductionCrawlPolicyError(
                f"closed production domain grants permission: {domain}"
            )
        if robots["outcome"] == "disallow" and classification != "denied":
            raise ProductionCrawlPolicyError(
                f"robots denial must deny production access for {domain}"
            )
        if (
            robots["outcome"] == "agent_not_covered"
            and classification == "approved"
        ):
            raise ProductionCrawlPolicyError(
                f"agent-specific robots ambiguity cannot approve {domain}"
            )
        contact = runtime["user_agent_contact"]
        if classification == "approved":
            _public_https_url(contact, field=f"{domain} User-Agent contact")
        elif contact is not None:
            raise ProductionCrawlPolicyError(
                f"closed production domain must not identify an active agent: {domain}"
            )
        if runtime["redirect_handling"] != "manual_policy_gate_each_hop":
            raise ProductionCrawlPolicyError(
                f"redirect handling is unsafe for {domain}"
            )
        if runtime["cache_policy"] not in {
            "immutable_content_addressed_snapshots", "no_fetch",
        }:
            raise ProductionCrawlPolicyError(f"cache policy is invalid for {domain}")
        if runtime["resume_policy"] not in {
            "replay_retained_then_refetch_after_cooldown", "manual_review_required",
        }:
            raise ProductionCrawlPolicyError(f"resume policy is invalid for {domain}")
        if classification != "approved" and (
            runtime["cache_policy"] != "no_fetch"
            or runtime["resume_policy"] != "manual_review_required"
        ):
            raise ProductionCrawlPolicyError(
                f"closed production domain has active cache/resume policy: {domain}"
            )
        if not all(isinstance(value, bool) for value in retention.values()):
            raise ProductionCrawlPolicyError(
                f"retention decisions must be explicit for {domain}"
            )
        if retention["redistribute_metadata"] or retention["redistribute_pdf"]:
            raise ProductionCrawlPolicyError(
                f"P2.7 cannot grant redistribution for {domain}"
            )
        if (
            retention["store_pdf_internal_copy"]
            != ({
                Permission.PDF_FETCH_FOR_PROCESSING.value,
                Permission.STORE_INTERNAL_COPY.value,
            }.issubset(permissions))
        ):
            raise ProductionCrawlPolicyError(
                f"PDF retention permissions disagree for {domain}"
            )
        if retention["store_metadata_snapshot"] != (
            Permission.METADATA_FETCH.value in permissions
        ):
            raise ProductionCrawlPolicyError(
                f"metadata retention permission disagrees for {domain}"
            )
        runtime_domains.append(_runtime_entry(item))

    if seen != set(expected_roles):
        missing = sorted(set(expected_roles) - seen)
        extra = sorted(seen - set(expected_roles))
        raise ProductionCrawlPolicyError(
            f"production review domain coverage differs: missing={missing}, extra={extra}"
        )
    resolved_policy["crawl"]["domains"] = runtime_domains
    validate_contract(ContractName.POLICY_CONFIG, resolved_policy)
    assert_secret_free(review)
    return resolved_policy


@dataclass(frozen=True)
class AutomaticVerificationGuardPolicy:
    same_source_failure_cooldown_hours: int
    max_targets_per_discovery: int
    pdf_sample_size: int

    @classmethod
    def from_policy(
        cls, policy: Mapping[str, Any]
    ) -> "AutomaticVerificationGuardPolicy":
        validate_contract(ContractName.POLICY_CONFIG, policy)
        automatic = policy.get("automatic_verification")
        if not isinstance(automatic, Mapping):
            raise ValueError(
                "policy is missing automatic_verification configuration"
            )
        return cls(
            same_source_failure_cooldown_hours=(
                automatic["same_source_failure_cooldown_hours"]
            ),
            max_targets_per_discovery=automatic["max_targets_per_discovery"],
            pdf_sample_size=automatic["pdf_sample_size"],
        )


class AutomaticVerificationHealthLedger:
    """Process-safe, restart-durable venue/source fetch cooldown state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def _default() -> dict[str, Any]:
        return {"version": HEALTH_LEDGER_VERSION, "sources": {}}

    @staticmethod
    def _key(venue_id: str, year: int, policy_domain: str) -> str:
        return artifact_fingerprint({
            "venue_id": venue_id,
            "year": year,
            "policy_domain": policy_domain,
        })

    def _load(self) -> dict[str, Any]:
        ledger = _read_object(self.path, default=self._default())
        if ledger.get("version") != HEALTH_LEDGER_VERSION:
            raise AutomaticVerificationLedgerError(
                "automatic-verification health ledger version is unsupported"
            )
        sources = ledger.get("sources")
        if not isinstance(sources, dict):
            raise AutomaticVerificationLedgerError(
                "automatic-verification health sources must be an object"
            )
        for key, entry in sources.items():
            if (
                not isinstance(key, str)
                or not isinstance(entry, dict)
                or set(entry) != {
                    "venue_id", "year", "policy_domain", "state", "observed_at",
                    "deadline_at", "failure_fingerprint", "category", "status_code",
                }
                or entry["state"] not in {"in_flight", "cooldown", "eligible"}
                or not isinstance(entry["venue_id"], str)
                or isinstance(entry["year"], bool)
                or not isinstance(entry["year"], int)
                or not isinstance(entry["policy_domain"], str)
                or (
                    entry["failure_fingerprint"] is not None
                    and not isinstance(entry["failure_fingerprint"], str)
                )
                or (
                    entry["category"] is not None
                    and not isinstance(entry["category"], str)
                )
                or (
                    entry["status_code"] is not None
                    and (
                        isinstance(entry["status_code"], bool)
                        or not isinstance(entry["status_code"], int)
                        or not 100 <= entry["status_code"] <= 599
                    )
                )
            ):
                raise AutomaticVerificationLedgerError(
                    "automatic-verification health entry is invalid"
                )
            parse_datetime(entry["observed_at"])
            if entry["deadline_at"] is not None:
                parse_datetime(entry["deadline_at"])
        return ledger

    @staticmethod
    def _bound(ledger: dict[str, Any]) -> None:
        sources = ledger["sources"]
        if len(sources) <= MAX_HEALTH_ENTRIES:
            return
        ordered = sorted(sources.items(), key=lambda item: item[1]["observed_at"])
        for key, _ in ordered[: len(sources) - MAX_HEALTH_ENTRIES]:
            del sources[key]

    def guard_and_claim(
        self,
        venue_id: str,
        year: int,
        policy_domain: str,
        *,
        at: datetime,
        policy: AutomaticVerificationGuardPolicy,
    ) -> None:
        observed = _utc(at, field="at")
        key = self._key(venue_id, year, policy_domain)
        with _exclusive_lock(self._lock_path):
            ledger = self._load()
            entry = ledger["sources"].get(key)
            if entry is not None and entry["state"] in {"in_flight", "cooldown"}:
                deadline = parse_datetime(entry["deadline_at"])
                if deadline > observed:
                    reason = (
                        "same_source_in_flight"
                        if entry["state"] == "in_flight"
                        else "same_source_cooldown"
                    )
                    raise AutomaticVerificationRefused(
                        f"automatic verification refused {policy_domain}",
                        reason=reason,
                        retry_at=deadline,
                    )
            deadline = observed + timedelta(
                hours=policy.same_source_failure_cooldown_hours
            )
            ledger["sources"][key] = {
                "venue_id": venue_id,
                "year": year,
                "policy_domain": policy_domain,
                "state": "in_flight",
                "observed_at": format_datetime(observed),
                "deadline_at": format_datetime(deadline),
                "failure_fingerprint": None,
                "category": None,
                "status_code": None,
            }
            self._bound(ledger)
            _atomic_write(self.path, ledger)

    def finalize_success(
        self, venue_id: str, year: int, policy_domain: str, *, at: datetime
    ) -> None:
        observed = _utc(at, field="at")
        key = self._key(venue_id, year, policy_domain)
        with _exclusive_lock(self._lock_path):
            ledger = self._load()
            ledger["sources"][key] = {
                "venue_id": venue_id,
                "year": year,
                "policy_domain": policy_domain,
                "state": "eligible",
                "observed_at": format_datetime(observed),
                "deadline_at": None,
                "failure_fingerprint": None,
                "category": None,
                "status_code": None,
            }
            self._bound(ledger)
            _atomic_write(self.path, ledger)

    def finalize_failure(
        self,
        venue_id: str,
        year: int,
        policy_domain: str,
        *,
        category: str,
        status_code: int | None,
        at: datetime,
        retry_at: datetime | None,
        policy: AutomaticVerificationGuardPolicy,
    ) -> datetime:
        observed = _utc(at, field="at")
        base_deadline = observed + timedelta(
            hours=policy.same_source_failure_cooldown_hours
        )
        deadline = max(
            base_deadline,
            _utc(retry_at, field="retry_at") if retry_at is not None else base_deadline,
        )
        fingerprint = artifact_fingerprint({
            "venue_id": venue_id,
            "year": year,
            "policy_domain": policy_domain,
            "category": category,
            "status_code": status_code,
        })
        key = self._key(venue_id, year, policy_domain)
        with _exclusive_lock(self._lock_path):
            ledger = self._load()
            ledger["sources"][key] = {
                "venue_id": venue_id,
                "year": year,
                "policy_domain": policy_domain,
                "state": "cooldown",
                "observed_at": format_datetime(observed),
                "deadline_at": format_datetime(deadline),
                "failure_fingerprint": fingerprint,
                "category": category,
                "status_code": status_code,
            }
            self._bound(ledger)
            _atomic_write(self.path, ledger)
        return deadline

    def source_state(
        self, venue_id: str, year: int, policy_domain: str
    ) -> Mapping[str, Any] | None:
        key = self._key(venue_id, year, policy_domain)
        with _exclusive_lock(self._lock_path):
            entry = self._load()["sources"].get(key)
        return dict(entry) if entry is not None else None


def _retry_after(value: str | None, observed_at: datetime) -> datetime | None:
    if value is None:
        return None
    stripped = value.strip()
    try:
        seconds = int(stripped)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        candidate = parsed.astimezone(timezone.utc)
    else:
        if seconds < 0:
            return None
        candidate = observed_at + timedelta(
            seconds=min(seconds, MAX_RETRY_AFTER_SECONDS)
        )
    maximum = observed_at + timedelta(seconds=MAX_RETRY_AFTER_SECONDS)
    return min(max(candidate, observed_at), maximum)


class _GuardedFetcher:
    """Add durable source health and stop coordination to one fetcher."""

    def __init__(
        self,
        fetcher: EvidenceFetcher,
        health: AutomaticVerificationHealthLedger,
        guard_policy: AutomaticVerificationGuardPolicy,
        *,
        venue_id: str,
        year: int,
        observed_at: datetime,
    ) -> None:
        self._fetcher = fetcher
        self._health = health
        self._guard_policy = guard_policy
        self._venue_id = venue_id
        self._year = year
        self._observed_at = observed_at
        self._stopped: AutomaticVerificationFetchError | None = None

    def fetch(self, request: FetchRequest) -> FetchResponse:
        if self._stopped is not None:
            raise AutomaticVerificationFetchError(
                "automatic verification stopped after a prior source failure",
                category="run_stopped",
                status_code=self._stopped.status_code,
                retry_at=self._stopped.retry_at,
            )
        try:
            self._health.guard_and_claim(
                self._venue_id,
                self._year,
                request.policy_domain,
                at=self._observed_at,
                policy=self._guard_policy,
            )
        except AutomaticVerificationRefused as exc:
            self._stopped = AutomaticVerificationFetchError(
                "automatic verification source cooldown is active",
                category=exc.reason,
                retry_at=exc.retry_at,
            )
            raise
        try:
            response = self._fetcher.fetch(request)
        except FetchBoundaryError as exc:
            category = getattr(exc, "category", type(exc).__name__)
            deadline = self._health.finalize_failure(
                self._venue_id,
                self._year,
                request.policy_domain,
                category=category,
                status_code=getattr(exc, "status_code", None),
                at=self._observed_at,
                retry_at=getattr(exc, "retry_at", None),
                policy=self._guard_policy,
            )
            stopped = AutomaticVerificationFetchError(
                "automatic verification transport failed",
                category=category,
                status_code=getattr(exc, "status_code", None),
                retry_at=deadline,
            )
            self._stopped = stopped
            raise stopped from exc

        stop_status = response.status_code in request.stop_statuses
        server_failure = response.status_code >= 500
        if stop_status or server_failure:
            category = f"http_{response.status_code}"
            retry_at = _retry_after(
                response.headers.get("retry-after"), self._observed_at
            ) if request.honor_retry_after else None
            deadline = self._health.finalize_failure(
                self._venue_id,
                self._year,
                request.policy_domain,
                category=category,
                status_code=response.status_code,
                at=self._observed_at,
                retry_at=retry_at,
                policy=self._guard_policy,
            )
            stopped = AutomaticVerificationFetchError(
                "automatic verification stopped on an HTTP response",
                category=category,
                status_code=response.status_code,
                retry_at=deadline,
            )
            self._stopped = stopped
            raise stopped
        self._health.finalize_success(
            self._venue_id,
            self._year,
            request.policy_domain,
            at=self._observed_at,
        )
        return response


@dataclass(frozen=True)
class AutomaticVerificationConfig:
    snapshot_root: Path
    health_ledger_path: Path
    policy_review_path: Path = PRODUCTION_CRAWL_POLICY_PATH

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_root", Path(self.snapshot_root))
        object.__setattr__(self, "health_ledger_path", Path(self.health_ledger_path))
        object.__setattr__(self, "policy_review_path", Path(self.policy_review_path))


def _target_order(discovery: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    targets = [
        ("candidate_milestone", item["milestone_id"], "conference_milestone")
        for item in discovery["candidate_milestones"]
    ]
    targets.extend(
        ("claim", item["claim_id"], {
            "conference": "source_identity",
            "other": "source_identity",
            "paper_list": "paper_list",
            "metadata": "metadata",
            "proceedings": "proceedings",
            "pdf": "pdf",
        }[item.get("claim_kind", "other")])
        for item in discovery["claims"]
    )
    return sorted(targets, key=lambda item: (item[2], item[0], item[1]))


def _target_urls(
    discovery: Mapping[str, Any], target_kind: str, target_id: str
) -> tuple[str, ...]:
    collection = (
        discovery["claims"]
        if target_kind == "claim"
        else discovery["candidate_milestones"]
    )
    identity = "claim_id" if target_kind == "claim" else "milestone_id"
    return tuple(
        item["evidence_urls"] for item in collection if item[identity] == target_id
    )[0]


def _html_profile(
    *, venue_id: str, year: int, url: str
) -> HtmlVerificationProfile:
    if is_known_colt_pmlr_volume(venue_id=venue_id, year=year, url=url):
        return COLT_PMLR_VOLUME_PROFILE
    return HtmlVerificationProfile()


def _discovery_with_target_urls(
    discovery: Mapping[str, Any], target_id: str, urls: Sequence[str]
) -> dict[str, Any]:
    """Build a private verifier view without mutating retained discovery."""
    resolved = deepcopy(dict(discovery))
    for claim in resolved["claims"]:
        if claim["claim_id"] == target_id:
            claim["evidence_urls"] = list(urls)
            return resolved
    raise AutomaticVerificationError("PDF target is absent from discovery")


def _bounded_pdf_listing_sample(
    request: Mapping[str, Any],
    target_id: str,
    urls: Sequence[str],
    *,
    sample_size: int,
) -> tuple[str, ...]:
    """Apply P2.3's stable ranking before building a contract-valid view."""
    return tuple(sorted(
        urls,
        key=lambda url: (
            hashlib.sha256(
                (
                    request["request_id"]
                    + "\0"
                    + target_id
                    + "\0"
                    + url
                ).encode("utf-8")
            ).hexdigest(),
            url,
        ),
    )[:sample_size])


class ProductionVerificationEffect:
    """Uninstalled production-capable implementation of ``VerificationEffect``."""

    def __init__(
        self,
        config: AutomaticVerificationConfig,
        *,
        _fetcher: EvidenceFetcher | None = None,
        _base_policy: Mapping[str, Any] | None = None,
        _catalog: Mapping[str, Any] | None = None,
    ) -> None:
        self._config = config
        self._fetcher = _fetcher if _fetcher is not None else LiveHttpFetcher()
        self._base_policy = deepcopy(dict(
            _base_policy if _base_policy is not None else load_policy_config()
        ))
        self._catalog = deepcopy(dict(
            _catalog if _catalog is not None else load_venue_catalog()
        ))
        self._health = AutomaticVerificationHealthLedger(
            config.health_ledger_path
        )

    def verify(
        self,
        discovery: Mapping[str, Any],
        *,
        observed_at: datetime,
    ) -> Sequence[VerificationBundle]:
        """Return bounded strict bundles without reduction or action effects."""
        observed = _utc(observed_at, field="observed_at")
        payload = deepcopy(dict(discovery))
        validate_contract(ContractName.DISCOVERY_RESULT, payload)
        if parse_datetime(payload["checked_at"]) > observed:
            raise AutomaticVerificationError(
                "automatic verification cannot consume future discovery evidence"
            )
        policy = load_production_crawl_policy(
            self._config.policy_review_path,
            observed_at=observed,
            base_policy=self._base_policy,
            catalog=self._catalog,
        )
        guard_policy = AutomaticVerificationGuardPolicy.from_policy(policy)
        selected = _target_order(payload)[: guard_policy.max_targets_per_discovery]
        if not selected:
            raise AutomaticVerificationError(
                "discovery result contains no deterministic verification target"
            )
        gate = CrawlPolicyGate(policy)
        store = FileSnapshotStore(self._config.snapshot_root)
        guarded = _GuardedFetcher(
            self._fetcher,
            self._health,
            guard_policy,
            venue_id=payload["venue_id"],
            year=payload["year"],
            observed_at=observed,
        )
        bundles: list[VerificationBundle] = []
        for target_kind, target_id, verification_kind in selected:
            request = build_verification_request(
                payload,
                requested_at=observed,
                claim_ids=[target_id] if target_kind == "claim" else [],
                candidate_milestone_ids=(
                    [target_id] if target_kind == "candidate_milestone" else []
                ),
            )
            if verification_kind == "pdf":
                evidence = []
                verification_discovery = payload
                target_urls = _target_urls(payload, target_kind, target_id)
                listing_shape = (
                    len(target_urls) == 1
                    and is_known_colt_pmlr_volume(
                        venue_id=payload["venue_id"],
                        year=payload["year"],
                        url=target_urls[0],
                    )
                )
                listing_resolved = False
                if listing_shape:
                    try:
                        listing = fetch_html_evidence(
                            gate=gate,
                            fetcher=guarded,
                            snapshot_store=store,
                            catalog=self._catalog,
                            venue_id=payload["venue_id"],
                            year=payload["year"],
                            discovery_id=payload["discovery_id"],
                            initial_url=target_urls[0],
                        )
                        analysis = analyze_html(
                            listing.final_hop.response,
                            catalog=self._catalog,
                            venue_id=payload["venue_id"],
                            year=payload["year"],
                            profile=COLT_PMLR_VOLUME_PROFILE,
                        )
                        if (
                            not analysis.identity_matches
                            or analysis.paper_count is None
                            or not (
                                COLT_PMLR_VOLUME_PROFILE.minimum_paper_count
                                <= analysis.paper_count
                                <= (
                                    COLT_PMLR_VOLUME_PROFILE.maximum_paper_count
                                    or analysis.paper_count
                                )
                            )
                        ):
                            raise AutomaticVerificationError(
                                "COLT/PMLR listing identity or count is unsupported"
                            )
                        pdf_urls = extract_pmlr_pdf_urls(
                            listing.final_hop.response,
                            minimum_count=(
                                COLT_PMLR_VOLUME_PROFILE.minimum_paper_count
                            ),
                            maximum_count=(
                                COLT_PMLR_VOLUME_PROFILE.maximum_paper_count
                                or 500
                            ),
                        )
                        verification_discovery = _discovery_with_target_urls(
                            payload,
                            target_id,
                            _bounded_pdf_listing_sample(
                                request,
                                target_id,
                                pdf_urls,
                                sample_size=guard_policy.pdf_sample_size,
                            ),
                        )
                        listing_resolved = True
                    except (
                        AutomaticVerificationError,
                        CrawlPolicyError,
                        HtmlVerificationError,
                        RedirectChainError,
                        FetchBoundaryError,
                    ):
                        pass
                sample = build_pdf_sample_plan(
                    request,
                    verification_discovery,
                    sample_size=guard_policy.pdf_sample_size,
                )[0]
                for url in (
                    sample.urls if not listing_shape or listing_resolved else ()
                ):
                    try:
                        evidence.append(fetch_pdf_evidence(
                            gate=gate,
                            fetcher=guarded,
                            snapshot_store=store,
                            catalog=self._catalog,
                            venue_id=payload["venue_id"],
                            year=payload["year"],
                            discovery_id=payload["discovery_id"],
                            initial_url=url,
                        ))
                    except (CrawlPolicyError, PdfRedirectError, FetchBoundaryError):
                        continue
                result = verify_pdf_evidence(
                    request,
                    verification_discovery,
                    catalog=self._catalog,
                    evidence=evidence,
                    verified_at=observed,
                    sample_size=guard_policy.pdf_sample_size,
                )
            elif verification_kind in _HTML_KINDS:
                html_evidence = []
                for url in _target_urls(payload, target_kind, target_id):
                    try:
                        fetched = fetch_html_evidence(
                            gate=gate,
                            fetcher=guarded,
                            snapshot_store=store,
                            catalog=self._catalog,
                            venue_id=payload["venue_id"],
                            year=payload["year"],
                            discovery_id=payload["discovery_id"],
                            initial_url=url,
                        )
                        html_evidence.append(HtmlEvidence(
                            fetched,
                            _html_profile(
                                venue_id=payload["venue_id"],
                                year=payload["year"],
                                url=url,
                            ),
                        ))
                    except (CrawlPolicyError, RedirectChainError, FetchBoundaryError):
                        continue
                result = verify_html_evidence(
                    request,
                    payload,
                    catalog=self._catalog,
                    evidence=html_evidence,
                    verified_at=observed,
                )
            else:  # pragma: no cover - request builder owns the closed mapping.
                raise AutomaticVerificationError(
                    f"unsupported verification kind: {verification_kind}"
                )
            validate_verification_result(result, request, payload)
            bundles.append(VerificationBundle(request=request, result=result))
        return tuple(bundles)
