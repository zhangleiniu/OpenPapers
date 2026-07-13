"""P2.3 deterministic PDF evidence verification.

This module composes the Phase 2.1 effect boundaries with bounded PDF sample
selection and signature inspection. It has no live transport, HTML identity
logic, state write, redistribution grant, or action-routing capability.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from automation.contracts import artifact_fingerprint
from automation.domain import Permission
from automation.verification import (
    MAX_FETCH_BYTES,
    CrawlDecision,
    CrawlDecisionStatus,
    CrawlPolicyError,
    CrawlPolicyGate,
    EvidenceFetcher,
    FetchResponse,
    SnapshotProvenance,
    SnapshotReference,
    SnapshotStore,
    SourceClassification,
    SourceClassificationError,
    SourceTrust,
    VerificationError,
    build_verification_result,
    classify_source,
    validate_request_against_discovery,
)


DEFAULT_PDF_SAMPLE_SIZE = 3
MAX_PDF_SAMPLE_SIZE = 10
MIN_PDF_BYTES = 1024
MAX_PDF_BYTES = MAX_FETCH_BYTES
MAX_REDIRECTS = 5
PDF_SIGNATURE = b"%PDF-"
_HARD_FAILURES = (
    "pdf_invalid_url",
    "pdf_http_status",
    "pdf_too_small",
    "pdf_signature_invalid",
)


class PdfVerificationError(VerificationError):
    """Raised when P2.3 cannot safely inspect supplied PDF evidence."""


class PdfRedirectError(PdfVerificationError):
    """A redirect chain stopped after retaining zero or more safe hops."""

    def __init__(
        self,
        message: str,
        *,
        hops: Sequence["RetainedPdfHop"] = (),
        blocked_url: str | None = None,
    ) -> None:
        self.hops = tuple(hops)
        self.blocked_url = blocked_url
        super().__init__(message)


@dataclass(frozen=True)
class PdfSamplePlan:
    """Stable bounded URL sample for one requested discovery PDF claim."""

    target_id: str
    urls: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.target_id or not self.urls:
            raise PdfVerificationError("PDF sample plan requires a target and URLs")
        if len(self.urls) > MAX_PDF_SAMPLE_SIZE:
            raise PdfVerificationError("PDF sample plan exceeds the hard limit")
        if len(set(self.urls)) != len(self.urls):
            raise PdfVerificationError("PDF sample plan URLs must be unique")


@dataclass(frozen=True)
class RetainedPdfHop:
    """One classified, policy-authorized, immutable PDF-chain hop."""

    classification: SourceClassification
    decision: CrawlDecision
    storage_decision: CrawlDecision
    response: FetchResponse
    snapshot: SnapshotReference

    def __post_init__(self) -> None:
        if (
            self.classification.url != self.response.requested_url
            or self.decision.url != self.response.requested_url
            or self.storage_decision.url != self.response.requested_url
            or self.classification.domain != self.decision.domain
            or self.classification.domain != self.storage_decision.domain
        ):
            raise PdfVerificationError(
                "retained PDF hop identity does not match its response"
            )
        if (
            self.decision.status is not CrawlDecisionStatus.ALLOWED
            or self.decision.permission is not Permission.PDF_FETCH_FOR_PROCESSING
            or not self.decision.policy_domain
        ):
            raise PdfVerificationError(
                "retained PDF hop lacks PDF-processing authorization"
            )
        if (
            self.storage_decision.status is not CrawlDecisionStatus.ALLOWED
            or (
                self.storage_decision.permission
                is not Permission.STORE_INTERNAL_COPY
            )
            or not self.storage_decision.policy_domain
        ):
            raise PdfVerificationError(
                "retained PDF hop lacks internal-copy authorization"
            )
        if self.decision.policy_domain != self.storage_decision.policy_domain:
            raise PdfVerificationError(
                "PDF fetch and storage decisions use different policy domains"
            )
        content_sha256 = hashlib.sha256(self.response.body).hexdigest()
        if (
            self.snapshot.content_sha256 != content_sha256
            or self.snapshot.size_bytes != len(self.response.body)
        ):
            raise PdfVerificationError(
                "retained PDF snapshot does not match response content"
            )

    @property
    def source_id(self) -> str:
        identity = {
            "url": self.response.requested_url,
            "status_code": self.response.status_code,
            "redirect_target_url": (
                self.response.redirect_hop.target_url
                if self.response.redirect_hop is not None
                else None
            ),
            "snapshot_id": self.snapshot.snapshot_id,
        }
        return f"source:{artifact_fingerprint(identity)[:32]}"

    def observation(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "url": self.response.requested_url,
            "redirect_target_url": (
                self.response.redirect_hop.target_url
                if self.response.redirect_hop is not None
                else None
            ),
            "source_trust": self.classification.trust.value,
            "policy_decision": self.decision.status.value,
            "policy_domain": self.decision.policy_domain,
            "permission": self.decision.permission.value,
            "fetch_status": "fetched",
            "http_status": self.response.status_code,
            "snapshot_id": self.snapshot.snapshot_id,
            "observed_at": self.response.fetched_at,
            "reason_code": "source_observed",
        }


@dataclass(frozen=True)
class PdfEvidenceBundle:
    """A selected URL and every retained response through its final hop."""

    initial_url: str
    venue_id: str
    year: int
    discovery_id: str
    hops: tuple[RetainedPdfHop, ...]

    def __post_init__(self) -> None:
        if not self.hops:
            raise PdfVerificationError("PDF evidence requires at least one hop")
        if self.hops[0].response.requested_url != self.initial_url:
            raise PdfVerificationError("first PDF hop must match the selected URL")
        for previous, current in zip(self.hops, self.hops[1:]):
            redirect = previous.response.redirect_hop
            if redirect is None or redirect.target_url != current.response.requested_url:
                raise PdfVerificationError("PDF hops do not form a redirect chain")
        if self.hops[-1].response.redirect_hop is not None:
            raise PdfVerificationError("PDF evidence cannot end on a redirect")

    @property
    def final_hop(self) -> RetainedPdfHop:
        return self.hops[-1]

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(hop.source_id for hop in self.hops)

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(hop.snapshot.snapshot_id for hop in self.hops)


@dataclass(frozen=True)
class PdfAnalysis:
    """Deterministic facts about one final PDF response."""

    valid: bool
    reason_code: str
    size_bytes: int


def _target_source(
    discovery: Mapping[str, Any], target_kind: str, target_id: str
) -> Mapping[str, Any]:
    if target_kind != "claim":
        raise PdfVerificationError("P2.3 accepts only discovery claim targets")
    for claim in discovery["claims"]:
        if claim["claim_id"] == target_id:
            return claim
    raise PdfVerificationError("request target is absent from discovery")


def _validate_sample_size(sample_size: int) -> None:
    if isinstance(sample_size, bool) or not isinstance(sample_size, int):
        raise PdfVerificationError("PDF sample size must be an integer")
    if not 1 <= sample_size <= MAX_PDF_SAMPLE_SIZE:
        raise PdfVerificationError(
            f"PDF sample size must be between 1 and {MAX_PDF_SAMPLE_SIZE}"
        )


def build_pdf_sample_plan(
    request: Mapping[str, Any],
    discovery: Mapping[str, Any],
    *,
    sample_size: int = DEFAULT_PDF_SAMPLE_SIZE,
) -> tuple[PdfSamplePlan, ...]:
    """Return an order-independent stable URL sample for each PDF target."""
    validate_request_against_discovery(request, discovery)
    if request["schema_version"] != 2:
        raise PdfVerificationError("P2.3 emits results only for v2 requests")
    if set(request["verification_kinds"]) != {"pdf"}:
        raise PdfVerificationError("verification kinds are outside P2.3 PDF scope")
    _validate_sample_size(sample_size)

    plans: list[PdfSamplePlan] = []
    for target in request["targets"]:
        if target["target_kind"] != "claim" or target["verification_kind"] != "pdf":
            raise PdfVerificationError("P2.3 accepts only PDF claim targets")
        claim = _target_source(
            discovery, target["target_kind"], target["target_id"]
        )
        ranked = sorted(
            claim["evidence_urls"],
            key=lambda url: (
                hashlib.sha256(
                    (request["request_id"] + "\0" + target["target_id"] + "\0" + url)
                    .encode("utf-8")
                ).hexdigest(),
                url,
            ),
        )
        plans.append(
            PdfSamplePlan(target["target_id"], tuple(ranked[:sample_size]))
        )
    return tuple(sorted(plans, key=lambda item: item.target_id))


def fetch_pdf_evidence(
    *,
    gate: CrawlPolicyGate,
    fetcher: EvidenceFetcher,
    snapshot_store: SnapshotStore,
    catalog: Mapping[str, Any],
    venue_id: str,
    year: int,
    discovery_id: str,
    initial_url: str,
    max_redirects: int = MAX_REDIRECTS,
    max_bytes: int = MAX_PDF_BYTES,
    timeout_seconds: float = 120.0,
) -> PdfEvidenceBundle:
    """Fetch and retain one PDF chain, independently gating every exact hop."""
    if (
        isinstance(max_redirects, bool)
        or not isinstance(max_redirects, int)
        or not 0 <= max_redirects <= MAX_REDIRECTS
    ):
        raise PdfVerificationError(
            f"max_redirects must be between 0 and {MAX_REDIRECTS}"
        )
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_PDF_BYTES
    ):
        raise PdfVerificationError(
            f"PDF max_bytes must be between 1 and {MAX_PDF_BYTES}"
        )
    if not 1900 <= year <= 2200:
        raise PdfVerificationError("PDF evidence year is invalid")
    if not discovery_id:
        raise PdfVerificationError("PDF evidence discovery ID is required")

    url = initial_url
    visited: set[str] = set()
    hops: list[RetainedPdfHop] = []
    while True:
        if url in visited:
            raise PdfRedirectError(
                "redirect loop detected", hops=hops, blocked_url=url
            )
        visited.add(url)
        classification = classify_source(catalog, venue_id, url)
        fetch_decision = gate.decide(
            url, Permission.PDF_FETCH_FOR_PROCESSING
        )
        if fetch_decision.status is not CrawlDecisionStatus.ALLOWED:
            raise PdfRedirectError(
                "PDF target stopped by crawl policy: "
                f"{fetch_decision.status.value}",
                hops=hops,
                blocked_url=url,
            )
        storage_decision = gate.decide(url, Permission.STORE_INTERNAL_COPY)
        if storage_decision.status is not CrawlDecisionStatus.ALLOWED:
            raise PdfRedirectError(
                "PDF snapshot retention stopped by crawl policy: "
                f"{storage_decision.status.value}",
                hops=hops,
                blocked_url=url,
            )
        try:
            response, decision = gate.fetch(
                fetcher,
                url=url,
                permission=Permission.PDF_FETCH_FOR_PROCESSING,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
            )
        except CrawlPolicyError as exc:
            raise PdfRedirectError(
                f"PDF target stopped by crawl policy: {exc.decision.status.value}",
                hops=hops,
                blocked_url=url,
            ) from exc
        assert decision.policy_domain is not None
        snapshot = snapshot_store.retain(
            response,
            SnapshotProvenance(
                venue_id=venue_id,
                year=year,
                discovery_id=discovery_id,
                source_trust=classification.trust,
                permission=Permission.STORE_INTERNAL_COPY,
                policy_domain=storage_decision.policy_domain,
            ),
        )
        hops.append(RetainedPdfHop(
            classification,
            decision,
            storage_decision,
            response,
            snapshot,
        ))
        redirect = response.redirect_hop
        if redirect is None:
            return PdfEvidenceBundle(
                initial_url=initial_url,
                venue_id=venue_id,
                year=year,
                discovery_id=discovery_id,
                hops=tuple(hops),
            )
        if len(hops) > max_redirects:
            raise PdfRedirectError(
                "redirect limit exceeded",
                hops=hops,
                blocked_url=redirect.target_url,
            )
        url = redirect.target_url


def analyze_pdf(
    response: FetchResponse,
    *,
    minimum_bytes: int = MIN_PDF_BYTES,
) -> PdfAnalysis:
    """Inspect one final response without parsing or granting PDF rights."""
    if (
        isinstance(minimum_bytes, bool)
        or not isinstance(minimum_bytes, int)
        or not 1 <= minimum_bytes <= MAX_PDF_BYTES
    ):
        raise PdfVerificationError(
            f"minimum PDF bytes must be between 1 and {MAX_PDF_BYTES}"
        )
    size_bytes = len(response.body)
    if response.status_code != 200:
        return PdfAnalysis(False, "pdf_http_status", size_bytes)
    content_length = response.headers.get("content-length")
    if content_length is not None:
        normalized = content_length.strip()
        if not normalized.isascii() or not normalized.isdecimal():
            return PdfAnalysis(False, "pdf_sample_incomplete", size_bytes)
        if int(normalized) != size_bytes:
            return PdfAnalysis(False, "pdf_sample_incomplete", size_bytes)
    if size_bytes < minimum_bytes:
        return PdfAnalysis(False, "pdf_too_small", size_bytes)
    if not response.body.startswith(PDF_SIGNATURE):
        return PdfAnalysis(False, "pdf_signature_invalid", size_bytes)
    return PdfAnalysis(True, "supported", size_bytes)


def _finding_id(request_id: str, target_id: str) -> str:
    identity = artifact_fingerprint({
        "request_id": request_id,
        "target_kind": "claim",
        "target_id": target_id,
        "verification_kind": "pdf",
    })
    return f"finding:{identity[:32]}"


def _reason_code(reasons: Sequence[str]) -> str:
    for reason in _HARD_FAILURES:
        if reason in reasons:
            return reason
    if "pdf_sample_incomplete" in reasons:
        return "pdf_sample_incomplete"
    return "unsupported_source_shape"


def verify_pdf_evidence(
    request: Mapping[str, Any],
    discovery: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any],
    evidence: Sequence[PdfEvidenceBundle],
    verified_at: datetime | str,
    sample_size: int = DEFAULT_PDF_SAMPLE_SIZE,
    minimum_bytes: int = MIN_PDF_BYTES,
) -> dict[str, Any]:
    """Build a strict v2 result for PDF targets from retained sample evidence."""
    plans = build_pdf_sample_plan(
        request, discovery, sample_size=sample_size
    )
    if (
        isinstance(minimum_bytes, bool)
        or not isinstance(minimum_bytes, int)
        or not 1 <= minimum_bytes <= MAX_PDF_BYTES
    ):
        raise PdfVerificationError(
            f"minimum PDF bytes must be between 1 and {MAX_PDF_BYTES}"
        )

    selected_urls = {url for plan in plans for url in plan.urls}
    bundles_by_url: dict[str, PdfEvidenceBundle] = {}
    observations: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_snapshots: set[str] = set()
    for bundle in evidence:
        if (
            bundle.discovery_id != discovery["discovery_id"]
            or bundle.venue_id != discovery["venue_id"]
            or bundle.year != discovery["year"]
        ):
            raise PdfVerificationError("PDF evidence identity does not match discovery")
        if bundle.initial_url not in selected_urls:
            raise PdfVerificationError("PDF evidence URL was not selected for sampling")
        if bundle.initial_url in bundles_by_url:
            raise PdfVerificationError("duplicate PDF evidence for a selected URL")
        bundles_by_url[bundle.initial_url] = bundle
        for hop in bundle.hops:
            expected_classification = classify_source(
                catalog, discovery["venue_id"], hop.response.requested_url
            )
            if hop.classification != expected_classification:
                raise PdfVerificationError(
                    "PDF evidence source classification does not match catalog"
                )
            source_seen = hop.source_id in seen_sources
            snapshot_seen = hop.snapshot.snapshot_id in seen_snapshots
            if source_seen != snapshot_seen:
                raise PdfVerificationError("inconsistent duplicate PDF evidence")
            if source_seen:
                continue
            seen_sources.add(hop.source_id)
            seen_snapshots.add(hop.snapshot.snapshot_id)
            observations.append(hop.observation())

    facets: dict[str, Any] = {
        "conference_status": None,
        "paper_list_status": None,
        "metadata_status": None,
        "pdf_status": None,
        "proceedings_status": None,
    }
    findings: list[dict[str, Any]] = []
    ready_facet_evidence: set[str] = set()
    partial_facet_evidence: set[str] = set()
    target_outcomes: list[tuple[str, str, tuple[str, ...]]] = []
    for plan in plans:
        reasons: list[str] = []
        source_ids: set[str] = set()
        evidence_ids: set[str] = set()
        valid_evidence_ids: set[str] = set()
        sampled_count = 0
        valid_count = 0
        for url in plan.urls:
            try:
                classify_source(catalog, discovery["venue_id"], url)
            except SourceClassificationError:
                reasons.append("pdf_invalid_url")
                continue
            bundle = bundles_by_url.get(url)
            if bundle is None:
                reasons.append("pdf_sample_incomplete")
                continue
            sampled_count += 1
            source_ids.update(bundle.source_ids)
            evidence_ids.update(bundle.evidence_ids)
            final = bundle.final_hop
            if final.classification.trust is SourceTrust.UNTRUSTED:
                reasons.append("unsupported_source_shape")
                continue
            analysis = analyze_pdf(final.response, minimum_bytes=minimum_bytes)
            if not analysis.valid:
                reasons.append(analysis.reason_code)
                continue
            valid_count += 1
            valid_evidence_ids.update(bundle.evidence_ids)

        complete = valid_count == len(plan.urls)
        hard_failure = bool(set(reasons) & set(_HARD_FAILURES))
        if complete:
            status = "verified"
            reason = "supported"
            ready_facet_evidence.update(valid_evidence_ids)
        elif hard_failure:
            status = "rejected"
            reason = _reason_code(reasons)
        else:
            status = "review_required"
            reason = _reason_code(reasons)
        if valid_count:
            partial_facet_evidence.update(valid_evidence_ids)
        findings.append({
            "finding_id": _finding_id(request["request_id"], plan.target_id),
            "target_kind": "claim",
            "target_id": plan.target_id,
            "verification_kind": "pdf",
            "status": status,
            "source_ids": sorted(source_ids),
            "evidence_ids": sorted(evidence_ids),
            "reason_code": reason,
            "metrics": {
                "pdf_sampled_count": sampled_count,
                "pdf_valid_count": valid_count,
            },
        })
        target_outcomes.append((status, reason, tuple(sorted(valid_evidence_ids))))

    if plans and all(status == "verified" for status, _, _ in target_outcomes):
        facets["pdf_status"] = {
            "value": "ready",
            "evidence_ids": sorted(ready_facet_evidence),
        }
    elif partial_facet_evidence:
        facets["pdf_status"] = {
            "value": "partial",
            "evidence_ids": sorted(partial_facet_evidence),
        }

    statuses = {finding["status"] for finding in findings}
    has_facet = facets["pdf_status"] is not None
    if "conflicting" in statuses:
        overall_status = "conflicting"
    elif has_facet and bool(statuses & {"error", "rejected", "review_required"}):
        overall_status = "partially_verified"
    elif has_facet or statuses == {"verified"}:
        overall_status = "verified"
    elif "error" in statuses:
        overall_status = "error"
    elif "rejected" in statuses:
        overall_status = "rejected"
    else:
        overall_status = "review_required"

    return build_verification_result(
        request,
        discovery,
        overall_status=overall_status,
        verified_at=verified_at,
        source_observations=sorted(
            observations, key=lambda observation: observation["source_id"]
        ),
        findings=findings,
        verified_facets=facets,
    )
