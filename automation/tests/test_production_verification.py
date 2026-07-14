import ast
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.configuration import load_policy_config, load_venue_catalog
from automation.live_fetch import LiveFetchError
from automation.local_control_plane import run_local_control_wakeup
from automation.production_verification import (
    PRODUCTION_CRAWL_POLICY_PATH,
    AutomaticVerificationConfig,
    AutomaticVerificationGuardPolicy,
    AutomaticVerificationHealthLedger,
    AutomaticVerificationLedgerError,
    ProductionCrawlPolicyError,
    ProductionVerificationEffect,
    load_production_crawl_policy,
)
from automation.tests.test_local_control_plane import (
    FakeDiscovery,
    due_state,
    seed_local_state,
)
from automation.verification import FetchRequest, FetchResponse, validate_verification_result


MODULE = Path(__file__).resolve().parents[1] / "production_verification.py"
NOW = datetime(2026, 7, 14, 21, 30, tzinfo=timezone.utc)


def discovery(
    *,
    venue_id="icml",
    year=2026,
    kind="conference",
    urls=("https://icml.cc/openpapers-fixture/2026/",),
):
    statuses = {
        "conference_status": "scheduled",
        "paper_list_status": "released" if kind == "paper_list" else "unknown",
        "metadata_status": "ready" if kind == "metadata" else "unknown",
        "pdf_status": "ready" if kind == "pdf" else "unknown",
        "proceedings_status": "archival" if kind == "proceedings" else "unknown",
    }
    return {
        "schema_version": 1,
        "discovery_id": f"discovery:{venue_id}:{year}:p27-fixture:{kind}",
        "venue_id": venue_id,
        "year": year,
        "checked_at": "2026-07-14T21:00:00Z",
        "provider": "fixture-provider",
        "model": "fixture-model",
        "prompt_version": "v1",
        **statuses,
        "claims": [{
            "claim_id": f"claim:{venue_id}:{year}:{kind}",
            "claim_kind": kind,
            "statement": f"Fixture claim for {kind}.",
            "evidence_urls": list(urls),
            "source_type": (
                "archival"
                if any(
                    host in url
                    for host in (
                        "aclanthology.org", "openreview.net", "mlr.press"
                    )
                    for url in urls
                )
                else "official"
            ),
            "published_at": None,
        }],
        "candidate_milestones": [],
        "confidence": 0.9,
        "uncertainties": [],
        "evidence_fingerprint": "d" * 64,
    }


def html_response(url, *, body=None, status=200, headers=None):
    return FetchResponse(
        requested_url=url,
        status_code=status,
        headers=headers or {"Content-Type": "text/html; charset=utf-8"},
        body=(
            body
            if body is not None
            else b"<html><title>ICML 2026</title><h1>International Conference on Machine Learning 2026</h1></html>"
        ),
        fetched_at="2026-07-14T21:30:00Z",
    )


class MappingFetcher:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.requests = []

    def fetch(self, request: FetchRequest) -> FetchResponse:
        self.requests.append(request)
        outcome = self.responses[request.url]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ProductionVerificationTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.catalog = load_venue_catalog()
        self.addCleanup(self._temp_dir.cleanup)

    def config(self, *, review_path=PRODUCTION_CRAWL_POLICY_PATH):
        return AutomaticVerificationConfig(
            snapshot_root=self.root / "snapshots",
            health_ledger_path=self.root / "verification-health.json",
            policy_review_path=review_path,
        )

    def effect(self, fetcher, *, review_path=PRODUCTION_CRAWL_POLICY_PATH):
        return ProductionVerificationEffect(
            self.config(review_path=review_path),
            _fetcher=fetcher,
            _catalog=self.catalog,
        )

    def write_review(self, mutate):
        payload = json.loads(PRODUCTION_CRAWL_POLICY_PATH.read_text(encoding="utf-8"))
        mutate(payload)
        path = self.root / "production-policy.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def assert_strict_bundles(self, bundles, source):
        self.assertTrue(bundles)
        for bundle in bundles:
            validate_verification_result(bundle.result, bundle.request, source)

    # -- complete dated production review ---------------------------------

    def test_review_covers_catalog_and_every_required_dimension(self):
        policy = load_production_crawl_policy(observed_at=NOW)
        expected = {
            domain
            for venue in self.catalog["venues"]
            for field in ("official_domains", "archival_domains")
            for domain in venue[field]
        } | {"vertexaisearch.cloud.google.com"}
        domains = {item["domain"]: item for item in policy["crawl"]["domains"]}
        self.assertEqual(set(domains), expected)
        self.assertEqual(domains["ecva.net"]["classification"], "review_required")
        self.assertEqual(
            domains["vertexaisearch.cloud.google.com"]["classification"],
            "denied",
        )
        self.assertEqual(domains["aaai.org"]["minimum_delay_seconds"], 43200.0)
        self.assertTrue(all(item["honor_retry_after"] for item in domains.values()))
        self.assertTrue(all(item["stop_on_captcha"] for item in domains.values()))
        self.assertFalse(any(
            permission.startswith("redistribute_")
            for item in domains.values()
            for permission in item["allowed_permissions"]
        ))

    def test_missing_review_dimension_and_stale_review_fail_closed(self):
        path = self.write_review(
            lambda payload: payload["domains"][0]["runtime"].pop("cache_policy")
        )
        with self.assertRaises(ProductionCrawlPolicyError):
            load_production_crawl_policy(path, observed_at=NOW)
        with self.assertRaisesRegex(ProductionCrawlPolicyError, "stale"):
            load_production_crawl_policy(
                observed_at=NOW + timedelta(days=91)
            )

    def test_review_cannot_grant_pdf_redistribution(self):
        def mutate(payload):
            item = next(
                entry for entry in payload["domains"]
                if entry["domain"] == "proceedings.mlr.press"
            )
            item["retention"]["redistribute_pdf"] = True
            item["runtime"]["allowed_permissions"].append("redistribute_pdf")

        path = self.write_review(mutate)
        with self.assertRaisesRegex(ProductionCrawlPolicyError, "redistribution"):
            load_production_crawl_policy(path, observed_at=NOW)

    # -- deterministic HTML/PDF verifier composition ----------------------

    def test_html_bundle_is_strict_and_uses_allowed_classified_source(self):
        source = discovery()
        url = source["claims"][0]["evidence_urls"][0]
        fetcher = MappingFetcher({url: html_response(url)})
        bundles = self.effect(fetcher).verify(source, observed_at=NOW)
        self.assert_strict_bundles(bundles, source)
        observations = bundles[0].result["source_observations"]
        self.assertTrue(observations)
        self.assertTrue(all(item["policy_decision"] == "allowed" for item in observations))
        self.assertTrue(all(item["source_trust"] == "official" for item in observations))
        self.assertEqual(len(fetcher.requests), 1)

    def test_compatible_v1_claim_without_kind_uses_source_identity(self):
        source = discovery()
        del source["claims"][0]["claim_kind"]
        url = source["claims"][0]["evidence_urls"][0]
        bundles = self.effect(MappingFetcher({url: html_response(url)})).verify(
            source, observed_at=NOW
        )
        self.assert_strict_bundles(bundles, source)
        self.assertEqual(bundles[0].request["verification_kinds"], ["source_identity"])

    def test_pdf_bundle_uses_bounded_signature_sampling_and_internal_permission(self):
        url = "https://proceedings.mlr.press/v1/openpapers-fixture.pdf"
        source = discovery(kind="pdf", urls=(url,))
        body = b"%PDF-1.7\n" + b"x" * 2048
        fetcher = MappingFetcher({
            url: FetchResponse(
                requested_url=url,
                status_code=200,
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Length": str(len(body)),
                },
                body=body,
                fetched_at="2026-07-14T21:30:00Z",
            )
        })
        bundles = self.effect(fetcher).verify(source, observed_at=NOW)
        self.assert_strict_bundles(bundles, source)
        self.assertEqual(bundles[0].result["verified_facets"]["pdf_status"]["value"], "ready")
        self.assertEqual(fetcher.requests[0].permission.value, "pdf_fetch_for_processing")

    def test_redirect_hops_are_independently_gated_and_retained(self):
        initial = "https://icml.cc/openpapers-fixture/redirect"
        final = "https://proceedings.mlr.press/v1/openpapers-fixture.html"
        source = discovery(urls=(initial,))
        fetcher = MappingFetcher({
            initial: html_response(
                initial,
                status=302,
                headers={"Location": final, "Content-Type": "text/html"},
                body=b"",
            ),
            final: html_response(final),
        })
        bundles = self.effect(fetcher).verify(source, observed_at=NOW)
        self.assert_strict_bundles(bundles, source)
        self.assertEqual([item.url for item in fetcher.requests], [initial, final])
        self.assertEqual(
            [item["policy_domain"] for item in bundles[0].result["source_observations"]],
            ["icml.cc", "proceedings.mlr.press"],
        )

    def test_unknown_domain_remains_review_required_before_fetch(self):
        source = discovery(urls=("https://unknown.example/openpapers",))
        fetcher = MappingFetcher({})
        bundles = self.effect(fetcher).verify(source, observed_at=NOW)
        self.assert_strict_bundles(bundles, source)
        self.assertEqual(fetcher.requests, [])
        self.assertEqual(bundles[0].result["source_observations"], [])

    def test_domain_budget_keeps_successful_partial_evidence(self):
        urls = (
            "https://icml.cc/openpapers-fixture/one",
            "https://icml.cc/openpapers-fixture/two",
        )

        def mutate(payload):
            item = next(
                entry for entry in payload["domains"] if entry["domain"] == "icml.cc"
            )
            item["runtime"]["max_requests_per_run"] = 1

        path = self.write_review(mutate)
        source = discovery(urls=urls)
        fetcher = MappingFetcher({url: html_response(url) for url in urls})
        bundles = self.effect(fetcher, review_path=path).verify(source, observed_at=NOW)
        self.assert_strict_bundles(bundles, source)
        self.assertEqual(len(fetcher.requests), 1)
        self.assertEqual(len(bundles[0].result["source_observations"]), 1)

    # -- restart-durable failure stops -------------------------------------

    def test_429_retry_after_opens_restart_cooldown_and_blocks_refetch(self):
        source = discovery()
        url = source["claims"][0]["evidence_urls"][0]
        first_fetcher = MappingFetcher({
            url: html_response(
                url,
                status=429,
                headers={
                    "Content-Type": "text/html",
                    "Retry-After": "86400",
                },
            )
        })
        first = self.effect(first_fetcher)
        bundles = first.verify(source, observed_at=NOW)
        self.assert_strict_bundles(bundles, source)
        state = first._health.source_state("icml", 2026, "icml.cc")
        self.assertEqual(state["state"], "cooldown")
        self.assertEqual(state["category"], "http_429")
        self.assertEqual(state["deadline_at"], "2026-07-15T21:30:00Z")

        reopened_fetcher = MappingFetcher({url: html_response(url)})
        reopened = self.effect(reopened_fetcher)
        replay = reopened.verify(source, observed_at=NOW + timedelta(minutes=5))
        self.assert_strict_bundles(replay, source)
        self.assertEqual(reopened_fetcher.requests, [])

    def test_403_and_captcha_open_typed_source_cooldowns(self):
        source = discovery()
        url = source["claims"][0]["evidence_urls"][0]
        cases = (
            (html_response(url, status=403), "http_403"),
            (LiveFetchError("captcha fixture", category="captcha"), "captcha"),
        )
        for index, (response, category) in enumerate(cases):
            with self.subTest(category=category):
                config = AutomaticVerificationConfig(
                    snapshot_root=self.root / f"snapshots-{index}",
                    health_ledger_path=self.root / f"health-{index}.json",
                )
                effect = ProductionVerificationEffect(
                    config,
                    _fetcher=MappingFetcher({url: response}),
                    _catalog=self.catalog,
                )
                bundles = effect.verify(source, observed_at=NOW)
                self.assert_strict_bundles(bundles, source)
                state = effect._health.source_state("icml", 2026, "icml.cc")
                self.assertEqual(state["category"], category)

    def test_corrupt_ledger_fails_closed_before_fetch(self):
        path = self.root / "verification-health.json"
        path.write_text(json.dumps({"version": 99, "sources": {}}), encoding="utf-8")
        source = discovery()
        url = source["claims"][0]["evidence_urls"][0]
        fetcher = MappingFetcher({url: html_response(url)})
        with self.assertRaises(AutomaticVerificationLedgerError):
            self.effect(fetcher).verify(source, observed_at=NOW)
        self.assertEqual(fetcher.requests, [])

    def test_crash_safe_in_flight_claim_survives_reopen(self):
        policy = load_policy_config()
        guard = AutomaticVerificationGuardPolicy.from_policy(policy)
        ledger = AutomaticVerificationHealthLedger(
            self.root / "verification-health.json"
        )
        ledger.guard_and_claim(
            "icml", 2026, "icml.cc", at=NOW, policy=guard
        )
        source = discovery()
        url = source["claims"][0]["evidence_urls"][0]
        fetcher = MappingFetcher({url: html_response(url)})
        bundles = self.effect(fetcher).verify(
            source, observed_at=NOW + timedelta(minutes=1)
        )
        self.assert_strict_bundles(bundles, source)
        self.assertEqual(fetcher.requests, [])
        state = ledger.source_state("icml", 2026, "icml.cc")
        self.assertEqual(state["state"], "in_flight")

    def test_cooldown_expiry_allows_a_new_source_attempt(self):
        source = discovery()
        url = source["claims"][0]["evidence_urls"][0]
        first = self.effect(MappingFetcher({url: html_response(url, status=403)}))
        first.verify(source, observed_at=NOW)

        reopened_fetcher = MappingFetcher({url: html_response(url)})
        reopened = self.effect(reopened_fetcher)
        bundles = reopened.verify(
            source, observed_at=NOW + timedelta(hours=6, seconds=1)
        )
        self.assert_strict_bundles(bundles, source)
        self.assertEqual(len(reopened_fetcher.requests), 1)
        self.assertEqual(
            reopened._health.source_state("icml", 2026, "icml.cc")["state"],
            "eligible",
        )

    def test_concurrent_claims_cannot_both_open_the_same_source(self):
        ledger = AutomaticVerificationHealthLedger(
            self.root / "verification-health.json"
        )
        guard = AutomaticVerificationGuardPolicy.from_policy(load_policy_config())
        outcomes = []

        def claim():
            try:
                ledger.guard_and_claim(
                    "icml", 2026, "icml.cc", at=NOW, policy=guard
                )
            except Exception as exc:  # pragma: no cover - asserted below
                outcomes.append(type(exc).__name__)
            else:
                outcomes.append("claimed")

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["AutomaticVerificationRefused", "claimed"])

    # -- exact protocol and package boundary -------------------------------

    def test_effect_round_trips_through_local_control_wakeup(self):
        source = discovery()
        url = source["claims"][0]["evidence_urls"][0]
        effect = self.effect(MappingFetcher({url: html_response(url)}))
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            seed_local_state(state_path, state=due_state())
            outcome = run_local_control_wakeup(
                state_path,
                scheduled_for=NOW,
                clock=lambda: NOW,
                discovery_effect=FakeDiscovery(source),
                verification_effect=effect,
                catalog=self.catalog,
                policy=load_policy_config(),
            )
        self.assertFalse(outcome.replayed)
        self.assertEqual(len(outcome.selections), 1)
        self.assertEqual(len(outcome.selections[0].verification_ids), 1)

    def test_module_has_no_installed_or_execution_layer_import(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        source = MODULE.read_text(encoding="utf-8")
        for forbidden in (
            "automation.execution_pipeline",
            "automation.mac_worker",
            "automation.local_service",
            "prefect",
            "requests",
            "subprocess",
        ):
            self.assertNotIn(forbidden, imported)
            if forbidden.startswith("automation."):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
