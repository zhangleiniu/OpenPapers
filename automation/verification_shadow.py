"""P2.S isolated live-shadow composition for deterministic verification."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from automation.configuration import load_policy_config, load_venue_catalog
from automation.contracts import ContractName, validate_contract
from automation.control_plane import consume_verification_record
from automation.control_state import ControlStateRepository
from automation.domain import assert_secret_free
from automation.html_verification import (
    ElementSelector,
    HtmlEvidence,
    HtmlVerificationProfile,
    RedirectChainError,
    fetch_html_evidence,
    verify_html_evidence,
)
from automation.pdf_verification import (
    PdfRedirectError,
    build_pdf_sample_plan,
    fetch_pdf_evidence,
    verify_pdf_evidence,
)
from automation.verification import (
    CrawlPolicyGate,
    EvidenceFetcher,
    FetchBoundaryError,
    FileSnapshotStore,
    build_verification_request,
    validate_verification_result,
)


SHADOW_POLICY_PATH = (
    Path(__file__).with_name("config") / "p2s_shadow_policy.v1.json"
)
_ROOT_MARKER = ".p2s-shadow-root.v1.json"
_HTML_KINDS = frozenset({
    "source_identity",
    "conference_milestone",
    "paper_list",
    "metadata",
    "proceedings",
})
_READINESS_ORDER = ("pdf", "proceedings", "metadata", "paper_list")
_MILESTONE_ORDER = (
    "conference_end",
    "conference_start",
    "acceptance_notification",
    "paper_list_expected",
    "proceedings_expected",
)
_POLICY_KEYS = frozenset({
    "schema_version",
    "shadow_only",
    "reviewed_at",
    "user_agent_contact",
    "review_basis",
    "defaults",
    "domains",
})
_POLICY_DEFAULT_KEYS = frozenset({
    "max_concurrency",
    "minimum_delay_seconds",
    "jitter_seconds",
    "max_requests_per_run",
    "honor_retry_after",
    "stop_statuses",
    "stop_on_captcha",
    "api_preferred",
})
_POLICY_DOMAIN_KEYS = frozenset({
    "domain",
    "classification",
    "robots_review",
    "allowed_permissions",
    "max_requests_per_run",
})
_SHADOW_PERMISSIONS = frozenset({
    "metadata_fetch",
    "pdf_fetch_for_processing",
    "store_internal_copy",
})


class ShadowVerificationError(RuntimeError):
    """The bounded P2.S shadow review cannot proceed safely."""


@dataclass(frozen=True)
class DiscoveryArtifact:
    path: Path
    result: dict[str, Any]
    source_domains: Mapping[str, str]


@dataclass(frozen=True)
class ShadowTarget:
    target_kind: str
    target_id: str
    verification_kind: str
    selected_urls: tuple[str, ...]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShadowVerificationError(f"cannot read JSON artifact: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ShadowVerificationError(f"JSON artifact is not an object: {path.name}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    assert_secret_free(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        payload,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    ) + "\n"
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ShadowVerificationError(
                f"cannot replay shadow artifact: {path.name}"
            ) from exc
        if existing != serialized:
            raise ShadowVerificationError(
                f"immutable shadow artifact conflicts: {path.name}"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_review_date(value: Any) -> None:
    if not isinstance(value, str):
        raise ShadowVerificationError("shadow policy review date must be a string")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ShadowVerificationError(
            "shadow policy review date must use YYYY-MM-DD"
        ) from exc


def load_shadow_policy(path: Path = SHADOW_POLICY_PATH) -> dict[str, Any]:
    """Load the separate P2.S-only review into the strict policy contract."""
    review = _read_json(Path(path))
    if set(review) != _POLICY_KEYS:
        raise ShadowVerificationError("shadow policy has missing or unknown fields")
    if review["schema_version"] != 1 or review["shadow_only"] is not True:
        raise ShadowVerificationError("shadow policy must be version 1 and shadow-only")
    _parse_review_date(review["reviewed_at"])
    if not isinstance(review["review_basis"], str) or not review["review_basis"]:
        raise ShadowVerificationError("shadow policy review basis is required")
    contact = review["user_agent_contact"]
    if (
        not isinstance(contact, str)
        or not contact.startswith("https://")
        or any(character in contact for character in "\r\n")
    ):
        raise ShadowVerificationError("shadow policy contact must be a safe HTTPS URL")
    defaults = review["defaults"]
    if not isinstance(defaults, dict) or set(defaults) != _POLICY_DEFAULT_KEYS:
        raise ShadowVerificationError("shadow policy defaults are invalid")
    domains = review["domains"]
    if not isinstance(domains, list) or not domains:
        raise ShadowVerificationError("shadow policy domains are required")
    configured: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in domains:
        if not isinstance(item, dict) or not set(item).issubset(
            _POLICY_DOMAIN_KEYS
        ) or not (_POLICY_DOMAIN_KEYS - {"max_requests_per_run"}).issubset(item):
            raise ShadowVerificationError("shadow policy domain entry is invalid")
        domain = item["domain"]
        if not isinstance(domain, str) or domain in seen:
            raise ShadowVerificationError("shadow policy domains must be unique strings")
        seen.add(domain)
        classification = item["classification"]
        permissions = item["allowed_permissions"]
        if classification not in {"approved", "review_required", "denied"}:
            raise ShadowVerificationError("shadow domain classification is invalid")
        if (
            not isinstance(permissions, list)
            or len(set(permissions)) != len(permissions)
            or not set(permissions).issubset(_SHADOW_PERMISSIONS)
        ):
            raise ShadowVerificationError("shadow domain permissions are invalid")
        if classification != "approved" and permissions:
            raise ShadowVerificationError("closed shadow domains cannot grant permission")
        if not isinstance(item["robots_review"], str) or not item["robots_review"]:
            raise ShadowVerificationError("shadow domain robots review is required")
        configured.append({
            "domain": domain,
            "classification": classification,
            "allowed_permissions": list(permissions),
            "max_concurrency": defaults["max_concurrency"],
            "minimum_delay_seconds": defaults["minimum_delay_seconds"],
            "jitter_seconds": defaults["jitter_seconds"],
            "max_requests_per_run": item.get(
                "max_requests_per_run", defaults["max_requests_per_run"]
            ),
            "honor_retry_after": defaults["honor_retry_after"],
            "stop_statuses": list(defaults["stop_statuses"]),
            "stop_on_captcha": defaults["stop_on_captcha"],
            "api_preferred": defaults["api_preferred"],
            "user_agent_contact": contact if classification == "approved" else None,
        })
    if "vertexaisearch.cloud.google.com" not in seen:
        raise ShadowVerificationError("shadow policy must classify grounding redirects")
    policy = load_policy_config()
    policy["crawl"]["domains"] = configured
    validate_contract(ContractName.POLICY_CONFIG, policy)
    return policy


def load_discovery_artifact(path: Path) -> DiscoveryArtifact:
    payload = _read_json(Path(path))
    result = payload.get("result")
    grounding = payload.get("grounding")
    if not isinstance(result, dict) or not isinstance(grounding, dict):
        raise ShadowVerificationError("discovery artifact lacks result or grounding")
    validate_contract(ContractName.DISCOVERY_RESULT, result)
    sources = grounding.get("sources")
    if not isinstance(sources, list):
        raise ShadowVerificationError("discovery grounding sources are invalid")
    source_domains: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise ShadowVerificationError("discovery grounding source is invalid")
        uri = source.get("uri")
        domain = source.get("domain")
        if (
            not isinstance(uri, str)
            or not isinstance(domain, str)
            or not domain
            or uri in source_domains
        ):
            raise ShadowVerificationError("grounding source identity is invalid")
        source_domains[uri] = domain.lower().rstrip(".")
    cited = {
        url
        for group in (result["claims"], result["candidate_milestones"])
        for target in group
        for url in target["evidence_urls"]
    }
    if not cited.issubset(source_domains):
        raise ShadowVerificationError("a cited URL lacks retained grounding metadata")
    assert_secret_free(payload)
    return DiscoveryArtifact(Path(path).resolve(), deepcopy(result), source_domains)


def latest_discovery_artifacts(
    root: Path,
    *,
    venue_ids: Sequence[str],
    year: int,
) -> dict[str, DiscoveryArtifact]:
    root = Path(root).resolve()
    selected: dict[str, DiscoveryArtifact] = {}
    for venue_id in venue_ids:
        candidates: list[DiscoveryArtifact] = []
        for path in root.glob(f"artifacts/*/{venue_id}/{year}-*.json"):
            artifact = load_discovery_artifact(path)
            if artifact.result["venue_id"] == venue_id and artifact.result["year"] == year:
                candidates.append(artifact)
        if not candidates:
            raise ShadowVerificationError(
                f"no retained discovery artifact for {venue_id} {year}"
            )
        selected[venue_id] = max(
            candidates,
            key=lambda item: (item.result["checked_at"], str(item.path)),
        )
    return selected


def _venue(catalog: Mapping[str, Any], venue_id: str) -> Mapping[str, Any]:
    for item in catalog["venues"]:
        if item["venue_id"] == venue_id:
            return item
    raise ShadowVerificationError(f"unknown catalog venue: {venue_id}")


def _intended_domain_allowed(
    catalog: Mapping[str, Any],
    venue_id: str,
    domain: str,
) -> bool:
    if domain.count(".") < 1:
        return False
    item = _venue(catalog, venue_id)
    configured = item["official_domains"] + item["archival_domains"]
    return any(
        domain == candidate
        or domain.endswith(f".{candidate}")
        or candidate.endswith(f".{domain}")
        for candidate in configured
    )


def _trusted_urls(
    artifact: DiscoveryArtifact,
    catalog: Mapping[str, Any],
    target: Mapping[str, Any],
) -> tuple[str, ...]:
    venue_id = artifact.result["venue_id"]
    return tuple(
        url
        for url in target["evidence_urls"]
        if _intended_domain_allowed(
            catalog, venue_id, artifact.source_domains[url]
        )
    )


def plan_shadow_targets(
    artifact: DiscoveryArtifact,
    catalog: Mapping[str, Any],
) -> tuple[ShadowTarget, ...]:
    """Choose at most one identity/milestone and one readiness observation."""
    result = artifact.result
    targets: list[ShadowTarget] = []
    milestones = sorted(
        result["candidate_milestones"],
        key=lambda item: (
            _MILESTONE_ORDER.index(item["milestone_type"])
            if item["milestone_type"] in _MILESTONE_ORDER
            else len(_MILESTONE_ORDER),
            item["milestone_id"],
        ),
    )
    for milestone in milestones:
        urls = _trusted_urls(artifact, catalog, milestone)
        if urls:
            targets.append(ShadowTarget(
                "candidate_milestone",
                milestone["milestone_id"],
                "conference_milestone",
                urls[:1],
            ))
            break
    if not targets:
        for claim in result["claims"]:
            urls = _trusted_urls(artifact, catalog, claim)
            if urls and claim["claim_kind"] in {"conference", "other"}:
                targets.append(ShadowTarget(
                    "claim",
                    claim["claim_id"],
                    "source_identity",
                    urls[:1],
                ))
                break

    for kind in _READINESS_ORDER:
        found = False
        for claim in result["claims"]:
            if claim["claim_kind"] != kind:
                continue
            urls = _trusted_urls(artifact, catalog, claim)
            if not urls:
                continue
            if kind == "pdf":
                request = build_verification_request(
                    result,
                    requested_at=result["checked_at"],
                    claim_ids=[claim["claim_id"]],
                    candidate_milestone_ids=[],
                )
                sampled = build_pdf_sample_plan(
                    request, result, sample_size=1
                )[0].urls
                urls = tuple(url for url in sampled if url in urls)
                if not urls:
                    continue
            targets.append(ShadowTarget(
                "claim", claim["claim_id"], kind, urls[:1]
            ))
            found = True
            break
        if found:
            break
    if not targets:
        raise ShadowVerificationError(
            f"no catalog-bounded shadow target for {result['venue_id']}"
        )
    return tuple(targets)


_IJCAI_PROFILE = HtmlVerificationProfile(
    paper_entry_selector=ElementSelector("li", ("ij-paper",)),
    paper_title_selector=ElementSelector("span", ("ij-ptitle",)),
    paper_author_selector=ElementSelector("span", ("ij-author",)),
    paper_abstract_selector=ElementSelector("div", ("ij-abstract",)),
    minimum_paper_count=10,
    maximum_paper_count=2_000,
)
_PMLR_LIST_PROFILE = HtmlVerificationProfile(
    paper_entry_selector=ElementSelector("div", ("paper",)),
    paper_title_selector=ElementSelector("p", ("title",)),
    paper_author_selector=ElementSelector("span", ("authors",)),
    minimum_paper_count=10,
    maximum_paper_count=10_000,
    proceedings_entry_selector=ElementSelector("div", ("paper",)),
    minimum_proceedings_count=10,
)
_ACL_PROCEEDINGS_PROFILE = HtmlVerificationProfile(
    proceedings_entry_selector=ElementSelector(
        "article", ("proceedings-volume",)
    ),
    minimum_proceedings_count=1,
)


def _profile(
    venue_id: str,
    verification_kind: str,
    intended_domain: str,
) -> HtmlVerificationProfile:
    if verification_kind in {"source_identity", "conference_milestone"}:
        return HtmlVerificationProfile()
    if venue_id == "ijcai" and verification_kind in {"paper_list", "metadata"}:
        return _IJCAI_PROFILE
    if intended_domain.endswith("mlr.press"):
        return _PMLR_LIST_PROFILE
    if intended_domain == "aclanthology.org" and verification_kind == "proceedings":
        return _ACL_PROCEEDINGS_PROFILE
    return HtmlVerificationProfile()


def _request_for_target(
    discovery: Mapping[str, Any],
    target: ShadowTarget,
    requested_at: datetime,
) -> dict[str, Any]:
    return build_verification_request(
        discovery,
        requested_at=requested_at,
        claim_ids=[target.target_id] if target.target_kind == "claim" else [],
        candidate_milestone_ids=(
            [target.target_id]
            if target.target_kind == "candidate_milestone"
            else []
        ),
    )


def _error_summary(error: BaseException) -> dict[str, Any]:
    summary: dict[str, Any] = {"error_type": type(error).__name__}
    blocked_url = getattr(error, "blocked_url", None)
    if isinstance(blocked_url, str):
        hostname = urlsplit(blocked_url).hostname
        if hostname:
            summary["blocked_domain"] = hostname.lower().rstrip(".")
    return summary


def _artifact_filename(result: Mapping[str, Any]) -> str:
    return f"{result['verification_id'].split(':')[-1]}.json"


def _validate_retained_verification(payload: Mapping[str, Any]) -> None:
    expected = {
        "artifact_version",
        "shadow_only",
        "discovery",
        "request",
        "result",
        "fetch_errors",
        "inert_actions",
    }
    if set(payload) != expected:
        raise ShadowVerificationError(
            "retained shadow verification has missing or unknown fields"
        )
    if payload["artifact_version"] != 1 or payload["shadow_only"] is not True:
        raise ShadowVerificationError(
            "retained verification is not shadow-only version 1"
        )
    if not all(
        isinstance(payload[name], dict)
        for name in ("discovery", "request", "result")
    ):
        raise ShadowVerificationError("retained verification bundle is invalid")
    if not isinstance(payload["fetch_errors"], list) or not isinstance(
        payload["inert_actions"], list
    ):
        raise ShadowVerificationError("retained shadow observations are invalid")
    validate_verification_result(
        payload["result"], payload["request"], payload["discovery"]
    )
    assert_secret_free(payload)


def _validate_completed_summary(
    summary: Mapping[str, Any],
    *,
    venue_ids: Sequence[str],
    year: int,
) -> None:
    expected = {
        "artifact_version",
        "shadow_only",
        "observed_at",
        "year",
        "venue_count",
        "venues",
        "effects",
    }
    zero_effects = {
        "jobs_created": 0,
        "scrapers_executed": 0,
        "notifications_sent": 0,
        "production_state_writes": 0,
    }
    if set(summary) != expected:
        raise ShadowVerificationError(
            "existing shadow summary has missing or unknown fields"
        )
    venues = summary["venues"]
    if (
        summary["artifact_version"] != 1
        or summary["shadow_only"] is not True
        or summary["year"] != year
        or not isinstance(venues, list)
        or summary["venue_count"] != len(venues)
        or [item.get("venue_id") for item in venues] != list(venue_ids)
        or summary["effects"] != zero_effects
    ):
        raise ShadowVerificationError(
            "existing shadow summary does not match the requested cohort"
        )
    _parse_timestamp(summary["observed_at"])
    assert_secret_free(summary)


def _result_summary(
    target: ShadowTarget,
    result: Mapping[str, Any],
    *,
    fetch_errors: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "target_kind": target.target_kind,
        "target_id": target.target_id,
        "verification_kind": target.verification_kind,
        "overall_status": result["overall_status"],
        "findings": [
            {
                "status": item["status"],
                "reason_code": item["reason_code"],
                "metrics": item["metrics"],
            }
            for item in result["findings"]
        ],
        "verified_facets": deepcopy(result["verified_facets"]),
        "verified_milestone_count": len(result["verified_milestones"]),
        "observed_domains": sorted({
            urlsplit(item["url"]).hostname
            for item in result["source_observations"]
            if urlsplit(item["url"]).hostname
        }),
        "fetch_errors": [dict(item) for item in fetch_errors],
        "inert_actions": [dict(item) for item in actions],
    }


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ShadowVerificationError("shadow marker timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShadowVerificationError("shadow marker timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShadowVerificationError("shadow marker timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def prepare_shadow_root(
    output_root: Path,
    discovery_root: Path,
    observed_at: datetime,
) -> tuple[Path, datetime]:
    output = Path(output_root).expanduser().resolve()
    discovery = Path(discovery_root).expanduser().resolve()
    if not any("shadow" in component.lower() for component in output.parts):
        raise ShadowVerificationError(
            "shadow output path must visibly contain a shadow component"
        )
    if output == discovery or output in discovery.parents or discovery in output.parents:
        raise ShadowVerificationError(
            "shadow output and discovery roots must not contain each other"
        )
    marker = output / _ROOT_MARKER
    if output.exists():
        entries = list(output.iterdir())
        if entries and not marker.is_file():
            raise ShadowVerificationError(
                "existing shadow output root lacks the P2.S marker"
            )
    else:
        output.mkdir(parents=True)
    if marker.is_file():
        payload = _read_json(marker)
        if set(payload) != {"schema_version", "shadow_only", "started_at"}:
            raise ShadowVerificationError("P2.S marker is invalid")
        if payload["schema_version"] != 1 or payload["shadow_only"] is not True:
            raise ShadowVerificationError("P2.S marker is not shadow-only version 1")
        started = _parse_timestamp(payload["started_at"])
    else:
        started = observed_at.astimezone(timezone.utc)
        _write_json(marker, {
            "schema_version": 1,
            "shadow_only": True,
            "started_at": started.isoformat().replace("+00:00", "Z"),
        })
    return output, started


def run_shadow_review(
    *,
    discovery_root: Path,
    output_root: Path,
    venue_ids: Sequence[str],
    year: int,
    fetcher: EvidenceFetcher,
    observed_at: datetime,
    policy_path: Path = SHADOW_POLICY_PATH,
) -> dict[str, Any]:
    """Run one bounded P2.S sample and return a secret-safe inert summary."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ShadowVerificationError("shadow observation time must be timezone-aware")
    if not venue_ids or len(set(venue_ids)) != len(venue_ids):
        raise ShadowVerificationError("shadow venues must be non-empty and unique")
    catalog = load_venue_catalog()
    catalog_ids = {item["venue_id"] for item in catalog["venues"]}
    unknown = set(venue_ids) - catalog_ids
    if unknown:
        raise ShadowVerificationError(f"unknown shadow venues: {sorted(unknown)}")
    policy = load_shadow_policy(Path(policy_path))
    output, observed = prepare_shadow_root(
        output_root, discovery_root, observed_at
    )
    completed_summary = output / "shadow-summary.v1.json"
    if completed_summary.is_file():
        summary = _read_json(completed_summary)
        _validate_completed_summary(summary, venue_ids=venue_ids, year=year)
        return summary
    artifacts = latest_discovery_artifacts(
        discovery_root, venue_ids=venue_ids, year=year
    )
    snapshots = FileSnapshotStore(output / "snapshots")
    gate = CrawlPolicyGate(policy)
    observed_text = observed.isoformat().replace("+00:00", "Z")
    venue_summaries: list[dict[str, Any]] = []

    with ControlStateRepository(output / "control" / "state.sqlite3") as repository:
        lease = repository.acquire_lease("p2s-shadow", ttl_seconds=86_400)
        try:
            for venue_id in venue_ids:
                artifact = artifacts[venue_id]
                target_summaries: list[dict[str, Any]] = []
                for target in plan_shadow_targets(artifact, catalog):
                    request = _request_for_target(
                        artifact.result, target, observed
                    )
                    replay = None
                    for path in (output / "verifications" / venue_id).glob("*.json"):
                        candidate = _read_json(path)
                        _validate_retained_verification(candidate)
                        if candidate.get("request", {}).get("request_id") == request["request_id"]:
                            replay = candidate
                            break
                    if replay is not None:
                        target_summaries.append(_result_summary(
                            target,
                            replay["result"],
                            fetch_errors=replay["fetch_errors"],
                            actions=replay["inert_actions"],
                        ))
                        continue
                    fetch_errors: list[dict[str, Any]] = []
                    if target.verification_kind == "pdf":
                        evidence = []
                        sample = build_pdf_sample_plan(
                            request, artifact.result, sample_size=1
                        )[0]
                        for url in sample.urls:
                            if url not in target.selected_urls:
                                fetch_errors.append({
                                    "error_type": "CitationNotSelected",
                                    "blocked_domain": artifact.source_domains[url],
                                })
                                continue
                            try:
                                evidence.append(fetch_pdf_evidence(
                                    gate=gate,
                                    fetcher=fetcher,
                                    snapshot_store=snapshots,
                                    catalog=catalog,
                                    venue_id=venue_id,
                                    year=year,
                                    discovery_id=artifact.result["discovery_id"],
                                    initial_url=url,
                                ))
                            except (PdfRedirectError, FetchBoundaryError) as exc:
                                fetch_errors.append(_error_summary(exc))
                        result = verify_pdf_evidence(
                            request,
                            artifact.result,
                            catalog=catalog,
                            evidence=evidence,
                            verified_at=observed,
                            sample_size=1,
                        )
                    elif target.verification_kind in _HTML_KINDS:
                        html_evidence = []
                        for url in target.selected_urls:
                            try:
                                bundle = fetch_html_evidence(
                                    gate=gate,
                                    fetcher=fetcher,
                                    snapshot_store=snapshots,
                                    catalog=catalog,
                                    venue_id=venue_id,
                                    year=year,
                                    discovery_id=artifact.result["discovery_id"],
                                    initial_url=url,
                                )
                                html_evidence.append(HtmlEvidence(
                                    bundle,
                                    _profile(
                                        venue_id,
                                        target.verification_kind,
                                        artifact.source_domains[url],
                                    ),
                                ))
                            except (RedirectChainError, FetchBoundaryError) as exc:
                                fetch_errors.append(_error_summary(exc))
                        result = verify_html_evidence(
                            request,
                            artifact.result,
                            catalog=catalog,
                            evidence=html_evidence,
                            verified_at=observed,
                        )
                    else:
                        raise ShadowVerificationError(
                            f"unsupported shadow verification kind: "
                            f"{target.verification_kind}"
                        )

                    repository.accept_verification(
                        artifact.result,
                        request,
                        result,
                        lease=lease,
                        received_at=observed,
                    )
                    record = next(
                        item
                        for item in repository.replay_verifications(
                            venue_id=venue_id, year=year
                        )
                        if item.result["verification_id"] == result["verification_id"]
                    )
                    consumption = consume_verification_record(
                        repository,
                        record,
                        catalog=catalog,
                        policy=policy,
                        lease=lease,
                    )
                    actions = [
                        item.as_dict() for item in consumption.reduction.actions
                    ]
                    retained = {
                        "artifact_version": 1,
                        "shadow_only": True,
                        "discovery": artifact.result,
                        "request": request,
                        "result": result,
                        "fetch_errors": fetch_errors,
                        "inert_actions": actions,
                    }
                    _write_json(
                        output / "verifications" / venue_id / _artifact_filename(result),
                        retained,
                    )
                    target_summaries.append(_result_summary(
                        target,
                        result,
                        fetch_errors=fetch_errors,
                        actions=actions,
                    ))
                state = repository.get_conference_state(venue_id, year)
                venue_summaries.append({
                    "venue_id": venue_id,
                    "year": year,
                    "discovery_id": artifact.result["discovery_id"],
                    "targets": target_summaries,
                    "shadow_state": (
                        {
                            "lifecycle_state": state.state["lifecycle_state"],
                            "facets": deepcopy(state.state["facets"]),
                            "blockers": list(state.state["blockers"]),
                            "revision": state.revision,
                        }
                        if state is not None
                        else None
                    ),
                })
        finally:
            repository.release_lease(lease)

    summary = {
        "artifact_version": 1,
        "shadow_only": True,
        "observed_at": observed_text,
        "year": year,
        "venue_count": len(venue_summaries),
        "venues": venue_summaries,
        "effects": {
            "jobs_created": 0,
            "scrapers_executed": 0,
            "notifications_sent": 0,
            "production_state_writes": 0,
        },
    }
    _write_json(output / "shadow-summary.v1.json", summary)
    return summary
