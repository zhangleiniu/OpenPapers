import ast
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.configuration import load_policy_config, load_venue_catalog
from automation.contracts import ContractName, validate_contract
from automation.discovery import (
    BudgetExceeded,
    DiscoveryValidationError,
    GroundingSource,
    ProviderError,
    ProviderResponse,
    normalize_provider_response,
    request_from_catalog,
)
from automation.local_control_plane import VerificationBundle, run_local_control_wakeup
from automation.production_discovery import (
    AutomaticDiscoveryConfig,
    AutomaticDiscoveryGuardPolicy,
    AutomaticDiscoveryLedgerError,
    AutomaticDiscoveryRefused,
    ProductionDiscoveryEffect,
)
from automation.tests.test_local_control_plane import due_state, seed_local_state
from automation.verification import build_verification_request, build_verification_result


FIXTURE = (
    Path(__file__).with_name("fixtures")
    / "phase1"
    / "gemini-grounded-response.v1.json"
)
MODULE = Path(__file__).resolve().parents[1] / "production_discovery.py"
NOW = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def load_response() -> ProviderResponse:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ProviderResponse(
        body=payload["body"],
        grounding_sources=tuple(
            GroundingSource(**source) for source in payload["grounding_sources"]
        ),
        search_queries=tuple(payload["search_queries"]),
    )


class FakeProvider:
    name = "fake-search"
    model = "fake-model"
    prompt_version = "v1"
    attempt_cost = 1

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def discover(self, request):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FactoryCounter:
    """Return one fixed provider and count how often it is (re)constructed."""

    def __init__(self, provider):
        self.provider = provider
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.provider


def policy_with(
    *,
    venue_failure_threshold=None,
    same_failure_cooldown_hours=None,
    systemic_circuit_hours=None,
    discovery_budget=None,
):
    policy = load_policy_config()
    if venue_failure_threshold is not None:
        policy["systemic_failure"]["venue_failure_threshold"] = venue_failure_threshold
    if same_failure_cooldown_hours is not None:
        policy["automatic_discovery"]["same_failure_cooldown_hours"] = (
            same_failure_cooldown_hours)
    if systemic_circuit_hours is not None:
        policy["automatic_discovery"]["systemic_circuit_hours"] = (
            systemic_circuit_hours)
    if discovery_budget:
        policy["discovery_budget"].update(discovery_budget)
    return policy


class ProductionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.catalog = load_venue_catalog()
        self.addCleanup(self._temp_dir.cleanup)

    def tearDown(self):
        pass

    def config(self, policy=None) -> AutomaticDiscoveryConfig:
        return AutomaticDiscoveryConfig.from_policy(
            policy,
            artifact_root=self.root / "artifacts",
            budget_ledger_path=self.root / "budget.json",
            health_ledger_path=self.root / "health.json",
            project="test-project",
            location="global",
            model="gemini-test",
        )

    def effect(self, provider, *, policy=None, clock=None):
        factory = FactoryCounter(provider)
        built = ProductionDiscoveryEffect(
            self.config(policy),
            clock=clock or (lambda: NOW),
            _provider_factory=factory,
        )
        return built, factory

    def request(self, venue_id="icml", year=2026):
        return request_from_catalog(self.catalog, venue_id, year)

    # -- successful round trip ------------------------------------------------

    def test_successful_discovery_returns_primary_result_and_clears_health(self):
        provider = FakeProvider([load_response()])
        built, factory = self.effect(provider)
        result = built.discover(self.request())
        validate_contract(ContractName.DISCOVERY_RESULT, result)
        self.assertEqual(result["venue_id"], "icml")
        self.assertEqual(factory.calls, 1)
        self.assertEqual(provider.calls, 1)
        state = built._health.venue_state("icml")
        self.assertEqual(state["state"], "eligible")
        self.assertIsNone(state["deadline_at"])
        attempts = built._budget_ledger.attempts_for_day(NOW.date())
        self.assertEqual(len(attempts), 1)

    # -- typed provider failure opens a per-venue cooldown --------------------

    def test_provider_failure_opens_cooldown_before_next_construction(self):
        provider = FakeProvider([
            ProviderError(
                "transport failure", category="search_api_failure",
                status_code=503),
        ])
        built, factory = self.effect(provider)
        with self.assertRaises(ProviderError):
            built.discover(self.request())
        state = built._health.venue_state("icml")
        self.assertEqual(state["state"], "cooldown")
        self.assertIsNotNone(state["systemic_fingerprint"])

        with self.assertRaises(AutomaticDiscoveryRefused) as ctx:
            built.discover(self.request())
        self.assertEqual(ctx.exception.reason, "same_venue_cooldown")
        # The refusal happens before another provider is constructed.
        self.assertEqual(factory.calls, 1)

    def test_cooldown_expires_and_allows_a_new_attempt(self):
        clock = MutableClock(NOW)
        provider = FakeProvider([
            ProviderError("boom", category="search_api_failure"),
            load_response(),
        ])
        built, factory = self.effect(
            provider,
            policy=policy_with(same_failure_cooldown_hours=1),
            clock=clock,
        )
        with self.assertRaises(ProviderError):
            built.discover(self.request())
        clock.value = NOW + timedelta(hours=1, seconds=1)
        result = built.discover(self.request())
        self.assertEqual(result["venue_id"], "icml")
        self.assertEqual(factory.calls, 2)
        state = built._health.venue_state("icml")
        self.assertEqual(state["state"], "eligible")

    # -- budget exhaustion is a guard decision, not a health failure ----------

    def test_budget_exceeded_is_a_guard_skip_not_a_cooldown(self):
        provider = FakeProvider([load_response()])
        built, factory = self.effect(
            provider,
            policy=policy_with(discovery_budget={
                "max_calls_per_day": 5,
                "max_calls_per_venue_per_day": 0,
                "max_concurrency": 2,
                "max_second_provider_calls_per_day": 1,
            }),
        )
        with self.assertRaises(BudgetExceeded):
            built.discover(self.request())
        self.assertEqual(provider.calls, 0)
        state = built._health.venue_state("icml")
        self.assertEqual(state["state"], "eligible")
        self.assertIsNone(state["deadline_at"])
        self.assertEqual(factory.calls, 1)

    # -- venue-specific validation failures never open the systemic circuit --

    def test_venue_specific_validation_failures_do_not_open_circuit(self):
        venues = ["icml", "aistats", "ijcai"]
        built = None
        for venue_id in venues:
            provider = FakeProvider([
                DiscoveryValidationError(
                    "mismatched evidence domain",
                    category="source_class_mismatch",
                ),
            ])
            built, _ = self.effect(
                provider, policy=policy_with(venue_failure_threshold=3))
            with self.assertRaises(DiscoveryValidationError):
                built.discover(self.request(venue_id))
        self.assertIsNone(built._health.circuit_state())
        for venue_id in venues:
            state = built._health.venue_state(venue_id)
            self.assertEqual(state["state"], "cooldown")
            self.assertIsNone(state["systemic_fingerprint"])

    # -- distinct venues with the same systemic fingerprint open one circuit --

    def test_three_distinct_venues_open_one_systemic_circuit(self):
        venues = ["icml", "aistats", "ijcai"]
        built = None
        for venue_id in venues:
            provider = FakeProvider([
                ProviderError(
                    "transport failure", category="search_api_failure",
                    status_code=503),
            ])
            built, _ = self.effect(
                provider, policy=policy_with(venue_failure_threshold=3))
            with self.assertRaises(ProviderError):
                built.discover(self.request(venue_id))
        circuit = built._health.circuit_state()
        self.assertIsNotNone(circuit)
        self.assertEqual(circuit["venues"], sorted(venues))

        # A fourth, unrelated venue is refused before provider construction.
        other_provider = FakeProvider([load_response()])
        blocked, factory = self.effect(
            other_provider, policy=policy_with(venue_failure_threshold=3))
        with self.assertRaises(AutomaticDiscoveryRefused) as ctx:
            blocked.discover(self.request("neurips", year=2026))
        self.assertEqual(ctx.exception.reason, "systemic_circuit_open")
        self.assertEqual(factory.calls, 0)

    def test_circuit_expires_after_its_configured_duration(self):
        clock = MutableClock(NOW)
        venues = ["icml", "aistats", "ijcai"]
        policy = policy_with(
            venue_failure_threshold=3,
            same_failure_cooldown_hours=1,
            systemic_circuit_hours=2,
        )
        for venue_id in venues:
            provider = FakeProvider([
                ProviderError("boom", category="search_api_failure"),
            ])
            built, _ = self.effect(provider, policy=policy, clock=clock)
            with self.assertRaises(ProviderError):
                built.discover(self.request(venue_id))
        self.assertIsNotNone(built._health.circuit_state())

        # The venue-specific cooldown has expired, but the circuit has not.
        clock.value = NOW + timedelta(hours=1, seconds=1)
        blocked_provider = FakeProvider([load_response()])
        blocked, blocked_factory = self.effect(
            blocked_provider, policy=policy, clock=clock)
        with self.assertRaises(AutomaticDiscoveryRefused) as ctx:
            blocked.discover(self.request())
        self.assertEqual(ctx.exception.reason, "systemic_circuit_open")
        self.assertEqual(blocked_factory.calls, 0)

        # Once the circuit itself expires, the same venue succeeds again.
        clock.value = NOW + timedelta(hours=2, seconds=1)
        provider = FakeProvider([load_response()])
        reopened, factory = self.effect(provider, policy=policy, clock=clock)
        result = reopened.discover(self.request())
        self.assertEqual(result["venue_id"], "icml")
        self.assertEqual(factory.calls, 1)

    # -- durable state across a simulated process restart --------------------

    def test_cooldown_survives_a_new_adapter_process(self):
        provider = FakeProvider([
            ProviderError("boom", category="search_api_failure"),
        ])
        first, _ = self.effect(provider)
        with self.assertRaises(ProviderError):
            first.discover(self.request())

        reopened_provider = FakeProvider([load_response()])
        reopened, factory = self.effect(reopened_provider)
        with self.assertRaises(AutomaticDiscoveryRefused) as ctx:
            reopened.discover(self.request())
        self.assertEqual(ctx.exception.reason, "same_venue_cooldown")
        self.assertEqual(factory.calls, 0)

    def test_crash_after_claim_blocks_without_inventing_a_failure(self):
        provider = FakeProvider([load_response()])
        built, factory = self.effect(provider)
        # Simulate a process death after the durable claim but before any
        # provider call or finalize.
        built._health.guard_and_claim(
            "icml", at=NOW, policy=self.config().guard_policy)

        reopened_provider = FakeProvider([load_response()])
        reopened, reopened_factory = self.effect(reopened_provider)
        with self.assertRaises(AutomaticDiscoveryRefused) as ctx:
            reopened.discover(self.request())
        self.assertEqual(ctx.exception.reason, "same_venue_in_flight")
        self.assertEqual(reopened_factory.calls, 0)
        # An unresolved claim never counts toward the systemic circuit.
        self.assertIsNone(built._health.circuit_state())

    # -- ledger safety ----------------------------------------------------

    def test_corrupt_health_ledger_fails_closed_before_construction(self):
        health_path = self.root / "health.json"
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text(
            json.dumps({"version": 99, "venues": {}, "circuit": None,
                        "systemic_events": []}),
            encoding="utf-8",
        )
        provider = FakeProvider([load_response()])
        built, factory = self.effect(provider)
        with self.assertRaises(AutomaticDiscoveryLedgerError):
            built.discover(self.request())
        self.assertEqual(factory.calls, 0)

    def test_concurrent_writers_do_not_lose_venue_health_updates(self):
        built, _ = self.effect(FakeProvider([load_response()]))
        errors = []

        def record(venue_id):
            try:
                built._health.finalize_failure(
                    venue_id,
                    occurrence_fingerprint="occurrence",
                    systemic_fingerprint=None,
                    category="source_class_mismatch",
                    at=NOW,
                    policy=self.config().guard_policy,
                )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        venue_ids = [f"venue-{i}" for i in range(8)]
        threads = [threading.Thread(target=record, args=(v,)) for v in venue_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        for venue_id in venue_ids:
            state = built._health.venue_state(venue_id)
            self.assertEqual(state["state"], "cooldown")

    # -- configuration -----------------------------------------------------

    def test_guard_policy_requires_the_automatic_discovery_block(self):
        policy = load_policy_config()
        del policy["automatic_discovery"]
        with self.assertRaises(ValueError):
            AutomaticDiscoveryGuardPolicy.from_policy(policy)

    def test_config_requires_explicit_private_paths(self):
        with self.assertRaises(TypeError):
            AutomaticDiscoveryConfig.from_policy(
                artifact_root=self.root / "artifacts",
                budget_ledger_path=self.root / "budget.json",
                project="test-project",
                location="global",
                model="gemini-test",
            )

    def test_low_confidence_escalation_returns_only_primary_result(self):
        response = load_response()
        body = deepcopy(dict(response.body))
        body["confidence"] = 0.4
        provider = FakeProvider([ProviderResponse(
            body=body,
            grounding_sources=response.grounding_sources,
            search_queries=response.search_queries,
        )])
        built, factory = self.effect(provider)
        result = built.discover(self.request())
        self.assertEqual(result["confidence"], 0.4)
        # Only one provider construction: no secondary provider is wired.
        self.assertEqual(factory.calls, 1)
        self.assertEqual(provider.calls, 1)

    # -- local-control composition round trip --------------------------------

    def test_adapter_round_trips_through_local_control_wakeup(self):
        html_url = "https://icml.cc/openpapers-fixture/production-discovery/index.html"
        body = {
            "venue_id": "icml",
            "year": 2026,
            "conference_status": "unknown",
            "paper_list_status": "released",
            "metadata_status": "unknown",
            "pdf_status": "unknown",
            "proceedings_status": "unknown",
            "claims": [{
                "venue_id": "icml",
                "year": 2026,
                "claim_kind": "paper_list",
                "statement": (
                    "The ICML 2026 accepted paper list is publicly available."
                ),
                "evidence_urls": [html_url],
                "source_type": "official",
                "published_at": None,
            }],
            "candidate_milestones": [],
            "confidence": 0.9,
            "uncertainties": [],
        }
        response = ProviderResponse(
            body=body,
            grounding_sources=(
                GroundingSource(uri=html_url, title="ICML 2026", domain="icml.cc"),
            ),
            search_queries=("icml 2026 accepted papers",),
        )
        # Precompute the exact discovery result the adapter will produce.
        # ``normalize_provider_response`` is the same pure function the
        # adapter calls internally (through ``DiscoveryService``), so, given
        # the same request/provider identity/response/clock, it reproduces a
        # byte-identical discovery without any ledger side effect. That lets
        # the verification bundle below cite the real discovery/claim IDs
        # before the guarded adapter actually runs inside the wakeup.
        identity_only_provider = FakeProvider([])
        discovery = normalize_provider_response(
            self.request(), identity_only_provider, response, NOW)
        claim_id = discovery["claims"][0]["claim_id"]
        request = build_verification_request(
            discovery,
            requested_at="2026-07-14T14:50:00Z",
            claim_ids=[claim_id],
            candidate_milestone_ids=[],
        )
        observation = {
            "source_id": "source:icml:paper-list",
            "url": html_url,
            "redirect_target_url": None,
            "source_trust": "official",
            "policy_decision": "allowed",
            "policy_domain": "icml.cc",
            "permission": "metadata_fetch",
            "fetch_status": "fetched",
            "http_status": 200,
            "snapshot_id": "snapshot:icml:paper-list",
            "observed_at": "2026-07-14T14:55:00Z",
            "reason_code": "source_observed",
        }
        evidence_ids = [observation["source_id"], observation["snapshot_id"]]
        finding = {
            "finding_id": "finding:icml:2026:paper-list",
            "target_kind": "claim",
            "target_id": claim_id,
            "verification_kind": "paper_list",
            "status": "verified",
            "source_ids": [observation["source_id"]],
            "evidence_ids": evidence_ids,
            "reason_code": "supported",
            "metrics": {"paper_count": 3},
        }
        result = build_verification_result(
            request,
            discovery,
            overall_status="partially_verified",
            verified_at="2026-07-14T14:55:00Z",
            source_observations=[observation],
            findings=[finding],
            verified_facets={
                "conference_status": None,
                "paper_list_status": {
                    "value": "released", "evidence_ids": evidence_ids},
                "metadata_status": None,
                "pdf_status": None,
                "proceedings_status": None,
            },
            verified_milestones=[],
            uncertainties=("Fixture requires deterministic review.",),
        )
        bundle = VerificationBundle(request=request, result=result)

        class FakeVerification:
            def __init__(self, bundles):
                self.bundles = bundles

            def verify(self, discovery, *, observed_at):
                return self.bundles

        # A fresh production adapter drives the actual composed wakeup: the
        # discovery above only proved the standalone contract; this proves
        # the same adapter type satisfies local_control_plane's narrow
        # DiscoveryEffect protocol end to end.
        wakeup_provider = FakeProvider([response])
        wakeup_effect, wakeup_factory = self.effect(wakeup_provider, clock=lambda: NOW)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            seed_local_state(path, state=due_state())
            outcome = run_local_control_wakeup(
                path,
                scheduled_for=NOW,
                clock=lambda: NOW,
                discovery_effect=wakeup_effect,
                verification_effect=FakeVerification((bundle,)),
                catalog=self.catalog,
                policy=load_policy_config(),
            )
        self.assertFalse(outcome.replayed)
        self.assertEqual(len(outcome.selections), 1)
        self.assertEqual(wakeup_factory.calls, 1)
        self.assertEqual(
            outcome.selections[0].verification_ids,
            (result["verification_id"],),
        )

    # -- static scope -------------------------------------------------------

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
            "automation.run_discovery",
            "prefect",
            "requests",
            "subprocess",
        ):
            self.assertNotIn(forbidden, imported)
            self.assertNotIn(forbidden, source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)


if __name__ == "__main__":
    unittest.main()
