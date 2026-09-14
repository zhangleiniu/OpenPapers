import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.agent_operations import (
    AgentOperationError,
    _utc_text,
    mark_schedule_completed,
    promote_run,
    recover_interrupted_event_date,
    reopen_needs_human,
    update_monitor_configuration,
)
from automation.codex_agent import CodexProcessResult, run_claimed_codex_agent
from automation.control_state import ControlStateRepository
from automation.domain import Writer
from automation.due_policy import claim_due_agent_run
from automation.event_dates import (
    EventDateEstimate,
    EventDateTarget,
    initialize_event_dates,
)
from automation.local_service.agent_control import (
    initialize_agent_production_root,
    validate_agent_production_root,
)
from automation.local_service.production import (
    initialize_production_root,
    validate_production_root,
)
from automation.resend_notifications import recipient_fingerprints


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)


def recipient_fingerprint(address):
    return recipient_fingerprints((address,))[0]


class ScheduleOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state.sqlite3"
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            try:
                repository.register_event_date_target(
                    "icml", 2026, registered_at=NOW, lease=lease
                )
                repository.register_event_date_target(
                    "aaai", 2026, registered_at=NOW, lease=lease
                )
            finally:
                repository.release_lease(lease)

    def tearDown(self):
        self.temp.cleanup()

    def _repository(self, at=NOW):
        return ControlStateRepository(
            self.state, writer=Writer.LOCAL_CONTROL_PLANE, clock=lambda: at
        )

    def _claim(self, repository, lease, venue_id="icml", year=2026):
        return repository.claim_event_date_attempt(
            venue_id, year,
            provider_name="fake", provider_model="fake", prompt_version="v1",
            claimed_at=NOW, lease=lease,
        )

    def test_recover_closes_one_interrupted_attempt_as_retry(self):
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            self._claim(repository, lease)
            repository.release_lease(lease)
        # The claim was never completed: the designed ambiguous-active state.

        dry = recover_interrupted_event_date(
            self.state, apply=False, clock=lambda: NOW + timedelta(hours=1)
        )
        self.assertEqual((dry["venue_id"], dry["applied"]), ("icml", False))
        with self._repository() as repository:
            self.assertEqual(
                repository.get_event_date_schedule("icml", 2026).status, "active"
            )

        applied = recover_interrupted_event_date(
            self.state, apply=True, clock=lambda: NOW + timedelta(hours=1)
        )
        self.assertEqual(applied["status"], "pending")
        with self._repository() as repository:
            record = repository.get_event_date_schedule("icml", 2026)
            self.assertEqual(record.status, "pending")
            history = repository.event_date_attempt_history("icml", 2026)
            self.assertEqual(history[-1].outcome, "retry")
            self.assertEqual(history[-1].failure_category, "operator_interrupted")

    def test_recover_refuses_ambiguous_multiplicity(self):
        with self.assertRaisesRegex(AgentOperationError, "exactly one"):
            recover_interrupted_event_date(self.state, apply=True)

    def test_mark_completed_flips_an_existing_agent_schedule(self):
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            claim = self._claim(repository, lease)
            repository.complete_event_date_success(
                claim,
                estimated_event_date="2026-07-01",
                estimated_at=NOW,
                next_check_at=NOW + timedelta(days=3),
                lease=lease,
            )
            repository.release_lease(lease)

        summary = mark_schedule_completed(
            self.state, "icml", 2026, apply=True, clock=lambda: NOW
        )
        self.assertEqual(summary["shape"], "complete_agent_schedule")
        self.assertEqual(summary["status"], "completed")
        with self._repository() as repository:
            self.assertEqual(
                repository.get_agent_schedule("icml", 2026).status, "completed"
            )
        replay = mark_schedule_completed(
            self.state, "icml", 2026, apply=True, clock=lambda: NOW
        )
        self.assertEqual(replay.get("already"), "completed")

    def test_mark_completed_terminalizes_a_dateless_target(self):
        with self.assertRaisesRegex(AgentOperationError, "--event-date"):
            mark_schedule_completed(self.state, "aaai", 2026, apply=True)

        summary = mark_schedule_completed(
            self.state, "aaai", 2026, event_date="2026-01-20",
            apply=True, clock=lambda: NOW,
        )
        self.assertEqual(summary["shape"], "terminalize_date_stage")
        self.assertEqual(summary["status"], "completed")
        with self._repository() as repository:
            event = repository.get_event_date_schedule("aaai", 2026)
            self.assertEqual(event.status, "scheduled")
            self.assertEqual(event.provider_name, "operator")
            agent = repository.get_agent_schedule("aaai", 2026)
            self.assertEqual(agent.status, "completed")
            # The terminalized target must never surface as due work again
            # (the untouched icml fixture row legitimately remains pending).
            due = repository.list_due_event_date_schedules(
                NOW + timedelta(days=400), limit=10
            )
            self.assertNotIn("aaai", {record.venue_id for record in due})

    def test_mark_completed_refuses_unknown_target(self):
        with self.assertRaisesRegex(AgentOperationError, "not a registered"):
            mark_schedule_completed(self.state, "uai", 2026, apply=True)

    def test_mark_completed_can_chain_the_successor_year(self):
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            claim = self._claim(repository, lease)
            repository.complete_event_date_success(
                claim, estimated_event_date="2026-07-01", estimated_at=NOW,
                next_check_at=NOW + timedelta(days=3), lease=lease,
            )
            repository.release_lease(lease)

        dry = mark_schedule_completed(
            self.state, "icml", 2026, apply=False,
            chain_next_year_interval=1, clock=lambda: NOW,
        )
        self.assertEqual(dry["chain_successor_year"], 2027)
        with self._repository() as repository:
            self.assertIsNone(repository.get_event_date_schedule("icml", 2027))

        applied = mark_schedule_completed(
            self.state, "icml", 2026, apply=True,
            chain_next_year_interval=1, clock=lambda: NOW,
        )
        self.assertEqual(applied["status"], "completed")
        with self._repository() as repository:
            self.assertIsNotNone(repository.get_event_date_schedule("icml", 2027))

    def test_mark_completed_without_interval_does_not_chain(self):
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            claim = self._claim(repository, lease)
            repository.complete_event_date_success(
                claim, estimated_event_date="2026-07-01", estimated_at=NOW,
                next_check_at=NOW + timedelta(days=3), lease=lease,
            )
            repository.release_lease(lease)

        summary = mark_schedule_completed(
            self.state, "icml", 2026, apply=True, clock=lambda: NOW,
        )
        self.assertNotIn("chain_successor_year", summary)
        with self._repository() as repository:
            self.assertIsNone(repository.get_event_date_schedule("icml", 2027))

    def test_mark_completed_rejects_a_non_positive_interval(self):
        with self.assertRaisesRegex(AgentOperationError, "positive integer"):
            mark_schedule_completed(
                self.state, "icml", 2026, apply=True,
                chain_next_year_interval=0,
            )

    def _seed_needs_human(self, venue_id="icml", year=2026):
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            claim = self._claim(repository, lease, venue_id=venue_id, year=year)
            repository.complete_event_date_success(
                claim, estimated_event_date="2026-07-01", estimated_at=NOW,
                next_check_at=NOW, lease=lease,
            )
            repository.release_lease(lease)
        with self._repository() as repository:
            lease = repository.acquire_lease("agent-runner")
            outcome = repository.claim_due_agent_run(
                claimed_at=NOW, monthly_run_limit=10,
                systemic_failure_threshold=3,
                systemic_failure_window=timedelta(hours=24),
                systemic_circuit_delay=timedelta(hours=24),
                lease=lease,
            )
            repository.complete_agent_run_attempt(
                outcome.claim, disposition="needs_human",
                explanation="fixture: blocked on something only a human can fix",
                completed_at=NOW, next_check_at=None, suggested_retry_at=None,
                failure_category=None, pause_after_failure=False, lease=lease,
            )
            repository.release_lease(lease)

    def test_reopen_needs_human_dry_run_changes_nothing(self):
        self._seed_needs_human()
        summary = reopen_needs_human(
            self.state, "icml", 2026, apply=False, clock=lambda: NOW
        )
        self.assertFalse(summary["applied"])
        self.assertEqual(summary["attempt_count"], 1)
        with self._repository() as repository:
            self.assertEqual(
                repository.get_agent_schedule("icml", 2026).status, "needs_human"
            )

    def test_reopen_needs_human_flips_to_scheduled_and_keeps_history(self):
        self._seed_needs_human()
        summary = reopen_needs_human(
            self.state, "icml", 2026, delay_minutes=30,
            apply=True, clock=lambda: NOW,
        )
        self.assertEqual(summary["status"], "scheduled")
        with self._repository() as repository:
            record = repository.get_agent_schedule("icml", 2026)
            self.assertEqual(record.status, "scheduled")
            self.assertEqual(record.next_check_at, _utc_text(NOW + timedelta(minutes=30)))
            self.assertIsNone(record.last_gate_reason)
            # Run history survives a reopen — it is not a fresh start.
            self.assertEqual(record.attempt_count, 1)
            self.assertEqual(record.last_disposition, "needs_human")

    def test_reopen_needs_human_refuses_other_statuses(self):
        # icml/2026 here is still 'scheduled' (never claimed), not needs_human.
        with self._repository() as repository:
            lease = repository.acquire_lease("event-date-initializer")
            claim = self._claim(repository, lease)
            repository.complete_event_date_success(
                claim, estimated_event_date="2026-07-01", estimated_at=NOW,
                next_check_at=NOW + timedelta(days=1), lease=lease,
            )
            repository.release_lease(lease)
        with self.assertRaisesRegex(AgentOperationError, "not needs_human"):
            reopen_needs_human(self.state, "icml", 2026, apply=True)

    def test_reopen_needs_human_refuses_unknown_target(self):
        with self.assertRaisesRegex(AgentOperationError, "not a registered"):
            reopen_needs_human(self.state, "uai", 2026, apply=True)

    def test_reopen_needs_human_rejects_negative_delay(self):
        self._seed_needs_human()
        with self.assertRaisesRegex(AgentOperationError, "non-negative integer"):
            reopen_needs_human(
                self.state, "icml", 2026, delay_minutes=-1, apply=True
            )


class MonitorConfigurationOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.internal = root / "internal"
        self.repository = root / "runtime"
        for path in (
            self.internal, self.internal / "control", self.internal / "monitor",
            self.repository / "automation" / "config",
        ):
            path.mkdir(mode=0o700, parents=True)
        self.registry = self.repository / "automation" / "conferences.json"
        self.registry.write_bytes(
            (ROOT / "automation" / "conferences.json").read_bytes()
        )
        targets = self.repository / "automation" / "config" / "agent_targets.v1.json"
        targets.write_text(json.dumps({
            "schema_version": 1,
            "targets": [{"venue_id": "icml", "year": 2026}],
        }, indent=2) + "\n", encoding="utf-8")
        self.agent_source = root / "agent-source"
        self.agent_source.mkdir(mode=0o700)
        for command in (
            ("git", "init", "-q"),
            ("git", "config", "user.name", "Fixture"),
            ("git", "config", "user.email", "fixture@example.invalid"),
        ):
            subprocess.run(command, cwd=self.agent_source, check=True)
        (self.agent_source / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(("git", "add", "README.md"), cwd=self.agent_source, check=True)
        subprocess.run(
            ("git", "commit", "-q", "-m", "fixture"),
            cwd=self.agent_source, check=True,
        )
        commit = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=self.agent_source,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        initialize_production_root(self.internal, {
            "schema_version": 1,
            # Deliberately stale: hash of different bytes and the wrong count.
            "registry_sha256": hashlib.sha256(b"stale registry").hexdigest(),
            "backup_sha256": "a" * 64,
            "remote_state_generation": "123456789",
            "expected_source_count": 6,
            "smtp_host": "smtp.example.test",
            "smtp_port": 465,
            "smtp_username": "openpapers",
            "email_from": "from@example.test",
            "email_to": "to@example.test",
        }, {
            "schema_version": 1,
            "openreview_username": "review-user",
            "openreview_password": "review-password",
            "smtp_password": "smtp-password",
        })
        initialize_agent_production_root(
            self.internal, self.repository,
            {
                "schema_version": 2,
                "mode": "agent_production_control",
                "external_effects_enabled": False,
                "agent_source_commit": commit,
                "agent_configuration": {
                    "schema_version": 2,
                    "targets_sha256": hashlib.sha256(
                        targets.read_bytes()
                    ).hexdigest(),
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
                    "failure_backoff_hours": [2, 6, 24],
                    "max_consecutive_failures": 5,
                    "monthly_run_limit": 120,
                    "systemic_failure_threshold": 3,
                    "systemic_failure_window_hours": 6,
                    "systemic_circuit_delay_hours": 6,
                    "minimum_free_bytes": 10_000_000_000,
                    "retention_max_retained": 10,
                    "retention_max_age_days": 30,
                    "retention_max_removals_per_run": 2,
                    "resend_recipient_sha256": recipient_fingerprint(
                        "to@example.test"
                    ),
                },
            },
            {"schema_version": 2, "resend": None},
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_update_rewrites_registry_configuration(self):
        dry = update_monitor_configuration(
            self.internal, self.repository, apply=False
        )
        self.assertTrue(dry["changed"])
        self.assertEqual(dry["before"]["expected_source_count"], 6)
        self.assertEqual(dry["after"]["expected_source_count"], 18)
        # Dry run left everything untouched and still valid.
        validate_agent_production_root(self.internal, self.repository)
        self.assertEqual(
            validate_production_root(self.internal)[0].expected_source_count, 6
        )

        applied = update_monitor_configuration(
            self.internal, self.repository, apply=True
        )
        self.assertTrue(applied["validated"])
        configuration, _ = validate_production_root(self.internal)
        self.assertEqual(configuration.expected_source_count, 18)
        self.assertEqual(
            configuration.registry_sha256,
            hashlib.sha256(self.registry.read_bytes()).hexdigest(),
        )
        validate_agent_production_root(self.internal, self.repository)

        replay = update_monitor_configuration(
            self.internal, self.repository, apply=True
        )
        self.assertFalse(replay["changed"])


def _git(root, *args):
    return subprocess.run(
        ("git", *args), cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


class _FixedDateProvider:
    name = model = prompt_version = "fixture"

    def estimate(self, request):
        # A day safely behind PROMOTE_NOW, so the check-time computed from
        # this date (8am America/Chicago) is already due regardless of the
        # UTC-offset arithmetic for that timezone/season.
        return EventDateEstimate((PROMOTE_NOW - timedelta(days=1)).date(), "fixture")


class _ScriptedInvoker:
    def __init__(self, write, *, disposition="success",
                 explanation="Scraped and validated the accepted papers."):
        self._write = write
        self._disposition = disposition
        self._explanation = explanation

    def invoke(self, invocation):
        self._write(invocation.cwd)
        return CodexProcessResult(0, json.dumps({
            "disposition": self._disposition,
            "explanation": self._explanation,
            "suggested_retry_at": None,
            "failure_category": None,
        }), "")


def _valid_paper(venue_id, year):
    return {
        "id": "paper1", "title": "Paper", "authors": ["Author"],
        "year": year, "conference": venue_id.upper(), "url": "https://example.test",
        "bibtex": "@article{x}", "abstract": "Abstract",
        "pdf_url": "https://example.test/paper1.pdf",
        "pdf_path": f"data/papers/{venue_id}/{year}/paper1.pdf",
    }


def _write_scrape_output(cwd, venue_id, year, *, include_pdf=True,
                          pdf_bytes=b"%PDF-" + b"x" * 1024):
    metadata_dir = cwd / "data" / "metadata" / venue_id
    papers_dir = cwd / "data" / "papers" / venue_id / str(year)
    metadata_dir.mkdir(parents=True)
    papers_dir.mkdir(parents=True)
    if include_pdf:
        (papers_dir / "paper1.pdf").write_bytes(pdf_bytes)
    payload = [_valid_paper(venue_id, year)]
    (metadata_dir / f"{venue_id}_{year}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


PROMOTE_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


class PromoteRunTests(unittest.TestCase):
    VENUE = "icml"
    YEAR = 2026

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Fixture")
        _git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / ".gitignore").write_text("data/\n", encoding="utf-8")
        (self.repo / "README.md").write_text("original\n", encoding="utf-8")
        _git(self.repo, "add", ".gitignore", "README.md")
        _git(self.repo, "commit", "-q", "-m", "fixture")
        self.state = self.root / "state.sqlite3"
        self.data_root = self.root / "data"
        self.runs_root = self.root / "runs"
        initialize_event_dates(
            self.state, (EventDateTarget(self.VENUE, self.YEAR),),
            _FixedDateProvider(), clock=lambda: PROMOTE_NOW,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, write, **kwargs):
        claim = claim_due_agent_run(self.state, clock=lambda: PROMOTE_NOW).claim
        outcome = run_claimed_codex_agent(
            self.state, self.repo, self.runs_root, claim,
            clock=lambda: PROMOTE_NOW, invoker=_ScriptedInvoker(write, **kwargs),
        )
        return claim.run_id, outcome

    def _promote(self, **kwargs):
        return promote_run(
            self.state, repository_root=self.repo, data_root=self.data_root,
            clock=lambda: PROMOTE_NOW, **kwargs,
        )

    def test_dry_run_reports_full_plan_and_changes_nothing(self):
        def write(cwd):
            _write_scrape_output(cwd, self.VENUE, self.YEAR)
            (cwd / "README.md").write_text("updated\n", encoding="utf-8")

        run_id, _ = self._run(write)
        summary = self._promote(run_id=run_id)

        self.assertFalse(summary["applied"])
        self.assertEqual(summary["data"]["metadata"]["action"], "copy")
        self.assertEqual(summary["data"]["pdfs"]["copy"], 1)
        readme_entry = next(
            f for f in summary["code"]["files"] if f["path"] == "README.md"
        )
        self.assertEqual(readme_entry["action"], "copy")
        self.assertFalse((self.data_root / "metadata").exists())
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")

    def test_apply_copies_data_and_code_and_is_idempotent(self):
        def write(cwd):
            _write_scrape_output(cwd, self.VENUE, self.YEAR)
            (cwd / "README.md").write_text("updated\n", encoding="utf-8")

        run_id, _ = self._run(write)
        applied = self._promote(run_id=run_id, apply=True)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["post_apply_validation"]["issues"], {})

        metadata_path = (
            self.data_root / "metadata" / self.VENUE / f"{self.VENUE}_{self.YEAR}.json"
        )
        self.assertTrue(metadata_path.exists())
        self.assertTrue(
            (self.data_root / "papers" / self.VENUE / str(self.YEAR) / "paper1.pdf")
            .exists()
        )
        self.assertTrue(
            (self.data_root / "metadata" / "pdf_completeness.v1.json").exists()
        )
        self.assertEqual((self.repo / "README.md").read_text(), "updated\n")
        self.assertIn("README.md", _git(self.repo, "status", "--porcelain"))

        replay = self._promote(run_id=run_id, apply=True)
        self.assertEqual(replay["data"]["metadata"]["action"], "skip")
        self.assertEqual(replay["data"]["pdfs"]["copy"], 0)
        self.assertEqual(replay["code"]["to_copy"], 0)

    def test_refuses_non_success_disposition(self):
        run_id, _ = self._run(
            lambda cwd: None, disposition="needs_human", explanation="blocked",
        )
        with self.assertRaisesRegex(AgentOperationError, "success"):
            self._promote(run_id=run_id)

    def test_refuses_when_worktree_is_gone(self):
        run_id, outcome = self._run(
            lambda cwd: _write_scrape_output(cwd, self.VENUE, self.YEAR)
        )
        shutil.rmtree(outcome.worktree_path)
        with self.assertRaisesRegex(AgentOperationError, "no longer exists on disk"):
            self._promote(run_id=run_id)

    def test_refuses_failed_independent_validation(self):
        run_id, _ = self._run(
            lambda cwd: _write_scrape_output(
                cwd, self.VENUE, self.YEAR, include_pdf=False
            )
        )
        with self.assertRaisesRegex(AgentOperationError, "validation"):
            self._promote(run_id=run_id)

    def test_data_conflict_requires_force(self):
        run_id, _ = self._run(
            lambda cwd: _write_scrape_output(cwd, self.VENUE, self.YEAR)
        )
        metadata_dir = self.data_root / "metadata" / self.VENUE
        metadata_dir.mkdir(parents=True)
        (metadata_dir / f"{self.VENUE}_{self.YEAR}.json").write_text(
            json.dumps([{"id": "different"}]), encoding="utf-8"
        )

        refused = self._promote(run_id=run_id, apply=True)
        self.assertEqual(refused["data"]["metadata"]["action"], "conflict")
        self.assertTrue(refused["data"]["blocked_by_metadata_conflict"])
        self.assertEqual(
            json.loads((metadata_dir / f"{self.VENUE}_{self.YEAR}.json").read_text()),
            [{"id": "different"}],
        )

        forced = self._promote(run_id=run_id, apply=True, force=True)
        self.assertEqual(forced["data"]["metadata"]["action"], "conflict")
        landed = json.loads(
            (metadata_dir / f"{self.VENUE}_{self.YEAR}.json").read_text()
        )
        self.assertEqual(landed[0]["id"], "paper1")

    def test_identical_data_is_a_safe_noop_without_force(self):
        def write(cwd):
            _write_scrape_output(cwd, self.VENUE, self.YEAR)

        run_id, outcome = self._run(write)
        worktree = outcome.worktree_path
        metadata_dir = self.data_root / "metadata" / self.VENUE
        pdf_dir = self.data_root / "papers" / self.VENUE / str(self.YEAR)
        metadata_dir.mkdir(parents=True)
        pdf_dir.mkdir(parents=True)
        shutil.copy(
            worktree / "data" / "metadata" / self.VENUE / f"{self.VENUE}_{self.YEAR}.json",
            metadata_dir / f"{self.VENUE}_{self.YEAR}.json",
        )
        shutil.copy(
            worktree / "data" / "papers" / self.VENUE / str(self.YEAR) / "paper1.pdf",
            pdf_dir / "paper1.pdf",
        )

        summary = self._promote(run_id=run_id)
        self.assertEqual(summary["data"]["metadata"]["action"], "skip")
        self.assertEqual(summary["data"]["pdfs"]["skip"], 1)
        self.assertEqual(summary["data"]["pdfs"]["copy"], 0)

    def test_code_conflict_refuses_that_file_but_still_lands_data(self):
        def write(cwd):
            _write_scrape_output(cwd, self.VENUE, self.YEAR)
            (cwd / "README.md").write_text("agent version\n", encoding="utf-8")

        run_id, _ = self._run(write)
        (self.repo / "README.md").write_text("operator changed\n", encoding="utf-8")

        applied = self._promote(run_id=run_id, apply=True)
        readme_entry = next(
            f for f in applied["code"]["files"] if f["path"] == "README.md"
        )
        self.assertEqual(readme_entry["action"], "refuse")
        self.assertEqual((self.repo / "README.md").read_text(), "operator changed\n")
        self.assertTrue(
            (self.data_root / "metadata" / self.VENUE / f"{self.VENUE}_{self.YEAR}.json")
            .exists()
        )

    def test_resolves_latest_terminal_run_by_venue_and_year(self):
        self._run(lambda cwd: None, disposition="needs_human", explanation="blocked")
        reopen_needs_human(
            self.state, self.VENUE, self.YEAR, apply=True, clock=lambda: PROMOTE_NOW,
        )
        run_id_2, _ = self._run(
            lambda cwd: _write_scrape_output(cwd, self.VENUE, self.YEAR)
        )

        summary = self._promote(venue_id=self.VENUE, year=self.YEAR)
        self.assertEqual(summary["run_id"], run_id_2)

    def test_requires_exactly_one_natural_key(self):
        with self.assertRaisesRegex(AgentOperationError, "run-id"):
            self._promote()
        with self.assertRaisesRegex(AgentOperationError, "run-id"):
            self._promote(run_id="agent-run:x", venue_id=self.VENUE, year=self.YEAR)


if __name__ == "__main__":
    unittest.main()
