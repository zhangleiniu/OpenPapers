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
)


FIXTURES = Path(__file__).with_name("fixtures")
DISCOVERY_FIXTURE = FIXTURES / "phase0" / "discovery-result.v1.json"
NOW = datetime(2026, 7, 13, 17, 30, tzinfo=timezone.utc)
VERIFICATION_MODULE = Path(__file__).resolve().parents[1] / "verification.py"


def load_discovery():
    return json.loads(DISCOVERY_FIXTURE.read_text(encoding="utf-8"))


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
        self.assertEqual(
            first["verification_kinds"],
            ["source_identity", "conference_milestone"],
        )
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

    def test_result_builder_is_semantically_stable_and_target_scoped(self):
        request = build_verification_request(load_discovery(), requested_at=NOW)
        first = build_verification_result(
            request,
            overall_status="review_required",
            verified_at=NOW,
            uncertainties=["No crawl policy has been approved."],
        )
        replay = build_verification_result(
            request,
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
                overall_status="rejected",
                verified_at=NOW,
                findings=[finding],
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


if __name__ == "__main__":
    unittest.main()
