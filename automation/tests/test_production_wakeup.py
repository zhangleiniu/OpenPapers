import ast
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.configuration import load_policy_config, load_venue_catalog
from automation.control_state import ControlStateRepository
from automation.discovery import BudgetExceeded, GroundingSource, ProviderError, ProviderResponse
from automation.domain import Writer
from automation.production_discovery import AutomaticDiscoveryRefused
from automation.production_wakeup import (
    ProductionControlPlaneConfig,
    ProductionControlPlaneConfigError,
    run_production_control_wakeup,
)
from automation.tests.test_local_control_plane import due_state, seed_local_state
from automation.verification import FetchRequest, FetchResponse


MODULE = Path(__file__).resolve().parents[1] / "production_wakeup.py"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
PDF_URL = "https://proceedings.mlr.press/openpapers-fixture/icml2026/paper.pdf"


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


def pdf_response(url, *, status=200, body=None):
    return FetchResponse(
        requested_url=url,
        status_code=status,
        headers={},
        body=body if body is not None else b"%PDF-1.4\n" + b"0" * 2000,
        fetched_at="2026-07-15T12:00:00Z",
    )


def pdf_ready_discovery_response(
    *, venue_id="icml", year=2026, pdf_url=PDF_URL
):
    """A raw provider response that normalizes to a single authoritative PDF claim.

    Every other status stays ``unknown`` so no other supporting claim is
    required; ``reduce_verification`` promotes ``pdf_ready`` directly from
    ``unknown`` once the sampled PDF evidence verifies, matching the real
    (non-fixture) lifecycle-reduction rule that only ``pdf_status`` gates the
    ``queue_existing_scraper`` action.
    """
    body = {
        "venue_id": venue_id,
        "year": year,
        "conference_status": "unknown",
        "paper_list_status": "unknown",
        "metadata_status": "unknown",
        "pdf_status": "ready",
        "proceedings_status": "unknown",
        "claims": [{
            "venue_id": venue_id,
            "year": year,
            "claim_kind": "pdf",
            "statement": "Sanitized fixture PDF claim for the P2.8 composition.",
            "evidence_urls": [pdf_url],
            "source_type": "archival",
            "published_at": None,
        }],
        "candidate_milestones": [],
        "confidence": 0.9,
        "uncertainties": [],
    }
    return ProviderResponse(
        body=body,
        grounding_sources=(
            GroundingSource(uri=pdf_url, title="Fixture PDF", domain="proceedings.mlr.press"),
        ),
        search_queries=(f"{venue_id} {year} pdf",),
    )


def policy_with(**overrides):
    policy = deepcopy(load_policy_config())
    for section, values in overrides.items():
        policy.setdefault(section, {}).update(values)
    return policy


class ProductionWakeupTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.catalog = load_venue_catalog()
        self.addCleanup(self._temp_dir.cleanup)

    def config(self):
        return ProductionControlPlaneConfig(
            control_state_path=self.root / "state.sqlite3",
            automation_root=self.root / "automation-effects",
            gemini_project="test-project",
            gemini_location="global",
            gemini_model="gemini-test",
        )

    def open_repository(self, config):
        return ControlStateRepository(
            config.control_state_path, writer=Writer.LOCAL_CONTROL_PLANE,
            clock=lambda: NOW,
        )

    # -- configuration ---------------------------------------------------

    def test_config_rejects_relative_and_overlapping_roots(self):
        with self.assertRaises(ProductionControlPlaneConfigError):
            ProductionControlPlaneConfig(
                control_state_path=Path("state.sqlite3"),
                automation_root=self.root / "automation-effects",
                gemini_project="p", gemini_location="l", gemini_model="m",
            )
        with self.assertRaises(ProductionControlPlaneConfigError):
            ProductionControlPlaneConfig(
                control_state_path=self.root / "shared" / "state.sqlite3",
                automation_root=self.root / "shared",
                gemini_project="p", gemini_location="l", gemini_model="m",
            )

    def test_config_requires_gemini_identity(self):
        with self.assertRaises(ProductionControlPlaneConfigError):
            ProductionControlPlaneConfig(
                control_state_path=self.root / "state.sqlite3",
                automation_root=self.root / "automation-effects",
                gemini_project="", gemini_location="l", gemini_model="m",
            )

    def test_config_derives_distinct_bounded_private_subpaths(self):
        config = self.config()
        self.assertEqual(
            config.discovery_artifact_root, config.automation_root / "discovery")
        self.assertEqual(
            config.verification_snapshot_root,
            config.automation_root / "verification-snapshots")
        paths = {
            config.discovery_artifact_root,
            config.discovery_budget_ledger_path,
            config.discovery_health_ledger_path,
            config.verification_snapshot_root,
            config.verification_health_ledger_path,
        }
        self.assertEqual(len(paths), 5)

    # -- successful round trip: real discovery + real verification -------

    def test_pdf_ready_discovery_and_verification_retain_exactly_one_job(self):
        config = self.config()
        seed_local_state(config.control_state_path, state=due_state())
        provider = FakeProvider([pdf_ready_discovery_response()])
        fetcher = MappingFetcher({PDF_URL: pdf_response(PDF_URL)})

        outcome = run_production_control_wakeup(
            config,
            scheduled_for=NOW,
            clock=lambda: NOW,
            catalog=self.catalog,
            _discovery_provider_factory=FactoryCounter(provider),
            _verification_fetcher=fetcher,
        )

        self.assertFalse(outcome.replayed)
        self.assertEqual(len(outcome.selections), 1)
        selected = outcome.selections[0]
        self.assertEqual(len(selected.execution_retentions), 1)
        retention = selected.execution_retentions[0]
        self.assertTrue(retention.applied)
        self.assertEqual(retention.record.state, "pending")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(fetcher.requests), 1)

        with self.open_repository(config) as repository:
            self.assertEqual(len(repository.list_execution_jobs()), 1)

        # Exact replay must call neither the provider nor the fetcher again
        # and must not create a duplicate job.
        replay = run_production_control_wakeup(
            config,
            scheduled_for=NOW,
            clock=lambda: NOW + timedelta(minutes=1),
            catalog=self.catalog,
            _discovery_provider_factory=FactoryCounter(provider),
            _verification_fetcher=fetcher,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.selections, ())
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(fetcher.requests), 1)
        with self.open_repository(config) as repository:
            self.assertEqual(len(repository.list_execution_jobs()), 1)

    # -- unsupported/invalid evidence persists no action ------------------

    def test_invalid_pdf_signature_persists_no_action(self):
        config = self.config()
        seed_local_state(config.control_state_path, state=due_state())
        provider = FakeProvider([pdf_ready_discovery_response()])
        fetcher = MappingFetcher({PDF_URL: pdf_response(PDF_URL, body=b"not a pdf")})

        outcome = run_production_control_wakeup(
            config,
            scheduled_for=NOW,
            clock=lambda: NOW,
            catalog=self.catalog,
            _discovery_provider_factory=FactoryCounter(provider),
            _verification_fetcher=fetcher,
        )

        self.assertFalse(outcome.replayed)
        self.assertEqual(len(outcome.selections), 1)
        self.assertEqual(outcome.selections[0].execution_retentions, ())
        with self.open_repository(config) as repository:
            self.assertEqual(repository.list_execution_jobs(), ())

    # -- circuit-open discovery guard refuses before any effect call -----

    def test_open_circuit_refuses_before_any_call_and_leaves_wakeup_active(self):
        config = self.config()
        seed_local_state(config.control_state_path, state=due_state())
        deadline = NOW + timedelta(hours=1)
        ledger_payload = {
            "version": 1,
            "venues": {},
            "circuit": {
                "systemic_fingerprint": "fixture-systemic-fingerprint",
                "venues": ["icml", "aistats", "ijcai"],
                "opened_at": "2026-07-15T10:00:00Z",
                "deadline_at": deadline.isoformat().replace("+00:00", "Z"),
            },
            "systemic_events": [],
        }
        config.discovery_health_ledger_path.parent.mkdir(parents=True, exist_ok=True)
        config.discovery_health_ledger_path.write_text(
            json.dumps(ledger_payload), encoding="utf-8"
        )
        provider = FakeProvider([pdf_ready_discovery_response()])
        fetcher = MappingFetcher({})

        with self.assertRaises(AutomaticDiscoveryRefused):
            run_production_control_wakeup(
                config,
                scheduled_for=NOW,
                clock=lambda: NOW,
                catalog=self.catalog,
                _discovery_provider_factory=FactoryCounter(provider),
                _verification_fetcher=fetcher,
            )

        self.assertEqual(provider.calls, 0)
        self.assertEqual(fetcher.requests, [])
        with self.open_repository(config) as repository:
            self.assertEqual(repository.list_scheduler_wakeups()[0].status, "active")
            self.assertEqual(repository.list_execution_jobs(), ())

    # -- budget exhaustion refuses before any provider call ----------------

    def test_budget_exhaustion_refuses_before_provider_call(self):
        config = self.config()
        seed_local_state(config.control_state_path, state=due_state())
        policy = policy_with(discovery_budget={"max_calls_per_day": 0})
        provider = FakeProvider([pdf_ready_discovery_response()])
        fetcher = MappingFetcher({})

        with self.assertRaises(BudgetExceeded):
            run_production_control_wakeup(
                config,
                scheduled_for=NOW,
                clock=lambda: NOW,
                catalog=self.catalog,
                policy=policy,
                _discovery_provider_factory=FactoryCounter(provider),
                _verification_fetcher=fetcher,
            )

        self.assertEqual(provider.calls, 0)
        self.assertEqual(fetcher.requests, [])
        with self.open_repository(config) as repository:
            self.assertEqual(repository.list_scheduler_wakeups()[0].status, "active")
            self.assertEqual(repository.list_execution_jobs(), ())

    # -- partial commit: an earlier selection's retention survives -------

    def test_partial_commit_retains_earlier_selection_when_a_later_one_refuses(self):
        config = self.config()
        icml_state = due_state()
        icml_state["next_check_at"] = "2026-07-13T10:00:00Z"
        aistats_state = due_state()
        aistats_state["venue_id"] = "aistats"
        aistats_state["next_check_at"] = "2026-07-13T11:00:00Z"
        seed_local_state(config.control_state_path, state=icml_state)
        with self.open_repository(config) as repository:
            lease = repository.acquire_lease("p2-8-fixture-seed")
            try:
                repository.store_conference_state(
                    aistats_state, expected_revision=0, lease=lease,
                    stored_at=NOW - timedelta(days=1),
                )
            finally:
                repository.release_lease(lease)

        provider = FakeProvider([
            pdf_ready_discovery_response(venue_id="icml"),
            ProviderError("transport failure", category="search_api_failure"),
        ])
        fetcher = MappingFetcher({PDF_URL: pdf_response(PDF_URL)})

        with self.assertRaises(ProviderError):
            run_production_control_wakeup(
                config,
                scheduled_for=NOW,
                clock=lambda: NOW,
                catalog=self.catalog,
                _discovery_provider_factory=FactoryCounter(provider),
                _verification_fetcher=fetcher,
            )

        self.assertEqual(provider.calls, 2)
        with self.open_repository(config) as repository:
            self.assertEqual(repository.list_scheduler_wakeups()[0].status, "active")
            jobs = repository.list_execution_jobs()
            self.assertEqual(len(jobs), 1)
            icml_current = repository.get_conference_state("icml", 2026)
            self.assertNotEqual(
                icml_current.state["next_check_at"], icml_state["next_check_at"]
            )
            aistats_current = repository.get_conference_state("aistats", 2026)
            self.assertEqual(
                aistats_current.state["next_check_at"],
                aistats_state["next_check_at"],
            )

    # -- static scope: no dispatch/execution/local-service import --------

    def test_module_has_no_dispatch_execution_or_installed_service_import(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        source = MODULE.read_text(encoding="utf-8")
        for forbidden in (
            "automation.execution_dispatch",
            "automation.execution_pipeline",
            "automation.mac_worker",
            "automation.staging_executor",
            "automation.local_service",
            "prefect",
            "requests",
            "subprocess",
        ):
            self.assertNotIn(forbidden, imported)
            self.assertNotIn(forbidden, source)
        self.assertNotIn("getenv", source)
        self.assertNotIn("os.environ", source)


if __name__ == "__main__":
    unittest.main()
