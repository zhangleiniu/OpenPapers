import ast
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from automation.configuration import load_policy_config, load_venue_catalog
from automation.html_verification import (
    COLT_PMLR_VOLUME_PROFILE,
    MAX_HTML_BYTES,
    ElementSelector,
    HtmlEvidence,
    HtmlVerificationError,
    HtmlVerificationProfile,
    RedirectChainError,
    analyze_html,
    extract_pmlr_pdf_urls,
    fetch_html_evidence,
    verify_html_evidence,
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


FIXTURES = Path(__file__).with_name("fixtures") / "phase2" / "html"
MODULE = Path(__file__).resolve().parents[1] / "html_verification.py"
NOW = datetime(2026, 7, 13, 19, 30, tzinfo=timezone.utc)

IJCAI_URL = "https://2026.ijcai.org/accepted-papers/"
EMNLP_URL = "https://aclanthology.org/events/emnlp-2026/"
NAACL_CONTAMINATION_URL = "https://aclanthology.org/events/acl-2026/"

IJCAI_PROFILE = HtmlVerificationProfile(
    paper_entry_selector=ElementSelector("li", ("ij-paper",)),
    paper_title_selector=ElementSelector("span", ("ij-ptitle",)),
    paper_author_selector=ElementSelector("span", ("ij-author",)),
    paper_abstract_selector=ElementSelector("div", ("ij-abstract",)),
    minimum_paper_count=3,
    maximum_paper_count=2_000,
)
PROCEEDINGS_PROFILE = HtmlVerificationProfile(
    proceedings_entry_selector=ElementSelector(
        "article", ("proceedings-volume",)
    ),
    minimum_proceedings_count=1,
    proceedings_status="archival",
)


def fixture(name):
    return (FIXTURES / name).read_bytes()


def domain_policy(domain, *, max_requests=10):
    return {
        "domain": domain,
        "classification": "approved",
        "allowed_permissions": ["metadata_fetch"],
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
        return FetchResponse(
            requested_url=request.url,
            status_code=response.get("status", 200),
            headers=response.get(
                "headers", {"Content-Type": "text/html; charset=utf-8"}
            ),
            body=response.get("body", b""),
            fetched_at=response.get("fetched_at", "2026-07-13T19:30:00Z"),
        )


def colt_pmlr_listing(count=181, *, extra_link=""):
    papers = "".join(
        "<div class='paper'><p class='title'>Sanitized Paper "
        f"{index}</p><span class='authors'>Author {index}</span>"
        f"<a href='paper{index}/paper{index}.pdf'>Download PDF</a></div>"
        for index in range(count)
    )
    return (
        "<html><title>Proceedings of the Thirty-Eighth Conference on "
        "Learning Theory 2025</title><h1>COLT 2025</h1>"
        f"{papers}{extra_link}</html>"
    ).encode("utf-8")


def discovery(venue_id, year, url, claim_kinds=(), *, milestone=None):
    statuses = {
        "conference_status": "scheduled",
        "paper_list_status": "released" if "paper_list" in claim_kinds else "unknown",
        "metadata_status": "ready" if "metadata" in claim_kinds else "unknown",
        "pdf_status": "ready" if "pdf" in claim_kinds else "unknown",
        "proceedings_status": (
            "archival" if "proceedings" in claim_kinds else "unknown"
        ),
    }
    claims = [
        {
            "claim_id": f"claim:{venue_id}:{year}:{kind}",
            "claim_kind": kind,
            "statement": f"Fixture claim for {kind}.",
            "evidence_urls": [url],
            "source_type": "archival" if "aclanthology.org" in url else "official",
            "published_at": None,
        }
        for kind in claim_kinds
    ]
    payload = {
        "schema_version": 1,
        "discovery_id": f"discovery:{venue_id}:{year}:html-fixture",
        "venue_id": venue_id,
        "year": year,
        "checked_at": "2026-07-13T19:00:00Z",
        "provider": "fixture-provider",
        "model": "fixture-model",
        "prompt_version": "v1",
        **statuses,
        "claims": claims,
        "candidate_milestones": [milestone] if milestone is not None else [],
        "confidence": 0.8,
        "uncertainties": [],
        "evidence_fingerprint": "b" * 64,
    }
    return payload


def response(url, body, *, status=200, content_type="text/html; charset=utf-8"):
    return FetchResponse(
        requested_url=url,
        status_code=status,
        headers={"Content-Type": content_type},
        body=body,
        fetched_at="2026-07-13T19:30:00Z",
    )


class RedirectCoordinatorTests(unittest.TestCase):
    def test_each_redirect_hop_is_independently_classified_gated_and_retained(self):
        initial = "https://www.ijcai.org/start"
        final = "https://mirror.example.test/ijcai-2026"
        fetcher = MappingFetcher({
            initial: {
                "status": 302,
                "headers": {"Location": final, "Content-Type": "text/html"},
            },
            final: {"body": fixture("ijcai-2026-accepted.html")},
        })
        gate = CrawlPolicyGate(policy_with(
            domain_policy("ijcai.org"),
            domain_policy("mirror.example.test"),
        ))
        catalog = load_venue_catalog()
        with tempfile.TemporaryDirectory() as directory:
            store = FileSnapshotStore(Path(directory))
            bundle = fetch_html_evidence(
                gate=gate,
                fetcher=fetcher,
                snapshot_store=store,
                catalog=catalog,
                venue_id="ijcai",
                year=2026,
                discovery_id="discovery:ijcai:2026:redirect",
                initial_url=initial,
            )
            replay = fetch_html_evidence(
                gate=CrawlPolicyGate(policy_with(
                    domain_policy("ijcai.org"),
                    domain_policy("mirror.example.test"),
                )),
                fetcher=MappingFetcher({
                    initial: {
                        "status": 302,
                        "headers": {
                            "Location": final,
                            "Content-Type": "text/html",
                        },
                    },
                    final: {"body": fixture("ijcai-2026-accepted.html")},
                }),
                snapshot_store=store,
                catalog=catalog,
                venue_id="ijcai",
                year=2026,
                discovery_id="discovery:ijcai:2026:redirect",
                initial_url=initial,
            )

            self.assertEqual([item.url for item in fetcher.requests], [initial, final])
            self.assertFalse(any(item.follow_redirects for item in fetcher.requests))
            self.assertEqual(gate.request_count("ijcai.org"), 1)
            self.assertEqual(gate.request_count("mirror.example.test"), 1)
            self.assertEqual(len(bundle.hops), 2)
            self.assertEqual(bundle.hops[0].classification.trust.value, "official")
            self.assertEqual(bundle.hops[1].classification.trust.value, "untrusted")
            self.assertTrue(all(hop.snapshot.manifest_path.is_file() for hop in bundle.hops))
            self.assertEqual(
                [hop.snapshot.snapshot_id for hop in bundle.hops],
                [hop.snapshot.snapshot_id for hop in replay.hops],
            )

    def test_unapproved_redirect_target_stops_before_request_and_keeps_first_hop(self):
        initial = "https://www.ijcai.org/start"
        blocked = "https://blocked.example.test/final"
        fetcher = MappingFetcher({
            initial: {
                "status": 302,
                "headers": {"Location": blocked, "Content-Type": "text/html"},
            },
        })
        gate = CrawlPolicyGate(policy_with(domain_policy("ijcai.org")))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RedirectChainError) as caught:
                fetch_html_evidence(
                    gate=gate,
                    fetcher=fetcher,
                    snapshot_store=FileSnapshotStore(Path(directory)),
                    catalog=load_venue_catalog(),
                    venue_id="ijcai",
                    year=2026,
                    discovery_id="discovery:ijcai:2026:blocked",
                    initial_url=initial,
                )

            self.assertEqual([item.url for item in fetcher.requests], [initial])
            self.assertEqual(caught.exception.blocked_url, blocked)
            self.assertEqual(len(caught.exception.hops), 1)
            self.assertTrue(caught.exception.hops[0].snapshot.manifest_path.is_file())

    def test_redirect_loop_and_limit_never_request_the_blocked_next_hop(self):
        first = "https://www.ijcai.org/first"
        second = "https://www.ijcai.org/second"
        responses = {
            first: {
                "status": 302,
                "headers": {"Location": second, "Content-Type": "text/html"},
            },
            second: {
                "status": 302,
                "headers": {"Location": first, "Content-Type": "text/html"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            loop_fetcher = MappingFetcher(responses)
            with self.assertRaisesRegex(RedirectChainError, "loop") as loop:
                fetch_html_evidence(
                    gate=CrawlPolicyGate(policy_with(domain_policy("ijcai.org"))),
                    fetcher=loop_fetcher,
                    snapshot_store=FileSnapshotStore(Path(directory) / "loop"),
                    catalog=load_venue_catalog(),
                    venue_id="ijcai",
                    year=2026,
                    discovery_id="discovery:ijcai:2026:loop",
                    initial_url=first,
                )
            self.assertEqual([item.url for item in loop_fetcher.requests], [first, second])
            self.assertEqual(len(loop.exception.hops), 2)

            limit_fetcher = MappingFetcher(responses)
            with self.assertRaisesRegex(RedirectChainError, "limit") as limit:
                fetch_html_evidence(
                    gate=CrawlPolicyGate(policy_with(domain_policy("ijcai.org"))),
                    fetcher=limit_fetcher,
                    snapshot_store=FileSnapshotStore(Path(directory) / "limit"),
                    catalog=load_venue_catalog(),
                    venue_id="ijcai",
                    year=2026,
                    discovery_id="discovery:ijcai:2026:limit",
                    initial_url=first,
                    max_redirects=0,
                )
            self.assertEqual([item.url for item in limit_fetcher.requests], [first])
            self.assertEqual(limit.exception.blocked_url, second)


class HtmlAnalysisTests(unittest.TestCase):
    def test_colt_pmlr_profile_requires_identity_and_plausible_distinct_count(self):
        response = FetchResponse(
            requested_url="https://proceedings.mlr.press/v291/",
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=colt_pmlr_listing(),
            fetched_at="2026-07-13T19:30:00Z",
        )
        analysis = analyze_html(
            response,
            catalog=load_venue_catalog(),
            venue_id="colt",
            year=2025,
            profile=COLT_PMLR_VOLUME_PROFILE,
        )
        self.assertTrue(analysis.identity_matches)
        self.assertEqual(analysis.paper_count, 181)
        self.assertEqual(analysis.proceedings_count, 181)
        urls = extract_pmlr_pdf_urls(response)
        self.assertEqual(len(urls), 181)
        self.assertTrue(all(url.startswith(
            "https://proceedings.mlr.press/v291/"
        ) for url in urls))

        unsafe = replace(
            response,
            body=colt_pmlr_listing(
                extra_link=(
                    "<div class='paper'><a href='https://example.test/evil.pdf'>"
                    "Download PDF</a></div>"
                )
            ),
        )
        with self.assertRaisesRegex(HtmlVerificationError, "unsafe PDF link"):
            extract_pmlr_pdf_urls(unsafe)

        encoded_escape = replace(
            response,
            body=colt_pmlr_listing(
                extra_link=(
                    "<div class='paper'><a href='%2e%2e/v292/evil.pdf'>"
                    "Download PDF</a></div>"
                )
            ),
        )
        with self.assertRaisesRegex(HtmlVerificationError, "unsafe PDF link"):
            extract_pmlr_pdf_urls(encoded_escape)

    def test_ijcai_fixture_has_exact_identity_date_distinct_list_and_metadata(self):
        analysis = analyze_html(
            response(IJCAI_URL, fixture("ijcai-2026-accepted.html")),
            catalog=load_venue_catalog(),
            venue_id="ijcai",
            year=2026,
            profile=IJCAI_PROFILE,
        )

        self.assertTrue(analysis.identity_matches)
        self.assertIn("2026-08-15", analysis.observed_dates)
        self.assertEqual(analysis.paper_count, 3)
        self.assertEqual(analysis.announced_complete_count, 3)
        self.assertEqual(analysis.metadata_complete_count, 3)

        wrong_year = analyze_html(
            response(
                IJCAI_URL,
                fixture("ijcai-2026-accepted.html").replace(b"2026", b"2025"),
            ),
            catalog=load_venue_catalog(),
            venue_id="ijcai",
            year=2026,
            profile=IJCAI_PROFILE,
        )
        self.assertTrue(wrong_year.venue_present)
        self.assertFalse(wrong_year.identity_matches)

    def test_naacl_requires_token_bounded_venue_identity_not_acl_substring(self):
        analysis = analyze_html(
            response(
                NAACL_CONTAMINATION_URL,
                fixture("naacl-2026-acl-contamination.html"),
            ),
            catalog=load_venue_catalog(),
            venue_id="naacl",
            year=2026,
            profile=PROCEEDINGS_PROFILE,
        )

        self.assertFalse(analysis.venue_present)
        self.assertFalse(analysis.identity_matches)
        self.assertEqual(analysis.proceedings_count, 1)

    def test_future_prose_is_not_a_current_proceedings_index(self):
        future = analyze_html(
            response(EMNLP_URL, fixture("emnlp-2026-future.html")),
            catalog=load_venue_catalog(),
            venue_id="emnlp",
            year=2026,
            profile=PROCEEDINGS_PROFILE,
        )
        current = analyze_html(
            response(EMNLP_URL, fixture("emnlp-2026-current-index.html")),
            catalog=load_venue_catalog(),
            venue_id="emnlp",
            year=2026,
            profile=PROCEEDINGS_PROFILE,
        )

        self.assertTrue(future.identity_matches)
        self.assertEqual(future.proceedings_count, 0)
        self.assertEqual(current.proceedings_count, 2)

    def test_non_html_oversized_and_malformed_inputs_fail_closed(self):
        catalog = load_venue_catalog()
        with self.assertRaisesRegex(HtmlVerificationError, "recognized HTML"):
            analyze_html(
                response(IJCAI_URL, b"%PDF-fixture", content_type="application/pdf"),
                catalog=catalog,
                venue_id="ijcai",
                year=2026,
                profile=IJCAI_PROFILE,
            )
        with self.assertRaisesRegex(HtmlVerificationError, "byte limit"):
            analyze_html(
                response(IJCAI_URL, b"x" * (MAX_HTML_BYTES + 1)),
                catalog=catalog,
                venue_id="ijcai",
                year=2026,
                profile=IJCAI_PROFILE,
            )
        with self.assertRaisesRegex(HtmlVerificationError, "duplicate attributes"):
            analyze_html(
                response(
                    IJCAI_URL,
                    b"<html><title>IJCAI 2026</title><div id='a' id='b'></div></html>",
                ),
                catalog=catalog,
                venue_id="ijcai",
                year=2026,
                profile=IJCAI_PROFILE,
            )


class HtmlResultTests(unittest.TestCase):
    def _evidence(self, directory, discovery_payload, url, body, profile):
        host = url.split("/", 3)[2]
        policy_domain = (
            "aclanthology.org" if host.endswith("aclanthology.org") else "ijcai.org"
        )
        bundle = fetch_html_evidence(
            gate=CrawlPolicyGate(policy_with(domain_policy(policy_domain))),
            fetcher=MappingFetcher({url: {"body": body}}),
            snapshot_store=FileSnapshotStore(Path(directory)),
            catalog=load_venue_catalog(),
            venue_id=discovery_payload["venue_id"],
            year=discovery_payload["year"],
            discovery_id=discovery_payload["discovery_id"],
            initial_url=url,
        )
        return HtmlEvidence(bundle, profile)

    def test_ijcai_list_and_metadata_verify_without_granting_pdf_readiness(self):
        item = discovery(
            "ijcai", 2026, IJCAI_URL, ("paper_list", "metadata", "pdf")
        )
        request = build_verification_request(
            item,
            requested_at=NOW,
            claim_ids=[
                "claim:ijcai:2026:paper_list",
                "claim:ijcai:2026:metadata",
            ],
            candidate_milestone_ids=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence(
                directory,
                item,
                IJCAI_URL,
                fixture("ijcai-2026-accepted.html"),
                IJCAI_PROFILE,
            )
            result = verify_html_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=[evidence],
                verified_at=NOW,
            )
            replay = verify_html_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=[evidence],
                verified_at="2026-07-14T19:30:00Z",
            )

        self.assertEqual(result["overall_status"], "verified")
        self.assertEqual(
            result["verified_facets"]["paper_list_status"]["value"],
            "released",
        )
        self.assertEqual(
            result["verified_facets"]["metadata_status"]["value"],
            "ready",
        )
        self.assertIsNone(result["verified_facets"]["pdf_status"])
        self.assertNotIn(
            "pdf",
            {finding["verification_kind"] for finding in result["findings"]},
        )
        self.assertTrue(all(
            finding["metrics"]["paper_count"] == 3
            for finding in result["findings"]
        ))
        self.assertEqual(result["verification_id"], replay["verification_id"])
        validate_verification_result(result, request, item)

    def test_exact_candidate_date_produces_a_verified_milestone(self):
        milestone = {
            "milestone_id": "milestone:ijcai:2026:end",
            "milestone_type": "conference_end",
            "date": "2026-08-15",
            "evidence_urls": [IJCAI_URL],
            "source_type": "official",
        }
        item = discovery("ijcai", 2026, IJCAI_URL, (), milestone=milestone)
        request = build_verification_request(
            item,
            requested_at=NOW,
            claim_ids=[],
            candidate_milestone_ids=[milestone["milestone_id"]],
        )
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence(
                directory,
                item,
                IJCAI_URL,
                fixture("ijcai-2026-accepted.html"),
                IJCAI_PROFILE,
            )
            result = verify_html_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=[evidence],
                verified_at=NOW,
            )

        self.assertEqual(result["overall_status"], "verified")
        self.assertEqual(
            result["verified_milestones"][0]["candidate_milestone_id"],
            milestone["milestone_id"],
        )

    def test_emnlp_future_promise_is_rejected_but_current_index_verifies(self):
        item = discovery("emnlp", 2026, EMNLP_URL, ("proceedings",))
        request = build_verification_request(
            item, requested_at=NOW, candidate_milestone_ids=[]
        )
        with tempfile.TemporaryDirectory() as directory:
            future = self._evidence(
                Path(directory) / "future",
                item,
                EMNLP_URL,
                fixture("emnlp-2026-future.html"),
                PROCEEDINGS_PROFILE,
            )
            current = self._evidence(
                Path(directory) / "current",
                item,
                EMNLP_URL,
                fixture("emnlp-2026-current-index.html"),
                PROCEEDINGS_PROFILE,
            )
            rejected = verify_html_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=[future],
                verified_at=NOW,
            )
            verified = verify_html_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=[current],
                verified_at=NOW,
            )

        self.assertEqual(rejected["overall_status"], "rejected")
        self.assertEqual(
            rejected["findings"][0]["reason_code"],
            "proceedings_not_found",
        )
        self.assertIsNone(rejected["verified_facets"]["proceedings_status"])
        self.assertEqual(verified["overall_status"], "verified")
        self.assertEqual(
            verified["verified_facets"]["proceedings_status"]["value"],
            "archival",
        )

    def test_acl_page_cannot_verify_naacl_identity_even_with_an_index(self):
        item = discovery(
            "naacl", 2026, NAACL_CONTAMINATION_URL, ("proceedings",)
        )
        request = build_verification_request(
            item, requested_at=NOW, candidate_milestone_ids=[]
        )
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence(
                directory,
                item,
                NAACL_CONTAMINATION_URL,
                fixture("naacl-2026-acl-contamination.html"),
                PROCEEDINGS_PROFILE,
            )
            result = verify_html_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=[evidence],
                verified_at=NOW,
            )

        self.assertEqual(result["overall_status"], "rejected")
        self.assertEqual(result["findings"][0]["reason_code"], "identity_mismatch")
        self.assertIsNone(result["verified_facets"]["proceedings_status"])

    def test_incomplete_metadata_is_partial_and_disagreeing_counts_conflict(self):
        item = discovery("ijcai", 2026, IJCAI_URL, ("metadata",))
        request = build_verification_request(
            item, requested_at=NOW, candidate_milestone_ids=[]
        )
        complete_body = fixture("ijcai-2026-accepted.html")
        incomplete_body = complete_body.replace(
            b"<div class=\"ij-abstract\">A complete abstract for the second paper.</div>",
            b"",
        )
        shorter_body = complete_body.replace(
            (
                b"<li class=\"ij-paper\">\n"
                b"          <span class=\"ij-ptitle\">A Reliable First Paper</span>\n"
                b"          <span class=\"ij-author\">Ada Lovelace</span>\n"
                b"          <div class=\"ij-abstract\">A complete abstract for the "
                b"first paper.</div>\n"
                b"        </li>"
            ),
            b"",
        )
        with tempfile.TemporaryDirectory() as directory:
            incomplete = self._evidence(
                Path(directory) / "incomplete",
                item,
                IJCAI_URL,
                incomplete_body,
                IJCAI_PROFILE,
            )
            complete = self._evidence(
                Path(directory) / "complete",
                item,
                IJCAI_URL,
                complete_body,
                IJCAI_PROFILE,
            )
            shorter_profile = HtmlVerificationProfile(
                paper_entry_selector=IJCAI_PROFILE.paper_entry_selector,
                paper_title_selector=IJCAI_PROFILE.paper_title_selector,
                paper_author_selector=IJCAI_PROFILE.paper_author_selector,
                paper_abstract_selector=IJCAI_PROFILE.paper_abstract_selector,
                minimum_paper_count=2,
                maximum_paper_count=2_000,
            )
            shorter = self._evidence(
                Path(directory) / "shorter",
                item,
                IJCAI_URL,
                shorter_body,
                shorter_profile,
            )
            partial = verify_html_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=[incomplete],
                verified_at=NOW,
            )
            conflicting = verify_html_evidence(
                request,
                item,
                catalog=load_venue_catalog(),
                evidence=[complete, shorter],
                verified_at=NOW,
            )

        self.assertEqual(partial["overall_status"], "partially_verified")
        self.assertEqual(
            partial["findings"][0]["reason_code"],
            "metadata_incomplete",
        )
        self.assertEqual(
            partial["verified_facets"]["metadata_status"]["value"],
            "partial",
        )
        self.assertEqual(conflicting["overall_status"], "conflicting")
        self.assertEqual(
            conflicting["findings"][0]["reason_code"],
            "conflicting_evidence",
        )

    def test_pdf_targets_and_uncited_html_are_rejected_at_the_scope_boundary(self):
        pdf_item = discovery("ijcai", 2026, IJCAI_URL, ("pdf",))
        pdf_request = build_verification_request(
            pdf_item, requested_at=NOW, candidate_milestone_ids=[]
        )
        with self.assertRaisesRegex(HtmlVerificationError, "outside P2.2"):
            verify_html_evidence(
                pdf_request,
                pdf_item,
                catalog=load_venue_catalog(),
                evidence=[],
                verified_at=NOW,
            )

        list_item = discovery("ijcai", 2026, IJCAI_URL, ("paper_list",))
        list_request = build_verification_request(
            list_item, requested_at=NOW, candidate_milestone_ids=[]
        )
        other_url = "https://2026.ijcai.org/not-cited/"
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence(
                directory,
                list_item,
                other_url,
                fixture("ijcai-2026-accepted.html"),
                IJCAI_PROFILE,
            )
            with self.assertRaisesRegex(HtmlVerificationError, "not cited"):
                verify_html_evidence(
                    list_request,
                    list_item,
                    catalog=load_venue_catalog(),
                    evidence=[evidence],
                    verified_at=NOW,
                )

    def test_forged_catalog_classification_is_rejected_before_result_building(self):
        item = discovery("ijcai", 2026, IJCAI_URL, ("paper_list",))
        request = build_verification_request(
            item, requested_at=NOW, candidate_milestone_ids=[]
        )
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence(
                directory,
                item,
                IJCAI_URL,
                fixture("ijcai-2026-accepted.html"),
                IJCAI_PROFILE,
            )
            hop = evidence.bundle.final_hop
            forged_hop = replace(
                hop,
                classification=replace(
                    hop.classification,
                    trust=SourceTrust.ARCHIVAL,
                    catalog_domain="ijcai.org",
                ),
            )
            forged = HtmlEvidence(
                replace(evidence.bundle, hops=(forged_hop,)),
                evidence.profile,
            )
            with self.assertRaisesRegex(
                HtmlVerificationError, "classification"
            ):
                verify_html_evidence(
                    request,
                    item,
                    catalog=load_venue_catalog(),
                    evidence=[forged],
                    verified_at=NOW,
                )

    def test_module_has_no_live_network_pdf_state_or_orchestration_dependency(self):
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
        self.assertTrue(
            {
                "requests", "urllib3", "sqlite3", "prefect", "google", "bs4",
            }.isdisjoint(imports)
        )
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("%PDF-", source)
        self.assertNotIn("queue_", source)


if __name__ == "__main__":
    unittest.main()
