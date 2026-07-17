import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from automation.agent_production import (
    AgentProductionConfigurationError,
    AgentProductionEffect,
    AgentProductionSecrets,
    _cohort_year_applies,
    build_live_agent_production_effect,
    load_agent_production_configuration,
    load_agent_targets,
)
from automation.resend_notifications import recipient_fingerprints
from automation.codex_agent import CodexProcessResult
from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.event_dates import EventDateEstimate, EventDateTarget
from automation.local_service.service import LocalEffectStatus
from automation.notifications import FailureCategory, TransportFailure, TransportReceipt


NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
TRACKED_TARGETS = (
    Path(__file__).resolve().parents[1] / "config" / "agent_targets.v1.json"
)


def git(root, *args):
    return subprocess.run(
        ("git", *args), cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


class Provider:
    name = model = prompt_version = "fake"

    def __init__(self):
        self.calls = []

    def estimate(self, request):
        self.calls.append(request)
        return EventDateEstimate(date(2026, 7, 15), "fixture")


class Invoker:
    def __init__(self):
        self.calls = []

    def invoke(self, invocation):
        self.calls.append(invocation)
        (invocation.cwd / "agent-change.txt").write_text("changed\n", encoding="utf-8")
        return CodexProcessResult(0, json.dumps({
            "disposition": "not_ready",
            "explanation": "Proceedings are not available.",
            "suggested_retry_at": None,
            "failure_category": None,
        }), "")


class Transport:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def send(self, intent, *, idempotency_key):
        self.calls.append((intent, idempotency_key))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class TransportFactory:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.transports = []

    def __call__(self):
        transport = Transport(self.outcomes.pop(0))
        self.transports.append(transport)
        return transport


class CohortYearApplicabilityTests(unittest.TestCase):
    def test_default_annual_lifecycle_applies_every_year(self):
        for year in (2025, 2026, 2027, 2100):
            self.assertTrue(_cohort_year_applies({"kind": "annual"}, year))

    def test_biennial_lifecycle_applies_only_on_the_anchored_parity(self):
        iccv = {"kind": "annual", "interval_years": 2, "cycle_anchor_year": 2025}
        eccv = {"kind": "annual", "interval_years": 2, "cycle_anchor_year": 2024}

        self.assertFalse(_cohort_year_applies(iccv, 2026))
        self.assertTrue(_cohort_year_applies(iccv, 2025))
        self.assertTrue(_cohort_year_applies(iccv, 2027))
        self.assertTrue(_cohort_year_applies(eccv, 2026))
        self.assertFalse(_cohort_year_applies(eccv, 2025))
        self.assertFalse(_cohort_year_applies(eccv, 2027))


class AgentProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "fixture")
        self.targets = self.root / "targets.json"
        self.targets.write_text(json.dumps({
            "schema_version": 1,
            "targets": [{"venue_id": "icml", "year": 2026}],
        }, indent=2) + "\n", encoding="utf-8")
        self.payload = {
            "schema_version": 2,
            "targets_sha256": hashlib.sha256(self.targets.read_bytes()).hexdigest(),
            "gemini_project_id": "project-id",
            "gemini_location": "global",
            "gemini_model": "gemini-2.5-flash",
            "monthly_date_lookup_limit": 3,
            "codex_binary": "/usr/bin/false",
            "codex_timeout_seconds": 60,
            "codex_max_output_bytes": 64000,
            "codex_max_changed_files": 100,
            "default_not_ready_delay_hours": 12,
            "minimum_retry_delay_hours": 1,
            "max_suggested_retry_delay_days": 30,
            "failure_backoff_hours": [2, 6, 24, 72],
            "max_consecutive_failures": 5,
            "monthly_run_limit": 120,
            "systemic_failure_threshold": 3,
            "systemic_failure_window_hours": 6,
            "systemic_circuit_delay_hours": 6,
            "minimum_free_bytes": 10_000_000_000,
            "retention_max_retained": 10,
            "retention_max_age_days": 30,
            "retention_max_removals_per_run": 2,
            "resend_recipient_sha256": "a" * 64,
        }
        self.configuration = load_agent_production_configuration(
            self.payload, targets_path=self.targets
        )
        self.state = self.root / "state.sqlite3"
        self.execution = self.root / "execution"
        self.execution.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_tracked_targets_and_strict_configuration_are_bounded(self):
        tracked = load_agent_targets(today=date(2026, 7, 16))
        tracked_payload = json.loads(TRACKED_TARGETS.read_text())
        cohort_venue_ids = set(tracked_payload["cohort"]["venue_ids"])
        extra_targets = {
            (item["venue_id"], item["year"])
            for item in tracked_payload["extra_targets"]
        }
        # ICCV is biennial (anchored on odd years) and does not occur in 2026;
        # every other cohort venue does. NAACL has no formula and is not in
        # the cohort at all — it only appears via the manually confirmed
        # extra_targets entry (naacl, 2027), regardless of "today".
        self.assertEqual(
            {(item.venue_id, item.year) for item in tracked},
            {(venue_id, 2026) for venue_id in cohort_venue_ids - {"iccv"}}
            | extra_targets,
        )
        self.assertEqual(len(tracked), 13)
        self.assertNotIn("jmlr", {item.venue_id for item in tracked})
        self.assertNotIn("iccv", {item.venue_id for item in tracked})
        self.assertIn(("naacl", 2027), {(item.venue_id, item.year) for item in tracked})
        invalid = dict(self.payload, resend_api_key="secret")
        with self.assertRaises(AgentProductionConfigurationError):
            load_agent_production_configuration(invalid, targets_path=self.targets)
        duplicate = {
            "schema_version": 1,
            "targets": [
                {"venue_id": "icml", "year": 2026},
                {"venue_id": "icml", "year": 2026},
            ],
        }
        self.targets.write_text(
            json.dumps(duplicate, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            load_agent_targets(self.targets)
        secrets = AgentProductionSecrets(
            "placeholder-key", "OpenPapers <from@example.test>",
            "to@example.test",
        )
        self.assertNotIn("placeholder-key", repr(secrets))
        with self.assertRaisesRegex(ValueError, "recipient approval"):
            build_live_agent_production_effect(
                repository_root=self.repo,
                configuration=self.configuration,
                secrets=secrets,
                credentials=None,
            )

    def test_schema_3_combines_cohort_and_manually_confirmed_extra_targets(self):
        combined = self.root / "combined.json"
        payload = {
            "schema_version": 3,
            "cohort": {
                "venue_ids": ["icml"],
                "initial_year": 2026,
                "rollover_month": 10,
                "years_ahead_after_rollover": 1,
            },
            "extra_targets": [{"venue_id": "naacl", "year": 2027}],
        }
        combined.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        targets = load_agent_targets(combined, today=date(2026, 7, 16))

        self.assertEqual(
            set(targets),
            {EventDateTarget("icml", 2026), EventDateTarget("naacl", 2027)},
        )
        # extra_targets need not be pre-sorted relative to the cohort (naacl
        # sorts before icml alphabetically, yet is listed second in the
        # file) — the loader is responsible for the combined canonical order.
        self.assertEqual(tuple(sorted(targets)), targets)

        # A duplicate between the cohort expansion and extra_targets is
        # still rejected, exactly like a duplicate within a single list.
        duplicate = dict(
            payload, extra_targets=[{"venue_id": "icml", "year": 2026}],
        )
        combined.write_text(json.dumps(duplicate, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unique"):
            load_agent_targets(combined, today=date(2026, 7, 16))

    def test_annual_cohort_rolls_forward_without_expanding_venue_scope(self):
        before = load_agent_targets(TRACKED_TARGETS, today=date(2026, 9, 30))
        rollover = load_agent_targets(TRACKED_TARGETS, today=date(2026, 10, 1))
        next_year = load_agent_targets(TRACKED_TARGETS, today=date(2027, 1, 1))

        cohort_venue_ids = set(
            json.loads(TRACKED_TARGETS.read_text())["cohort"]["venue_ids"]
        )
        # ICCV (biennial, odd years) and ECCV (biennial, even years) each sit
        # out one of the two rollover years; every other cohort venue appears
        # in both. The manually confirmed (naacl, 2027) extra target is
        # unconditional — present regardless of "today" or the rollover
        # window, since it isn't governed by the calendar formula at all.
        self.assertEqual(len(before), 13)
        self.assertEqual({item.year for item in before} - {2027}, {2026})
        self.assertNotIn("iccv", {item.venue_id for item in before})
        self.assertIn("eccv", {item.venue_id for item in before})
        self.assertIn(("naacl", 2027), {(i.venue_id, i.year) for i in before})
        self.assertEqual(len(rollover), 25)
        self.assertEqual({item.year for item in rollover}, {2026, 2027})
        self.assertEqual(len(next_year), 13)
        self.assertEqual({item.year for item in next_year}, {2027})
        self.assertIn("iccv", {item.venue_id for item in next_year})
        self.assertNotIn("eccv", {item.venue_id for item in next_year})
        self.assertIn(("naacl", 2027), {(i.venue_id, i.year) for i in next_year})
        # Every cohort-formula venue appears at least once across the
        # rollover window; NAACL sits outside the cohort by design (it comes
        # from extra_targets, not the calendar formula) and JMLR never
        # appears at all (continuous, excluded entirely).
        self.assertEqual(
            {item.venue_id for item in rollover} - {"naacl"}, cohort_venue_ids
        )
        self.assertNotIn("jmlr", {item.venue_id for item in rollover})

    def test_rollover_registers_all_targets_but_attempts_one_date(self):
        payload = dict(
            self.payload,
            targets_sha256=hashlib.sha256(TRACKED_TARGETS.read_bytes()).hexdigest(),
        )
        configuration = load_agent_production_configuration(
            payload, targets_path=TRACKED_TARGETS, target_date=date(2026, 10, 1)
        )
        provider = Provider()
        state = self.root / "rollover.sqlite3"
        effect = AgentProductionEffect(
            repository_root=self.repo,
            configuration=configuration,
            event_date_provider=provider,
            codex_invoker=Invoker(),
            transport_factory=TransportFactory([]),
        )

        effect.run(
            state_path=state,
            execution_root=self.execution,
            scheduled_for=NOW,
            observed_at=NOW,
        )

        with ControlStateRepository(
            state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: NOW
        ) as repository:
            schedules = repository.list_event_date_schedules()
        self.assertEqual(len(schedules), 25)
        self.assertEqual(len(provider.calls), 1)

    def test_configuration_accepts_legacy_single_and_v3_recipient_allowlist(self):
        self.assertEqual(self.configuration.resend_recipient_sha256s, ("a" * 64,))
        recipients = ("first@example.test", "second@example.test")
        payload = dict(self.payload)
        payload.pop("resend_recipient_sha256")
        payload.update({
            "schema_version": 3,
            "resend_recipient_sha256s": list(recipient_fingerprints(recipients)),
        })

        configuration = load_agent_production_configuration(
            payload, targets_path=self.targets
        )

        self.assertEqual(
            configuration.resend_recipient_sha256s,
            recipient_fingerprints(recipients),
        )
        with self.assertRaisesRegex(
            AgentProductionConfigurationError, "plural interface"
        ):
            configuration.resend_recipient_sha256
        with self.assertRaises(AgentProductionConfigurationError):
            load_agent_production_configuration(
                dict(payload, resend_recipient_sha256s=[]),
                targets_path=self.targets,
            )
        with self.assertRaises(AgentProductionConfigurationError):
            load_agent_production_configuration(
                dict(payload, resend_recipient_sha256s="not-a-list"),
                targets_path=self.targets,
            )

    def test_execution_volume_capacity_gate_precedes_every_external_effect(self):
        provider = Provider()
        invoker = Invoker()
        transports = TransportFactory([])
        configuration = load_agent_production_configuration(
            dict(self.payload, minimum_free_bytes=10_000_000_000_000),
            targets_path=self.targets,
        )
        effect = AgentProductionEffect(
            repository_root=self.repo,
            configuration=configuration,
            event_date_provider=provider,
            codex_invoker=invoker,
            transport_factory=transports,
        )
        with self.assertRaisesRegex(ValueError, "insufficient free space"):
            effect.run(
                state_path=self.state,
                execution_root=self.execution,
                scheduled_for=NOW,
                observed_at=NOW,
            )
        self.assertEqual(provider.calls, [])
        self.assertEqual(invoker.calls, [])
        self.assertEqual(transports.transports, [])

    def test_installed_source_and_managed_runs_are_safe_siblings(self):
        provider = Provider()
        source = self.execution / "agent-source"
        source.mkdir()
        effect = AgentProductionEffect(
            repository_root=source,
            configuration=self.configuration,
            event_date_provider=provider,
            codex_invoker=Invoker(),
            transport_factory=TransportFactory([]),
        )

        outcome = effect.run(
            state_path=self.state,
            execution_root=self.execution,
            scheduled_for=NOW,
            observed_at=NOW,
        )

        self.assertEqual(outcome.status, LocalEffectStatus.COMPLETED)
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse((source / "agent-change.txt").exists())
        self.assertFalse((self.execution / "agent-runs").exists())

    def test_execution_layout_rejects_source_and_managed_run_overlaps(self):
        provider = Provider()
        layouts = (
            (self.repo, self.repo),
            (self.repo, self.repo / "execution"),
            (self.execution / "agent-runs", self.execution),
            (self.execution / "agent-runs" / "source", self.execution),
        )
        for repository_root, execution_root in layouts:
            with self.subTest(
                repository_root=repository_root,
                execution_root=execution_root,
            ):
                execution_root.mkdir(parents=True, exist_ok=True)
                repository_root.mkdir(parents=True, exist_ok=True)
                effect = AgentProductionEffect(
                    repository_root=repository_root,
                    configuration=self.configuration,
                    event_date_provider=provider,
                    codex_invoker=Invoker(),
                    transport_factory=TransportFactory([]),
                )

                with self.assertRaisesRegex(ValueError, "must be disjoint"):
                    effect.run(
                        state_path=self.state,
                        execution_root=execution_root,
                        scheduled_for=NOW,
                        observed_at=NOW,
                    )

        self.assertEqual(provider.calls, [])

    def test_fake_wake_initializes_then_runs_and_retries_report_once(self):
        provider = Provider()
        invoker = Invoker()
        transports = TransportFactory([
            TransportFailure(FailureCategory.TIMEOUT),
            TransportReceipt("receipt:retry"),
        ])
        effect = AgentProductionEffect(
            repository_root=self.repo,
            configuration=self.configuration,
            event_date_provider=provider,
            codex_invoker=invoker,
            transport_factory=transports,
        )
        kwargs = {
            "state_path": self.state,
            "execution_root": self.execution,
            "scheduled_for": NOW,
            "observed_at": NOW,
        }

        initialized = effect.run(**kwargs)
        self.assertEqual(initialized.status, LocalEffectStatus.COMPLETED)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(invoker.calls, [])
        self.assertEqual(transports.transports, [])

        ran = effect.run(**kwargs)
        self.assertEqual(ran.status, LocalEffectStatus.COMPLETED)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(invoker.calls), 1)
        self.assertEqual(len(transports.transports), 1)
        self.assertFalse((self.repo / "agent-change.txt").exists())

        retried = effect.run(**kwargs)
        self.assertEqual(retried.status, LocalEffectStatus.COMPLETED)
        self.assertEqual(len(invoker.calls), 1)
        self.assertEqual(len(transports.transports), 2)
        self.assertEqual(
            transports.transports[0].calls[0][1],
            transports.transports[1].calls[0][1],
        )

        idle = effect.run(**kwargs)
        self.assertEqual(idle.status, LocalEffectStatus.NO_DUE_WORK)
        self.assertEqual(len(invoker.calls), 1)


if __name__ == "__main__":
    unittest.main()
