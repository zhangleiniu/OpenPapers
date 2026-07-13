import ast
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from automation.configuration import load_policy_config, load_venue_catalog
from automation.contracts import (
    ContractName,
    ContractValidationError,
    artifact_fingerprint,
    validate_contract,
)
from automation.domain import Permission
from automation.verification import (
    CrawlDecisionStatus,
    CrawlPolicyError,
    CrawlPolicyGate,
    FetchBoundaryError,
    FetchRequest,
    FetchResponse,
    FileSnapshotStore,
    MAX_FETCH_BYTES,
    MAX_FETCH_TIMEOUT_SECONDS,
    SnapshotConflictError,
    SnapshotProvenance,
    SourceClassificationError,
    SourceTrust,
    VerificationError,
    build_verification_request,
    build_verification_result,
    classify_source,
    validate_request_against_discovery,
    validate_verification_result,
)


FIXTURES = Path(__file__).with_name("fixtures")
DISCOVERY_FIXTURE = FIXTURES / "phase0" / "discovery-result.v1.json"
NOW = datetime(2026, 7, 13, 17, 30, tzinfo=timezone.utc)
VERIFICATION_MODULE = Path(__file__).resolve().parents[1] / "verification.py"


def load_discovery():
    return json.loads(DISCOVERY_FIXTURE.read_text(encoding="utf-8"))


def load_phase2(name):
    return json.loads(
        (FIXTURES / "phase2" / name).read_text(encoding="utf-8"))


def resign_result(result):
    evidence = {
        field: result[field]
        for field in (
            "request_id",
            "discovery_id",
            "venue_id",
            "year",
            "overall_status",
            "source_observations",
            "findings",
            "verified_facets",
            "verified_milestones",
            "uncertainties",
        )
    }
    fingerprint = artifact_fingerprint(evidence)
    result["evidence_fingerprint"] = fingerprint
    result["verification_id"] = f"verification:{fingerprint[:32]}"
    return result


def domain_policy(
    domain,
    *,
    classification="approved",
    permissions=("metadata_fetch",),
    max_requests=2,
):
    return {
        "domain": domain,
        "classification": classification,
        "allowed_permissions": list(permissions),
        "max_concurrency": 1,
        "minimum_delay_seconds": 0.5,
        "jitter_seconds": 0.1,
        "max_requests_per_run": max_requests,
        "honor_retry_after": True,
        "stop_statuses": [403, 429],
        "stop_on_captcha": True,
        "api_preferred": False,
        "user_agent_contact": (
            "OpenPapers maintainer@example.test"
            if classification == "approved" else None
        ),
    }


def policy_with(*domains):
    policy = load_policy_config()
    policy["crawl"]["domains"] = list(domains)
    return policy


class FakeFetcher:
    def __init__(self, response=None):
        self.requests = []
        self.response = response

    def fetch(self, request: FetchRequest) -> FetchResponse:
        self.requests.append(request)
        if self.response is not None:
            return self.response
        return FetchResponse(
            requested_url=request.url,
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=b"<html>fixture</html>",
            fetched_at="2026-07-13T17:30:00Z",
        )


class VerificationContractTests(unittest.TestCase):
    def test_foundation_has_no_live_network_or_state_dependency(self):
        tree = ast.parse(VERIFICATION_MODULE.read_text(encoding="utf-8"))
        imports = {
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertTrue(
            {"requests", "sqlite3", "prefect", "google"}.isdisjoint(imports))

    def test_request_builder_selects_exact_discovery_targets(self):
        discovery = load_discovery()
        first = build_verification_request(discovery, requested_at=NOW)
        replay = build_verification_request(
            discovery,
            requested_at="2026-07-14T17:30:00Z",
        )

        self.assertEqual(first["request_id"], replay["request_id"])
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(
            first["verification_kinds"],
            ["source_identity", "conference_milestone"],
        )
        self.assertEqual(first["targets"], [
            {
                "target_kind": "candidate_milestone",
                "target_id": "milestone:icml:2026:end",
                "verification_kind": "conference_milestone",
            },
            {
                "target_kind": "claim",
                "target_id": "claim:icml:2026:list",
                "verification_kind": "source_identity",
            },
        ])
        self.assertEqual(first["discovery_id"], discovery["discovery_id"])
        self.assertNotIn("action", first)
        validate_request_against_discovery(first, discovery)

        changed = deepcopy(discovery)
        changed["evidence_fingerprint"] = "f" * 64
        with self.assertRaisesRegex(VerificationError, "fingerprint"):
            validate_request_against_discovery(first, changed)

        with self.assertRaisesRegex(VerificationError, "absent"):
            build_verification_request(
                discovery,
                requested_at=NOW,
                claim_ids=["claim:icml:2026:missing"],
                candidate_milestone_ids=[],
            )
        with self.assertRaisesRegex(VerificationError, "at least one"):
            build_verification_request(
                discovery,
                requested_at=NOW,
                claim_ids=[],
                candidate_milestone_ids=[],
            )

    def test_request_semantics_reject_kind_target_and_identity_drift(self):
        discovery = load_discovery()
        request = build_verification_request(discovery, requested_at=NOW)
        mutations = (
            ("kinds", lambda item: item.update(
                verification_kinds=["paper_list", "conference_milestone"])),
            ("target kind", lambda item: item["targets"][1].update(
                target_kind="candidate_milestone")),
            ("verification kind", lambda item: item["targets"][1].update(
                verification_kind="paper_list")),
            ("request ID", lambda item: item.update(
                request_id="verify-request:11111111111111111111111111111111")),
        )
        for label, mutate in mutations:
            candidate = deepcopy(request)
            mutate(candidate)
            with self.subTest(label=label), self.assertRaises(
                    VerificationError):
                validate_request_against_discovery(candidate, discovery)

    def test_v1_compatibility_and_v2_fixtures_are_semantically_replayable(self):
        discovery = load_discovery()
        for version in (1, 2):
            request = load_phase2(f"verification-request.v{version}.json")
            result = load_phase2(f"verification-result.v{version}.json")
            with self.subTest(version=version):
                validate_request_against_discovery(request, discovery)
                validate_verification_result(result, request, discovery)

    def test_result_builder_is_semantically_stable_and_target_scoped(self):
        discovery = load_discovery()
        request = build_verification_request(discovery, requested_at=NOW)
        first = build_verification_result(
            request,
            discovery,
            overall_status="review_required",
            verified_at=NOW,
            uncertainties=["No crawl policy has been approved."],
        )
        replay = build_verification_result(
            request,
            discovery,
            overall_status="review_required",
            verified_at="2026-07-14T17:30:00Z",
            uncertainties=["No crawl policy has been approved."],
        )

        self.assertEqual(first["verification_id"], replay["verification_id"])
        self.assertEqual(
            first["evidence_fingerprint"], replay["evidence_fingerprint"])
        self.assertTrue(all(
            value is None for value in first["verified_facets"].values()))
        self.assertNotIn("transition", first)

        finding = {
            "finding_id": "finding:icml:2026:missing",
            "target_kind": "claim",
            "target_id": "claim:icml:2026:missing",
            "verification_kind": "paper_list",
            "status": "rejected",
            "source_ids": [],
            "evidence_ids": [],
            "reason_code": "unsupported_source_shape",
            "metrics": None,
        }
        with self.assertRaisesRegex(VerificationError, "absent"):
            build_verification_result(
                request,
                discovery,
                overall_status="rejected",
                verified_at=NOW,
                findings=[finding],
            )

    def test_result_rejects_kind_drift_and_evidence_free_verified_status(self):
        discovery = load_discovery()
        request = build_verification_request(discovery, requested_at=NOW)
        result = load_phase2("verification-result.v2.json")

        for field, value in (
            ("target_kind", "candidate_milestone"),
            ("verification_kind", "paper_list"),
        ):
            candidate = deepcopy(result)
            candidate["findings"][0][field] = value
            resign_result(candidate)
            with self.subTest(field=field), self.assertRaises(
                    VerificationError):
                validate_verification_result(candidate, request, discovery)

        milestone_drift = deepcopy(result)
        milestone_drift["verified_milestones"] = [{
            "candidate_milestone_id": "milestone:icml:2026:end",
            "milestone_type": "conference_end",
            "scope": "conference",
            "date": "2026-07-19",
            "source_type": "official",
            "source_url": "https://icml.cc/virtual/2026/calendar",
            "evidence_ids": ["snapshot:33333333333333333333333333333333"],
        }]
        resign_result(milestone_drift)
        with self.assertRaisesRegex(VerificationError, "milestone content"):
            validate_verification_result(
                milestone_drift, request, discovery)

        finding = deepcopy(result["findings"][0])
        finding["evidence_ids"] = []
        with self.assertRaises((ContractValidationError, VerificationError)):
            build_verification_result(
                request,
                discovery,
                overall_status="verified",
                verified_at=NOW,
                source_observations=result["source_observations"],
                findings=[finding],
            )

        legacy_request = load_phase2("verification-request.v1.json")
        evidence_free = load_phase2("verification-result.v1.json")
        evidence_free["findings"][0]["evidence_ids"] = []
        evidence_free["verified_milestones"] = []
        resign_result(evidence_free)
        validate_contract(ContractName.VERIFICATION_RESULT, evidence_free)
        with self.assertRaisesRegex(VerificationError, "requires retained"):
            validate_verification_result(
                evidence_free, legacy_request, discovery)

        with self.assertRaisesRegex(VerificationError, "overall status"):
            build_verification_result(
                request,
                discovery,
                overall_status="rejected",
                verified_at=NOW,
                source_observations=result["source_observations"],
                findings=result["findings"],
            )

    def test_result_rejects_dangling_finding_facet_and_milestone_evidence(self):
        discovery = load_discovery()
        request = build_verification_request(discovery, requested_at=NOW)
        base = load_phase2("verification-result.v2.json")
        dangling = "snapshot:99999999999999999999999999999999"

        finding = deepcopy(base)
        finding["findings"][0]["evidence_ids"] = [dangling]
        resign_result(finding)

        facet = deepcopy(base)
        facet["verified_facets"]["conference_status"] = {
            "value": "ended",
            "evidence_ids": [dangling],
        }
        resign_result(facet)

        milestone = deepcopy(base)
        milestone["verified_milestones"] = [{
            "candidate_milestone_id": "milestone:icml:2026:end",
            "milestone_type": "conference_end",
            "scope": "conference",
            "date": "2026-07-18",
            "source_type": "official",
            "source_url": "https://icml.cc/virtual/2026/calendar",
            "evidence_ids": [dangling],
        }]
        resign_result(milestone)

        for label, candidate in (
            ("finding", finding), ("facet", facet), ("milestone", milestone)):
            with self.subTest(label=label), self.assertRaisesRegex(
                    VerificationError, "evidence absent"):
                validate_verification_result(candidate, request, discovery)

    def test_result_rejects_missing_policy_domain_and_signed_urls(self):
        discovery = load_discovery()
        request = build_verification_request(discovery, requested_at=NOW)
        result = load_phase2("verification-result.v2.json")

        legacy_request = load_phase2("verification-request.v1.json")
        missing_policy = load_phase2("verification-result.v1.json")
        missing_policy["source_observations"][0]["policy_domain"] = None
        resign_result(missing_policy)
        validate_contract(ContractName.VERIFICATION_RESULT, missing_policy)
        with self.assertRaisesRegex(VerificationError, "policy domain"):
            validate_verification_result(
                missing_policy, legacy_request, discovery)

        signed = deepcopy(result)
        signed["source_observations"][0]["url"] = (
            "https://icml.cc/source?X-Amz-Signature=not-a-real-signature")
        resign_result(signed)
        with self.assertRaisesRegex(
                SourceClassificationError, "signed query"):
            validate_verification_result(signed, request, discovery)

        lost_redirect = load_phase2("verification-result.v1.json")
        lost_redirect["source_observations"][0]["http_status"] = 302
        resign_result(lost_redirect)
        validate_contract(ContractName.VERIFICATION_RESULT, lost_redirect)
        with self.assertRaisesRegex(VerificationError, "redirect edge"):
            validate_verification_result(
                lost_redirect, legacy_request, discovery)

    def test_result_retains_a_replayable_redirect_edge(self):
        discovery = load_discovery()
        request = build_verification_request(discovery, requested_at=NOW)
        result = load_phase2("verification-result.v2.json")
        observation = deepcopy(result["source_observations"][0])
        observation["http_status"] = 302
        observation["redirect_target_url"] = "https://icml.cc/archive?page=2"
        finding = deepcopy(result["findings"][0])
        finding["status"] = "review_required"
        finding["reason_code"] = "redirect_review_required"

        redirected = build_verification_result(
            request,
            discovery,
            overall_status="review_required",
            verified_at=NOW,
            source_observations=[observation],
            findings=[finding],
        )

        self.assertEqual(
            redirected["source_observations"][0]["redirect_target_url"],
            "https://icml.cc/archive?page=2",
        )

    def test_result_contract_rejects_observation_that_bypasses_policy(self):
        fixture = (
            FIXTURES / "phase2" / "verification-result.v1.json"
        )
        result = json.loads(fixture.read_text(encoding="utf-8"))
        result["source_observations"][0]["policy_decision"] = "denied"

        with self.assertRaises(ContractValidationError):
            validate_contract(ContractName.VERIFICATION_RESULT, result)


class SourceTrustTests(unittest.TestCase):
    def test_catalog_domains_are_classified_without_suffix_confusion(self):
        catalog = load_venue_catalog()
        cases = (
            ("https://icml.cc/2026", SourceTrust.OFFICIAL, "icml.cc"),
            ("https://virtual.icml.cc/2026", SourceTrust.OFFICIAL, "icml.cc"),
            (
                "https://proceedings.mlr.press/v300/paper.html",
                SourceTrust.ARCHIVAL,
                "proceedings.mlr.press",
            ),
            ("https://evilicml.cc/2026", SourceTrust.UNTRUSTED, None),
        )
        for url, trust, catalog_domain in cases:
            with self.subTest(url=url):
                result = classify_source(catalog, "icml", url)
                self.assertEqual(result.trust, trust)
                self.assertEqual(result.catalog_domain, catalog_domain)

    def test_invalid_urls_and_unknown_venues_fail_closed(self):
        catalog = load_venue_catalog()
        for url in (
            "http://icml.cc/2026",
            "https://user:password@icml.cc/2026",
            "https://icml.cc:8443/2026",
            "https://icml.cc/2026?access_token=not-a-real-secret",
            "https://icml.cc/2026?X-Goog-Signature=not-a-real-signature",
            "https://icml.cc/2026#access-token",
        ):
            with self.subTest(url=url), self.assertRaises(
                    SourceClassificationError):
                classify_source(catalog, "icml", url)
        with self.assertRaisesRegex(SourceClassificationError, "unknown"):
            classify_source(catalog, "missing", "https://icml.cc/2026")


class CrawlPolicyGateTests(unittest.TestCase):
    def test_closed_decisions_never_call_fetcher(self):
        policies = (
            (
                policy_with(),
                "https://new.example.test/source",
                Permission.METADATA_FETCH,
                CrawlDecisionStatus.REVIEW_REQUIRED,
            ),
            (
                policy_with(domain_policy(
                    "icml.cc", classification="denied", permissions=())),
                "https://icml.cc/source",
                Permission.METADATA_FETCH,
                CrawlDecisionStatus.DENIED,
            ),
            (
                policy_with(domain_policy("icml.cc")),
                "https://icml.cc/paper.pdf",
                Permission.PDF_FETCH_FOR_PROCESSING,
                CrawlDecisionStatus.PERMISSION_MISSING,
            ),
        )
        for policy, url, permission, expected in policies:
            fetcher = FakeFetcher()
            gate = CrawlPolicyGate(policy)
            with self.subTest(expected=expected), self.assertRaises(
                    CrawlPolicyError) as raised:
                gate.fetch(
                    fetcher,
                    url=url,
                    permission=permission,
                    max_bytes=1024,
                    timeout_seconds=5,
                )
            self.assertEqual(raised.exception.decision.status, expected)
            self.assertEqual(fetcher.requests, [])

    def test_most_specific_policy_and_request_budget_are_enforced(self):
        gate = CrawlPolicyGate(policy_with(
            domain_policy("icml.cc", max_requests=1),
            domain_policy(
                "private.icml.cc", classification="denied", permissions=()),
        ))
        fetcher = FakeFetcher()

        with self.assertRaises(CrawlPolicyError) as raised:
            gate.fetch(
                fetcher,
                url="https://private.icml.cc/source",
                permission=Permission.METADATA_FETCH,
                max_bytes=1024,
                timeout_seconds=5,
            )
        self.assertEqual(raised.exception.decision.status,
                         CrawlDecisionStatus.DENIED)
        self.assertEqual(fetcher.requests, [])

        response, decision = gate.fetch(
            fetcher,
            url="https://icml.cc/source",
            permission=Permission.METADATA_FETCH,
            max_bytes=1024,
            timeout_seconds=5,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(decision.status, CrawlDecisionStatus.ALLOWED)
        self.assertEqual(len(fetcher.requests), 1)
        request = fetcher.requests[0]
        self.assertFalse(request.follow_redirects)
        self.assertTrue(request.honor_retry_after)
        self.assertIn(429, request.stop_statuses)

        with self.assertRaises(CrawlPolicyError) as exhausted:
            gate.fetch(
                fetcher,
                url="https://icml.cc/another",
                permission=Permission.METADATA_FETCH,
                max_bytes=1024,
                timeout_seconds=5,
            )
        self.assertEqual(
            exhausted.exception.decision.status,
            CrawlDecisionStatus.REQUEST_BUDGET_EXHAUSTED,
        )
        self.assertEqual(len(fetcher.requests), 1)

    def test_fetcher_must_preserve_authorized_url_and_byte_limit(self):
        policy = policy_with(domain_policy("icml.cc"))
        wrong_url = FakeFetcher(FetchResponse(
            requested_url="https://other.example/source",
            status_code=200,
            headers={},
            body=b"ok",
            fetched_at="2026-07-13T17:30:00Z",
        ))
        with self.assertRaisesRegex(FetchBoundaryError, "differs"):
            CrawlPolicyGate(policy).fetch(
                wrong_url,
                url="https://icml.cc/source",
                permission=Permission.METADATA_FETCH,
                max_bytes=1024,
                timeout_seconds=5,
            )

        too_large = FakeFetcher(FetchResponse(
            requested_url="https://icml.cc/source",
            status_code=200,
            headers={},
            body=b"x" * 5,
            fetched_at="2026-07-13T17:30:00Z",
        ))
        with self.assertRaisesRegex(FetchBoundaryError, "more bytes"):
            CrawlPolicyGate(policy).fetch(
                too_large,
                url="https://icml.cc/source",
                permission=Permission.METADATA_FETCH,
                max_bytes=4,
                timeout_seconds=5,
            )

        with self.assertRaisesRegex(FetchBoundaryError, "cannot exceed"):
            FetchRequest(
                url="https://icml.cc/source",
                permission=Permission.METADATA_FETCH,
                max_bytes=MAX_FETCH_BYTES + 1,
                timeout_seconds=MAX_FETCH_TIMEOUT_SECONDS + 1,
                policy_domain="icml.cc",
                user_agent_contact="OpenPapers maintainer@example.test",
                max_concurrency=1,
                minimum_delay_seconds=0,
                jitter_seconds=0,
                honor_retry_after=True,
                stop_statuses=(429,),
                stop_on_captcha=True,
                api_preferred=False,
            )
        with self.assertRaisesRegex(FetchBoundaryError, "cannot exceed"):
            FetchRequest(
                url="https://icml.cc/source",
                permission=Permission.METADATA_FETCH,
                max_bytes=1024,
                timeout_seconds=MAX_FETCH_TIMEOUT_SECONDS + 1,
                policy_domain="icml.cc",
                user_agent_contact="OpenPapers maintainer@example.test",
                max_concurrency=1,
                minimum_delay_seconds=0,
                jitter_seconds=0,
                honor_retry_after=True,
                stop_statuses=(429,),
                stop_on_captcha=True,
                api_preferred=False,
            )


class SnapshotStoreTests(unittest.TestCase):
    def test_snapshot_is_content_addressed_secret_safe_and_idempotent(self):
        response = FetchResponse(
            requested_url="https://icml.cc/source",
            status_code=200,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "ETag": "fixture-etag",
                "Set-Cookie": "must-not-be-retained",
                "Authorization": "must-not-be-retained",
            },
            body=b"<html>fixture</html>",
            fetched_at="2026-07-13T17:30:00Z",
        )
        provenance = SnapshotProvenance(
            venue_id="icml",
            year=2026,
            discovery_id="discovery:icml:2026:001",
            source_trust=SourceTrust.OFFICIAL,
            permission=Permission.METADATA_FETCH,
            policy_domain="icml.cc",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = FileSnapshotStore(Path(temp_dir))
            first = store.retain(response, provenance)
            replay = store.retain(response, provenance)

            self.assertEqual(first, replay)
            self.assertTrue(first.object_path.name.endswith(".html"))
            self.assertEqual(first.object_path.read_bytes(), response.body)
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["headers"],
                {
                    "content-type": "text/html; charset=utf-8",
                    "etag": "fixture-etag",
                },
            )
            serialized = first.manifest_path.read_text(encoding="utf-8").lower()
            self.assertNotIn("cookie", serialized)
            self.assertNotIn("authorization", serialized)

            first.object_path.write_bytes(b"corrupt")
            with self.assertRaises(SnapshotConflictError):
                store.retain(response, provenance)

    def test_snapshot_rejects_policy_domain_mismatch(self):
        response = FetchResponse(
            requested_url="https://icml.cc/source",
            status_code=200,
            headers={},
            body=b"fixture",
            fetched_at="2026-07-13T17:30:00Z",
        )
        provenance = SnapshotProvenance(
            venue_id="icml",
            year=2026,
            discovery_id="discovery:icml:2026:001",
            source_trust=SourceTrust.OFFICIAL,
            permission=Permission.METADATA_FETCH,
            policy_domain="example.test",
        )
        with tempfile.TemporaryDirectory() as temp_dir, self.assertRaisesRegex(
                VerificationError, "outside"):
            FileSnapshotStore(Path(temp_dir)).retain(response, provenance)

    def test_redirect_hop_is_sanitized_retained_and_never_followed(self):
        response = FetchResponse(
            requested_url="https://icml.cc/source",
            status_code=302,
            headers={
                "Location": "/archive?page=2",
                "Content-Type": "text/html",
            },
            body=b"redirect fixture",
            fetched_at="2026-07-13T17:30:00Z",
        )
        self.assertIsNotNone(response.redirect_hop)
        self.assertEqual(
            response.redirect_hop.target_url,
            "https://icml.cc/archive?page=2",
        )
        provenance = SnapshotProvenance(
            venue_id="icml",
            year=2026,
            discovery_id="discovery:icml:2026:001",
            source_trust=SourceTrust.OFFICIAL,
            permission=Permission.METADATA_FETCH,
            policy_domain="icml.cc",
        )
        fetcher = FakeFetcher(response)
        gate = CrawlPolicyGate(policy_with(domain_policy("icml.cc")))
        fetched, _ = gate.fetch(
            fetcher,
            url="https://icml.cc/source",
            permission=Permission.METADATA_FETCH,
            max_bytes=1024,
            timeout_seconds=5,
        )
        self.assertEqual(len(fetcher.requests), 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            reference = FileSnapshotStore(Path(temp_dir)).retain(
                fetched, provenance)
            manifest = json.loads(
                reference.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["snapshot_version"], 2)
            self.assertEqual(manifest["redirect_hop"], {
                "source_url": "https://icml.cc/source",
                "target_url": "https://icml.cc/archive?page=2",
                "status_code": 302,
            })
            self.assertNotIn("location", manifest["headers"])

    def test_redirect_rejects_missing_or_signed_location_before_retention(self):
        with self.assertRaisesRegex(FetchBoundaryError, "Location"):
            FetchResponse(
                requested_url="https://icml.cc/source",
                status_code=302,
                headers={},
                body=b"",
                fetched_at="2026-07-13T17:30:00Z",
            )
        with self.assertRaisesRegex(FetchBoundaryError, "safe to retain"):
            FetchResponse(
                requested_url="https://icml.cc/source",
                status_code=302,
                headers={
                    "Location": (
                        "https://cdn.example.test/paper?Signature="
                        "not-a-real-signature")
                },
                body=b"",
                fetched_at="2026-07-13T17:30:00Z",
            )


if __name__ == "__main__":
    unittest.main()
