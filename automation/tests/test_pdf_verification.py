import ast
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from automation.configuration import load_policy_config, load_venue_catalog
from automation.pdf_verification import (
    MAX_PDF_SAMPLE_SIZE,
    MIN_PDF_BYTES,
    PdfRedirectError,
    PdfVerificationError,
    analyze_pdf,
    build_pdf_sample_plan,
    fetch_pdf_evidence,
    verify_pdf_evidence,
)
from automation.verification import (
    CrawlPolicyGate,
    FetchRequest,
    FetchResponse,
    FileSnapshotStore,
    SourceTrust,
    build_verification_request,
    validate_verification_result,
)


FIXTURES = Path(__file__).with_name("fixtures") / "phase2" / "pdf"
MODULE = Path(__file__).resolve().parents[1] / "pdf_verification.py"
NOW = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
URLS = (
    "https://icml.cc/virtual/2026/poster/101.pdf",
    "https://icml.cc/virtual/2026/poster/102.pdf",
    "https://icml.cc/virtual/2026/poster/103.pdf",
    "https://icml.cc/virtual/2026/poster/104.pdf",
)


def fixture(name):
    return (FIXTURES / name).read_bytes()


def valid_pdf():
    body = fixture("sanitized-valid.pdf")
    return body + b"\n" + b"x" * (MIN_PDF_BYTES + 32 - len(body))


def domain_policy(domain, *permissions, max_requests=20):
    return {
        "domain": domain,
        "classification": "approved",
        "allowed_permissions": list(permissions),
        "max_concurrency": 1,
        "minimum_delay_seconds": 0.0,
        "jitter_seconds": 0.0,
        "max_requests_per_run": max_requests,
        "honor_retry_after": True,
        "stop_statuses": [403, 429],
        "stop_on_captcha": True,
        "api_preferred": False,
        "user_agent_contact": "OpenPapers maintainer@example.test",
    }


def policy_with(*domains):
    policy = load_policy_config()
    policy["crawl"]["domains"] = list(domains)
    return policy


class MappingFetcher:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.requests = []

    def fetch(self, request: FetchRequest) -> FetchResponse:
        self.requests.append(request)
        response = self.responses[request.url]
        body = response.get("body", b"")
        headers = response.get("headers")
        if headers is None:
            headers = {
                "Content-Type": "application/pdf",
                "Content-Length": str(len(body)),
            }
        return FetchResponse(
            requested_url=request.url,
            status_code=response.get("status", 200),
            headers=headers,
            body=body,
            fetched_at=response.get("fetched_at", "2026-07-13T20:00:00Z"),
        )


def discovery(urls=URLS, *, claim_kind="pdf"):
    return {
        "schema_version": 1,
        "discovery_id": "discovery:icml:2026:pdf-fixture",
        "venue_id": "icml",
        "year": 2026,
        "checked_at": "2026-07-13T19:00:00Z",
        "provider": "fixture-provider",
        "model": "fixture-model",
        "prompt_version": "v1",
        "conference_status": "unknown",
        "paper_list_status": "unknown",
        "metadata_status": "unknown",
        "pdf_status": "ready" if claim_kind == "pdf" else "unknown",
        "proceedings_status": "unknown",
        "claims": [{
            "claim_id": "claim:icml:2026:pdf",
            "claim_kind": claim_kind,
            "statement": "Sanitized fixture claim.",
            "evidence_urls": list(urls),
            "source_type": "official",
            "published_at": None,
        }],
        "candidate_milestones": [],
        "confidence": 0.8,
        "uncertainties": [],
        "evidence_fingerprint": "c" * 64,
    }


def request_for(item):
    return build_verification_request(
        item,
        requested_at=NOW,
        candidate_milestone_ids=[],
    )


def response(url, body, *, status=200, headers=None):
    if headers is None:
        headers = {
            "Content-Type": "application/pdf",
            "Content-Length": str(len(body)),
        }
    return FetchResponse(
        requested_url=url,
        status_code=status,
        headers=headers,
        body=body,
        fetched_at="2026-07-13T20:00:00Z",
    )


class PdfSamplePlanTests(unittest.TestCase):
    def test_sample_is_bounded_order_independent_and_replay_stable(self):
        first_item = discovery(URLS)
        second_item = discovery(tuple(reversed(URLS)))
        first_request = request_for(first_item)
        second_request = request_for(second_item)

        first = build_pdf_sample_plan(first_request, first_item)
        replay = build_pdf_sample_plan(first_request, first_item)
        reordered = build_pdf_sample_plan(second_request, second_item)

        self.assertEqual(first, replay)
        self.assertEqual(first, reordered)
        self.assertEqual(len(first[0].urls), 3)
        omitted = next(iter(set(URLS) - set(first[0].urls)))
        self.assertEqual(set(first[0].urls) | {omitted}, set(URLS))
        with self.assertRaisesRegex(PdfVerificationError, "between"):
            build_pdf_sample_plan(first_request, first_item, sample_size=0)
        with self.assertRaisesRegex(PdfVerificationError, "between"):
            build_pdf_sample_plan(
                first_request,
                first_item,
                sample_size=MAX_PDF_SAMPLE_SIZE + 1,
            )

    def test_non_pdf_and_mixed_requests_are_outside_the_package_scope(self):
        item = discovery((URLS[0],), claim_kind="metadata")
        request = request_for(item)
        with self.assertRaisesRegex(PdfVerificationError, "outside P2.3"):
            build_pdf_sample_plan(request, item)


class PdfFetchTests(unittest.TestCase):
    def test_fetch_and_internal_copy_permissions_are_required_before_request(self):
        fetcher = MappingFetcher({URLS[0]: {"body": valid_pdf()}})
        gate = CrawlPolicyGate(policy_with(
            domain_policy("icml.cc", "store_internal_copy")
        ))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PdfRedirectError, "permission_missing"):
                fetch_pdf_evidence(
                    gate=gate,
                    fetcher=fetcher,
                    snapshot_store=FileSnapshotStore(Path(directory)),
                    catalog=load_venue_catalog(),
                    venue_id="icml",
                    year=2026,
                    discovery_id="discovery:icml:2026:pdf-fixture",
                    initial_url=URLS[0],
                )
        self.assertEqual(fetcher.requests, [])

        storage_fetcher = MappingFetcher({URLS[0]: {"body": valid_pdf()}})
        storage_gate = CrawlPolicyGate(policy_with(
            domain_policy("icml.cc", "pdf_fetch_for_processing")
        ))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PdfRedirectError, "snapshot retention"):
                fetch_pdf_evidence(
                    gate=storage_gate,
                    fetcher=storage_fetcher,
                    snapshot_store=FileSnapshotStore(Path(directory)),
                    catalog=load_venue_catalog(),
                    venue_id="icml",
                    year=2026,
                    discovery_id="discovery:icml:2026:pdf-fixture",
                    initial_url=URLS[0],
                )
        self.assertEqual(storage_fetcher.requests, [])

    def test_each_redirect_hop_uses_pdf_processing_permission_and_is_retained(self):
        final = "https://proceedings.mlr.press/v300/paper.pdf"
        fetcher = MappingFetcher({
            URLS[0]: {
                "status": 302,
                "headers": {"Location": final},
            },
            final: {"body": valid_pdf()},
        })
        gate = CrawlPolicyGate(policy_with(
            domain_policy(
                "icml.cc", "pdf_fetch_for_processing", "store_internal_copy"
            ),
            domain_policy(
                "proceedings.mlr.press",
                "pdf_fetch_for_processing",
                "store_internal_copy",
            ),
        ))
        with tempfile.TemporaryDirectory() as directory:
            bundle = fetch_pdf_evidence(
                gate=gate,
                fetcher=fetcher,
                snapshot_store=FileSnapshotStore(Path(directory)),
                catalog=load_venue_catalog(),
                venue_id="icml",
                year=2026,
                discovery_id="discovery:icml:2026:pdf-fixture",
                initial_url=URLS[0],
            )

            self.assertTrue(all(
                '"permission": "store_internal_copy"'
                in hop.snapshot.manifest_path.read_text(encoding="utf-8")
                for hop in bundle.hops
            ))

        self.assertEqual(len(bundle.hops), 2)
        self.assertEqual([item.url for item in fetcher.requests], [URLS[0], final])
        self.assertTrue(all(
            item.permission.value == "pdf_fetch_for_processing"
            and not item.follow_redirects
            for item in fetcher.requests
        ))
        self.assertTrue(all(
            hop.observation()["permission"] == "pdf_fetch_for_processing"
            for hop in bundle.hops
        ))

    def test_closed_redirect_loop_and_limit_stop_before_the_next_request(self):
        blocked = "https://unreviewed.example.test/paper.pdf"
        fetcher = MappingFetcher({
            URLS[0]: {"status": 302, "headers": {"Location": blocked}},
        })
        gate = CrawlPolicyGate(policy_with(
            domain_policy(
                "icml.cc", "pdf_fetch_for_processing", "store_internal_copy"
            )
        ))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PdfRedirectError) as caught:
                fetch_pdf_evidence(
                    gate=gate,
                    fetcher=fetcher,
                    snapshot_store=FileSnapshotStore(Path(directory)),
                    catalog=load_venue_catalog(),
                    venue_id="icml",
                    year=2026,
                    discovery_id="discovery:icml:2026:pdf-fixture",
                    initial_url=URLS[0],
                )
        self.assertEqual(caught.exception.blocked_url, blocked)
        self.assertEqual(len(caught.exception.hops), 1)
        self.assertEqual([item.url for item in fetcher.requests], [URLS[0]])

        loop_fetcher = MappingFetcher({
            URLS[0]: {"status": 302, "headers": {"Location": URLS[1]}},
            URLS[1]: {"status": 302, "headers": {"Location": URLS[0]}},
        })
        loop_gate = CrawlPolicyGate(policy_with(
            domain_policy(
                "icml.cc", "pdf_fetch_for_processing", "store_internal_copy"
            )
        ))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PdfRedirectError, "loop"):
                fetch_pdf_evidence(
                    gate=loop_gate,
                    fetcher=loop_fetcher,
                    snapshot_store=FileSnapshotStore(Path(directory)),
                    catalog=load_venue_catalog(),
                    venue_id="icml",
                    year=2026,
                    discovery_id="discovery:icml:2026:pdf-fixture",
                    initial_url=URLS[0],
                )
        self.assertEqual(len(loop_fetcher.requests), 2)

        limit_fetcher = MappingFetcher({
            URLS[0]: {"status": 302, "headers": {"Location": URLS[1]}},
        })
        limit_gate = CrawlPolicyGate(policy_with(
            domain_policy(
                "icml.cc", "pdf_fetch_for_processing", "store_internal_copy"
            )
        ))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PdfRedirectError, "limit"):
                fetch_pdf_evidence(
                    gate=limit_gate,
                    fetcher=limit_fetcher,
                    snapshot_store=FileSnapshotStore(Path(directory)),
                    catalog=load_venue_catalog(),
                    venue_id="icml",
                    year=2026,
                    discovery_id="discovery:icml:2026:pdf-fixture",
                    initial_url=URLS[0],
                    max_redirects=0,
                )
        self.assertEqual(len(limit_fetcher.requests), 1)


class PdfAnalysisTests(unittest.TestCase):
    def test_status_size_length_and_signature_fail_closed(self):
        good = valid_pdf()
        cases = (
            (response(URLS[0], good, status=404), "pdf_http_status"),
            (response(URLS[0], b"%PDF-short"), "pdf_too_small"),
            (
                response(
                    URLS[0],
                    fixture("sanitized-not-pdf.html") + b"x" * MIN_PDF_BYTES,
                ),
                "pdf_signature_invalid",
            ),
            (
                response(
                    URLS[0],
                    good,
                    headers={
                        "Content-Type": "application/pdf",
                        "Content-Length": str(len(good) + 1),
                    },
                ),
                "pdf_sample_incomplete",
            ),
            (
                response(
                    URLS[0],
                    good,
                    headers={
                        "Content-Type": "application/pdf",
                        "Content-Length": "unknown",
                    },
                ),
                "pdf_sample_incomplete",
            ),
        )
        for item, reason in cases:
            with self.subTest(reason=reason):
                analysis = analyze_pdf(item)
                self.assertFalse(analysis.valid)
                self.assertEqual(analysis.reason_code, reason)

        accepted = analyze_pdf(response(URLS[0], good))
        self.assertTrue(accepted.valid)
        self.assertEqual(accepted.reason_code, "supported")


class PdfResultTests(unittest.TestCase):
    def _bundles(self, directory, item, urls, responses):
        fetcher = MappingFetcher(responses)
        gate = CrawlPolicyGate(policy_with(
            domain_policy(
                "icml.cc", "pdf_fetch_for_processing", "store_internal_copy"
            )
        ))
        store = FileSnapshotStore(Path(directory))
        bundles = [
            fetch_pdf_evidence(
                gate=gate,
                fetcher=fetcher,
                snapshot_store=store,
                catalog=load_venue_catalog(),
                venue_id=item["venue_id"],
                year=item["year"],
                discovery_id=item["discovery_id"],
                initial_url=url,
            )
            for url in urls
        ]
        return bundles, fetcher

    def test_complete_deterministic_sample_verifies_ready_and_replays(self):
        item = discovery()
        request = request_for(item)
        plan = build_pdf_sample_plan(request, item)[0]
        responses = {url: {"body": valid_pdf()} for url in plan.urls}
        with tempfile.TemporaryDirectory() as directory:
            bundles, fetcher = self._bundles(
                directory, item, plan.urls, responses
            )
            result = verify_pdf_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=bundles,
                verified_at=NOW,
            )
            replay = verify_pdf_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=list(reversed(bundles)),
                verified_at="2026-07-14T20:00:00Z",
            )

        self.assertEqual(result["overall_status"], "verified")
        self.assertEqual(result["findings"][0]["status"], "verified")
        self.assertEqual(result["findings"][0]["metrics"], {
            "pdf_sampled_count": 3,
            "pdf_valid_count": 3,
        })
        self.assertEqual(result["verified_facets"]["pdf_status"]["value"], "ready")
        self.assertEqual(result["verification_id"], replay["verification_id"])
        self.assertEqual(len(fetcher.requests), 3)
        self.assertTrue(all(
            observation["permission"] == "pdf_fetch_for_processing"
            for observation in result["source_observations"]
        ))
        self.assertNotIn("redistribute_pdf", str(result))
        self.assertNotIn("action", result)
        validate_verification_result(result, request, item)

    def test_missing_sample_is_partial_but_never_ready(self):
        item = discovery()
        request = request_for(item)
        plan = build_pdf_sample_plan(request, item)[0]
        present = plan.urls[:2]
        with tempfile.TemporaryDirectory() as directory:
            bundles, _ = self._bundles(
                directory,
                item,
                present,
                {url: {"body": valid_pdf()} for url in present},
            )
            result = verify_pdf_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=bundles,
                verified_at=NOW,
            )

        self.assertEqual(result["overall_status"], "partially_verified")
        self.assertEqual(result["findings"][0]["status"], "review_required")
        self.assertEqual(
            result["findings"][0]["reason_code"], "pdf_sample_incomplete"
        )
        self.assertEqual(result["verified_facets"]["pdf_status"]["value"], "partial")

    def test_invalid_samples_cannot_verify_and_use_specific_reasons(self):
        invalid_bodies = (
            ({"status": 404, "body": valid_pdf()}, "pdf_http_status"),
            ({"body": b"%PDF-short"}, "pdf_too_small"),
            (
                {"body": fixture("sanitized-not-pdf.html") + b"x" * MIN_PDF_BYTES},
                "pdf_signature_invalid",
            ),
        )
        for invalid_response, reason in invalid_bodies:
            with self.subTest(reason=reason):
                item = discovery((URLS[0],))
                request = request_for(item)
                with tempfile.TemporaryDirectory() as directory:
                    bundles, _ = self._bundles(
                        directory,
                        item,
                        (URLS[0],),
                        {URLS[0]: invalid_response},
                    )
                    result = verify_pdf_evidence(
                        request,
                        item,
                        catalog=load_venue_catalog(),
                        evidence=bundles,
                        verified_at=NOW,
                    )
                self.assertEqual(result["overall_status"], "rejected")
                self.assertEqual(result["findings"][0]["reason_code"], reason)
                self.assertIsNone(result["verified_facets"]["pdf_status"])

    def test_unsafe_url_and_untrusted_final_source_fail_closed(self):
        unsafe_item = discovery(("https://icml.cc:444/paper.pdf",))
        unsafe_request = request_for(unsafe_item)
        unsafe_result = verify_pdf_evidence(
            unsafe_request,
            unsafe_item,
            catalog=load_venue_catalog(),
            evidence=[],
            verified_at=NOW,
        )
        self.assertEqual(unsafe_result["overall_status"], "rejected")
        self.assertEqual(
            unsafe_result["findings"][0]["reason_code"], "pdf_invalid_url"
        )

        final = "https://approved-cdn.example.test/paper"
        item = discovery((URLS[0],))
        request = request_for(item)
        fetcher = MappingFetcher({
            URLS[0]: {"status": 302, "headers": {"Location": final}},
            final: {"body": valid_pdf()},
        })
        gate = CrawlPolicyGate(policy_with(
            domain_policy(
                "icml.cc", "pdf_fetch_for_processing", "store_internal_copy"
            ),
            domain_policy(
                "approved-cdn.example.test",
                "pdf_fetch_for_processing",
                "store_internal_copy",
            ),
        ))
        with tempfile.TemporaryDirectory() as directory:
            bundle = fetch_pdf_evidence(
                gate=gate,
                fetcher=fetcher,
                snapshot_store=FileSnapshotStore(Path(directory)),
                catalog=load_venue_catalog(),
                venue_id="icml",
                year=2026,
                discovery_id=item["discovery_id"],
                initial_url=URLS[0],
            )
            result = verify_pdf_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=[bundle],
                verified_at=NOW,
            )
        self.assertEqual(result["overall_status"], "review_required")
        self.assertEqual(
            result["findings"][0]["reason_code"], "unsupported_source_shape"
        )

    def test_unselected_identity_and_forged_evidence_are_rejected(self):
        item = discovery()
        request = request_for(item)
        selected = build_pdf_sample_plan(request, item)[0].urls
        unselected = next(url for url in URLS if url not in selected)
        with tempfile.TemporaryDirectory() as directory:
            bundles, _ = self._bundles(
                directory,
                item,
                (unselected,),
                {unselected: {"body": valid_pdf()}},
            )
            with self.assertRaisesRegex(PdfVerificationError, "not selected"):
                verify_pdf_evidence(
                    request,
                    item,
                    catalog=load_venue_catalog(),
                    evidence=bundles,
                    verified_at=NOW,
                )

        selected_url = selected[0]
        with tempfile.TemporaryDirectory() as directory:
            bundles, _ = self._bundles(
                directory,
                item,
                (selected_url,),
                {selected_url: {"body": valid_pdf()}},
            )
            bundle = bundles[0]
            with self.assertRaisesRegex(PdfVerificationError, "identity"):
                verify_pdf_evidence(
                    request,
                    item,
                    catalog=load_venue_catalog(),
                    evidence=[replace(bundle, year=2025)],
                    verified_at=NOW,
                )
            forged_hop = replace(
                bundle.final_hop,
                classification=replace(
                    bundle.final_hop.classification,
                    trust=SourceTrust.ARCHIVAL,
                    catalog_domain="icml.cc",
                ),
            )
            forged = replace(bundle, hops=(forged_hop,))
            with self.assertRaisesRegex(PdfVerificationError, "classification"):
                verify_pdf_evidence(
                    request,
                    item,
                    catalog=load_venue_catalog(),
                    evidence=[forged],
                    verified_at=NOW,
                )

    def test_module_has_no_live_html_state_or_orchestration_dependency(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
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
        self.assertTrue({
            "requests", "urllib3", "sqlite3", "prefect", "google", "bs4",
            "html",
        }.isdisjoint(imports))
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("REDISTRIBUTE_PDF", source)
        self.assertNotIn("queue_", source)


if __name__ == "__main__":
    unittest.main()
