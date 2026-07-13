import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.configuration import load_policy_config, load_venue_catalog
from automation.discovery import (
    ArtifactStore,
    BudgetExceeded,
    BudgetLimits,
    DiscoveryRequest,
    DiscoveryService,
    DiscoveryValidationError,
    GroundingSource,
    JsonBudgetLedger,
    ProviderResponse,
    RetryableProviderError,
    normalize_provider_response,
    request_from_catalog,
    safe_error_summary,
)


FIXTURE = (
    Path(__file__).with_name("fixtures")
    / "phase1"
    / "gemini-grounded-response.v1.json"
)
NOW = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)


def load_response() -> ProviderResponse:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ProviderResponse(
        body=payload["body"],
        grounding_sources=tuple(
            GroundingSource(**source)
            for source in payload["grounding_sources"]
        ),
        search_queries=tuple(payload["search_queries"]),
    )


class FakeProvider:
    name = "fake-search"
    model = "fake-model"
    prompt_version = "v1"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def discover(self, request: DiscoveryRequest) -> ProviderResponse:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SlowProvider:
    name = "slow-fake-search"
    model = "fake-model"
    prompt_version = "v1"

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def discover(self, request: DiscoveryRequest) -> ProviderResponse:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            return load_response()
        finally:
            with self.lock:
                self.active -= 1


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def limits(**overrides) -> BudgetLimits:
    configured = BudgetLimits.from_policy(load_policy_config())
    values = {
        "max_calls_per_day": configured.max_calls_per_day,
        "max_calls_per_venue_per_day": configured.max_calls_per_venue_per_day,
        "max_concurrency": configured.max_concurrency,
        "max_second_provider_calls_per_day": (
            configured.max_second_provider_calls_per_day),
    }
    values.update(overrides)
    return BudgetLimits(**values)


class DiscoveryValidationTests(unittest.TestCase):
    def setUp(self):
        self.request = request_from_catalog(
            load_venue_catalog(), "icml", 2026)
        self.provider = FakeProvider([load_response()])

    def test_grounded_response_normalizes_to_strict_contract(self):
        result = normalize_provider_response(
            self.request, self.provider, load_response(), NOW)
        self.assertEqual(result["venue_id"], "icml")
        self.assertEqual(result["year"], 2026)
        self.assertEqual(
            [item["milestone_type"] for item in result["candidate_milestones"]],
            ["conference_start", "conference_end"],
        )
        self.assertEqual(result["claims"][0]["claim_kind"], "conference")
        self.assertTrue(all(
            item["scope"] == "conference"
            for item in result["candidate_milestones"]
        ))
        self.assertRegex(result["evidence_fingerprint"], r"^[a-f0-9]{64}$")
        self.assertNotIn("action", result)
        self.assertNotIn("command", result)
        self.assertNotIn("next_check_at", result)

    def test_venue_year_mismatch_is_rejected_at_every_level(self):
        for mutation in ("result", "claim", "milestone", "date"):
            response = load_response()
            body = deepcopy(dict(response.body))
            if mutation == "result":
                body["year"] = 2025
            elif mutation == "claim":
                body["claims"][0]["venue_id"] = "aistats"
            elif mutation == "milestone":
                body["candidate_milestones"][0]["year"] = 2027
            else:
                body["candidate_milestones"][0]["date"] = "2027-07-13"
            candidate = ProviderResponse(
                body=body,
                grounding_sources=response.grounding_sources,
                search_queries=response.search_queries,
            )
            with self.subTest(mutation=mutation), self.assertRaises(
                    DiscoveryValidationError):
                normalize_provider_response(
                    self.request, self.provider, candidate, NOW)

    def test_unsupported_claim_and_false_source_class_are_rejected(self):
        for mutation in ("unsupported_url", "false_archival"):
            response = load_response()
            body = deepcopy(dict(response.body))
            if mutation == "unsupported_url":
                body["claims"][0]["evidence_urls"] = [
                    "https://unsupported.example/claim"
                ]
            else:
                body["claims"][0]["source_type"] = "archival"
            candidate = ProviderResponse(
                body=body,
                grounding_sources=response.grounding_sources,
                search_queries=response.search_queries,
            )
            with self.subTest(mutation=mutation), self.assertRaises(
                    DiscoveryValidationError) as raised:
                normalize_provider_response(
                    self.request, self.provider, candidate, NOW)
            expected = (
                "unsupported_claim_evidence"
                if mutation == "unsupported_url" else "source_class_mismatch"
            )
            self.assertEqual(raised.exception.category, expected)

    def test_non_unknown_status_requires_a_typed_supporting_claim(self):
        response = load_response()
        body = deepcopy(dict(response.body))
        body["pdf_status"] = "partial"
        candidate = ProviderResponse(
            body=body,
            grounding_sources=response.grounding_sources,
            search_queries=response.search_queries,
        )

        with self.assertRaises(DiscoveryValidationError) as raised:
            normalize_provider_response(
                self.request, self.provider, candidate, NOW)

        self.assertEqual(raised.exception.category, "unsupported_status")

    def test_acceptance_notification_requires_main_track_scope(self):
        response = load_response()
        body = deepcopy(dict(response.body))
        body["candidate_milestones"][0]["milestone_type"] = (
            "acceptance_notification")
        candidate = ProviderResponse(
            body=body,
            grounding_sources=response.grounding_sources,
            search_queries=response.search_queries,
        )

        with self.assertRaises(DiscoveryValidationError) as raised:
            normalize_provider_response(
                self.request, self.provider, candidate, NOW)

        self.assertEqual(
            raised.exception.category, "milestone_scope_mismatch")

    def test_conference_status_is_derived_from_grounded_end_date(self):
        result = normalize_provider_response(
            self.request,
            self.provider,
            load_response(),
            datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result["conference_status"], "ended")

    def test_acceptance_notification_may_precede_conference_year(self):
        response = load_response()
        body = deepcopy(dict(response.body))
        milestone = body["candidate_milestones"][0]
        milestone["milestone_type"] = "acceptance_notification"
        milestone["scope"] = "main_track"
        milestone["date"] = "2025-12-01"
        candidate = ProviderResponse(
            body=body,
            grounding_sources=response.grounding_sources,
            search_queries=response.search_queries,
        )

        result = normalize_provider_response(
            self.request, self.provider, candidate, NOW)

        self.assertEqual(
            result["candidate_milestones"][0]["date"], "2025-12-01")

    def test_continuous_venue_drops_conference_milestones(self):
        request = request_from_catalog(load_venue_catalog(), "jmlr", 2026)
        response = load_response()
        body = deepcopy(dict(response.body))
        body["venue_id"] = "jmlr"
        body["claims"][0]["venue_id"] = "jmlr"
        for milestone in body["candidate_milestones"]:
            milestone["venue_id"] = "jmlr"
        source = GroundingSource(
            uri="https://jmlr.org/papers/",
            title="JMLR papers",
            domain="jmlr.org",
        )
        body["claims"][0]["evidence_urls"] = [source.uri]
        for milestone in body["candidate_milestones"]:
            milestone["evidence_urls"] = [source.uri]
        candidate = ProviderResponse(
            body=body,
            grounding_sources=(source,),
            search_queries=response.search_queries,
        )

        result = normalize_provider_response(
            request, self.provider, candidate, NOW)

        self.assertEqual(result["conference_status"], "unknown")
        self.assertEqual(result["candidate_milestones"], [])


class DiscoveryControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = request_from_catalog(
            load_venue_catalog(), "icml", 2026)
        self.clock = MutableClock(NOW)

    def service(
        self,
        provider,
        *,
        configured_limits=None,
        max_retries=0,
        secondary=None,
    ) -> DiscoveryService:
        return DiscoveryService(
            provider,
            ArtifactStore(self.root / "artifacts"),
            JsonBudgetLedger(self.root / "budget.v1.json"),
            configured_limits or limits(),
            secondary_provider=secondary,
            clock=self.clock,
            max_retries=max_retries,
        )

    def test_repeated_request_uses_cache_without_spending_budget(self):
        provider = FakeProvider([load_response()])
        service = self.service(provider)
        first = service.discover(self.request)
        second = service.discover(self.request)
        self.assertFalse(first.primary.cache_hit)
        self.assertTrue(second.primary.cache_hit)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(first.primary.artifact_path, second.primary.artifact_path)
        attempts = service.budget_ledger.attempts_for_day(NOW.date())
        self.assertEqual(len(attempts), 1)

        artifact = json.loads(
            first.primary.artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(artifact),
            {"artifact_version", "request_fingerprint", "provider_role",
             "result", "grounding"},
        )
        self.assertNotIn("state", artifact)
        self.assertNotIn("job", artifact)

    def test_each_retry_attempt_consumes_budget_and_retains_safe_error(self):
        provider = FakeProvider([
            RetryableProviderError("temporary provider failure"),
            load_response(),
        ])
        service = self.service(
            provider,
            configured_limits=limits(
                max_calls_per_day=3,
                max_calls_per_venue_per_day=3,
            ),
            max_retries=1,
        )
        outcome = service.discover(self.request)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(
            len(service.budget_ledger.attempts_for_day(NOW.date())), 2)
        errors = list((self.root / "artifacts" / "errors").rglob("*.json"))
        self.assertEqual(len(errors), 1)
        retained_error = json.loads(errors[0].read_text(encoding="utf-8"))
        self.assertEqual(retained_error["error_type"], "RetryableProviderError")
        self.assertEqual(retained_error["error_category"], "provider_error")
        self.assertNotIn("message", retained_error)
        self.assertFalse(outcome.primary.cache_hit)

    def test_safe_error_summary_exposes_only_category_and_status(self):
        error = RetryableProviderError(
            "response text that must not be exposed",
            category="api_transient",
            status_code=503,
            diagnostics={"text_length": 42, "text_shape": "object"},
        )
        self.assertEqual(safe_error_summary(error), "api_transient:http_503")
        self.assertNotIn("response", safe_error_summary(error))

    def test_multi_call_provider_reserves_cost_atomically(self):
        blocked = FakeProvider([load_response()])
        blocked.attempt_cost = 2
        blocked_service = self.service(
            blocked,
            configured_limits=limits(
                max_calls_per_day=1,
                max_calls_per_venue_per_day=1,
            ),
        )
        with self.assertRaises(BudgetExceeded):
            blocked_service.discover(self.request)
        self.assertEqual(blocked.calls, 0)
        self.assertEqual(
            blocked_service.budget_ledger.attempts_for_day(NOW.date()), [])

        allowed = FakeProvider([load_response()])
        allowed.attempt_cost = 2
        allowed_service = self.service(
            allowed,
            configured_limits=limits(
                max_calls_per_day=2,
                max_calls_per_venue_per_day=2,
            ),
        )
        allowed_service.discover(self.request)
        self.assertEqual(
            len(allowed_service.budget_ledger.attempts_for_day(NOW.date())),
            2,
        )

    def test_normalization_rejection_is_retained_with_safe_category(self):
        response = load_response()
        body = deepcopy(dict(response.body))
        body["claims"][0]["evidence_urls"] = [
            "https://unsupported.example/claim"
        ]
        provider = FakeProvider([ProviderResponse(
            body=body,
            grounding_sources=response.grounding_sources,
            search_queries=response.search_queries,
        )])
        service = self.service(provider)
        with self.assertRaises(DiscoveryValidationError):
            service.discover(self.request)
        errors = list((self.root / "artifacts" / "errors").rglob("*.json"))
        self.assertEqual(len(errors), 1)
        retained = json.loads(errors[0].read_text(encoding="utf-8"))
        self.assertEqual(
            retained["error_category"], "unsupported_claim_evidence")

    def test_per_venue_limit_blocks_forced_call_and_resets_next_utc_day(self):
        provider = FakeProvider([load_response(), load_response()])
        service = self.service(
            provider,
            configured_limits=limits(max_calls_per_venue_per_day=1),
        )
        service.discover(self.request, force=True)
        with self.assertRaises(BudgetExceeded):
            service.discover(self.request, force=True)
        self.assertEqual(provider.calls, 1)

        self.clock.value = NOW + timedelta(days=1)
        outcome = service.discover(self.request, force=True)
        self.assertFalse(outcome.primary.cache_hit)
        self.assertEqual(provider.calls, 2)

    def test_low_confidence_uses_bounded_second_provider_path(self):
        primary_response = load_response()
        primary_body = deepcopy(dict(primary_response.body))
        primary_body["confidence"] = 0.4
        primary = FakeProvider([ProviderResponse(
            body=primary_body,
            grounding_sources=primary_response.grounding_sources,
            search_queries=primary_response.search_queries,
        )])
        secondary = FakeProvider([load_response()])
        secondary.name = "independent-fake-search"
        service = self.service(
            primary,
            secondary=secondary,
            configured_limits=limits(
                max_calls_per_day=3,
                max_calls_per_venue_per_day=3,
                max_second_provider_calls_per_day=1,
            ),
        )
        outcome = service.discover(self.request)
        self.assertTrue(outcome.escalation_requested)
        self.assertIsNotNone(outcome.secondary)
        self.assertIsNone(outcome.escalation_skipped_reason)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(secondary.calls, 1)
        attempts = service.budget_ledger.attempts_for_day(NOW.date())
        self.assertEqual(
            [attempt["provider_role"] for attempt in attempts],
            ["primary", "secondary"],
        )

    def test_low_confidence_without_second_provider_is_observation_only(self):
        response = load_response()
        body = deepcopy(dict(response.body))
        body["confidence"] = 0.4
        provider = FakeProvider([ProviderResponse(
            body=body,
            grounding_sources=response.grounding_sources,
            search_queries=response.search_queries,
        )])
        outcome = self.service(provider).discover(self.request)
        self.assertTrue(outcome.escalation_requested)
        self.assertIsNone(outcome.secondary)
        self.assertEqual(
            outcome.escalation_skipped_reason,
            "second_provider_not_configured",
        )

    def test_configured_concurrency_bounds_simultaneous_provider_calls(self):
        provider = SlowProvider()
        service = self.service(
            provider,
            configured_limits=limits(
                max_calls_per_day=4,
                max_calls_per_venue_per_day=4,
                max_concurrency=1,
            ),
        )
        with ThreadPoolExecutor(max_workers=4) as executor:
            outcomes = list(executor.map(
                lambda _: service.discover(self.request, force=True),
                range(4),
            ))
        self.assertEqual(len(outcomes), 4)
        self.assertEqual(provider.max_active, 1)


if __name__ == "__main__":
    unittest.main()
