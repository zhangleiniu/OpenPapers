import ast
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from automation.cases import (
    CaseControl,
    CaseControlRequest,
    CaseObservation,
    derive_case_id,
)
from automation.configuration import load_policy_config
from automation.control_state import (
    ControlStateRepository,
    LeaseLostError,
    NotificationIntentConflictError,
)
from automation.domain import ActionType, BlockerCode
from automation.lifecycle import (
    ActionIntent,
    CasePayload,
    HumanReviewPayload,
    QueueExistingScraperPayload,
    RecheckPayload,
    TransitionNoticePayload,
)
from automation.notification_integration import (
    integrate_action_intents,
    persist_due_digest_shadow,
)
from automation.notifications import build_immediate_notification


NOW = datetime(2026, 7, 13, 23, 30, tzinfo=timezone.utc)
MODULE = Path(__file__).resolve().parents[1] / "notification_integration.py"


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def timestamp(value):
    return value.isoformat().replace("+00:00", "Z")


def transition_action():
    return ActionIntent(
        action_id="action:transition:icml:2026:pdf-ready",
        action_type=ActionType.NOTIFY_TRANSITION,
        venue_id="icml",
        year=2026,
        evidence_ids=("verification:icml:2026:pdf",),
        payload=TransitionNoticePayload(
            transition_id="transition:icml:2026:pdf-ready",
            previous_state="metadata_ready",
            new_state="pdf_ready",
        ),
    )


def case_action(*, action_id="action:case:icml:2026:blockers"):
    return ActionIntent(
        action_id=action_id,
        action_type=ActionType.CREATE_OR_UPDATE_CASE,
        venue_id="icml",
        year=2026,
        evidence_ids=("verification:icml:2026:html",),
        payload=CasePayload(
            blocker_codes=(
                BlockerCode.NO_PDF.value,
                BlockerCode.UNSUPPORTED_SCRAPER.value,
            ),
            verification_status="partially_verified",
        ),
    )


def ignored_actions():
    common = {
        "venue_id": "icml",
        "year": 2026,
        "evidence_ids": ("verification:icml:2026:html",),
    }
    return (
        ActionIntent(
            action_id="action:recheck:icml:2026",
            action_type=ActionType.RECHECK_AT,
            payload=RecheckPayload(
                at="2026-07-20T23:30:00Z", reason="await_archival_pdf"
            ),
            **common,
        ),
        ActionIntent(
            action_id="action:review:icml:2026",
            action_type=ActionType.REQUEST_HUMAN_REVIEW,
            payload=HumanReviewPayload(
                reasons=("identity_mismatch",),
                verification_status="review_required",
            ),
            **common,
        ),
        ActionIntent(
            action_id="action:queue:icml:2026",
            action_type=ActionType.QUEUE_EXISTING_SCRAPER,
            payload=QueueExistingScraperPayload(
                readiness="pdf_ready",
                scraper_module="scrapers.icml",
                scraper_class="ICMLScraper",
            ),
            **common,
        ),
    )


def case_observation(*, venue_id, blocker, age_days, suffix="1"):
    observed = NOW - timedelta(days=age_days)
    return CaseObservation(
        event_id=f"case-event:{venue_id}:2026:{blocker}:{suffix}",
        venue_id=venue_id,
        year=2026,
        blocker=blocker,
        summary=f"Sanitized {blocker} fixture.",
        evidence_ids=(f"evidence:{venue_id}:2026:{blocker}:{suffix}",),
        observed_at=timestamp(observed),
    )


class ActionShadowIntegrationTests(unittest.TestCase):
    def test_expired_lease_cannot_register_shadow_output(self):
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "control.sqlite3", clock=clock
            ) as repository:
                lease = repository.acquire_lease(
                    "p3-4-shadow", ttl_seconds=1
                )
                clock.value = NOW + timedelta(seconds=2)
                with self.assertRaises(LeaseLostError):
                    integrate_action_intents(
                        repository,
                        (transition_action(),),
                        lease=lease,
                        occurred_at=NOW,
                    )
                self.assertIsNone(
                    repository.get_notification_by_source(
                        transition_action().action_id
                    )
                )

    def test_transition_and_case_events_persist_at_most_one_pending_output(self):
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite3"
            with ControlStateRepository(path, clock=clock) as repository:
                lease = repository.acquire_lease("p3-4-shadow")
                first = integrate_action_intents(
                    repository,
                    (transition_action(), case_action(), *ignored_actions()),
                    lease=lease,
                    occurred_at=NOW,
                    run_ids=("run:p3-4:fixture",),
                )
                self.assertEqual(len(first.case_writes), 2)
                self.assertEqual(len(first.notification_writes), 3)
                self.assertTrue(
                    all(item.applied for item in first.notification_writes)
                )
                self.assertEqual(
                    first.ignored_action_ids,
                    tuple(action.action_id for action in ignored_actions()),
                )

                replay = integrate_action_intents(
                    repository,
                    (transition_action(), case_action(), *ignored_actions()),
                    lease=lease,
                    occurred_at=NOW,
                    run_ids=("run:p3-4:fixture",),
                )
                self.assertEqual(len(replay.case_writes), 2)
                self.assertTrue(all(item.replayed for item in replay.case_writes))
                self.assertTrue(
                    all(not item.applied for item in replay.notification_writes)
                )

                notification_ids = tuple(
                    item.record.notification_id for item in first.notification_writes
                )
                self.assertEqual(len(set(notification_ids)), 3)
                for item in first.notification_writes:
                    record = repository.get_notification(item.record.notification_id)
                    self.assertEqual(record.status, "pending")
                    self.assertEqual(record.attempt_count, 0)
                    self.assertIsNone(record.delivered_at)
                    self.assertEqual(
                        repository.notification_attempt_history(
                            record.notification_id
                        ),
                        (),
                    )
                    for source_id in record.intent.source_ids:
                        self.assertEqual(
                            repository.get_notification_by_source(source_id), record
                        )

                transition = first.notification_writes[0].record.intent
                conflicting = build_immediate_notification(
                    event_id=transition.source_ids[0],
                    occurred_at=NOW,
                    venue_id="icml",
                    year=2026,
                    summary="A different meaning for the same event.",
                    evidence_ids=transition.evidence_ids,
                    run_ids=transition.run_ids,
                )
                self.assertEqual(
                    conflicting.notification_id, transition.notification_id
                )
                with self.assertRaisesRegex(
                    NotificationIntentConflictError, "different meaning"
                ):
                    repository.register_notification_intent(
                        conflicting,
                        lease=lease,
                        registered_at=NOW,
                    )

                no_pdf = repository.get_case(
                    derive_case_id("icml", 2026, BlockerCode.NO_PDF)
                )
                unsupported = repository.get_case(
                    derive_case_id("icml", 2026, BlockerCode.UNSUPPORTED_SCRAPER)
                )
                self.assertIsNotNone(no_pdf)
                self.assertIsNotNone(unsupported)
                self.assertEqual(len(repository.case_history(no_pdf.case_id)), 1)
                self.assertEqual(
                    len(repository.case_event_history(no_pdf.case_id)), 1
                )

            with ControlStateRepository(path, clock=clock) as reopened:
                for notification_id in notification_ids:
                    record = reopened.get_notification(notification_id)
                    self.assertEqual(record.status, "pending")
                    self.assertEqual(record.attempt_count, 0)

    def test_case_commit_survives_shadow_output_failure_and_replay_recovers(self):
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "control.sqlite3", clock=clock
            ) as repository:
                lease = repository.acquire_lease("p3-4-shadow")
                original = repository.register_notification_intent
                with mock.patch.object(
                    repository,
                    "register_notification_intent",
                    side_effect=RuntimeError("forced shadow registration failure"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "forced shadow"):
                        integrate_action_intents(
                            repository,
                            (case_action(),),
                            lease=lease,
                            occurred_at=NOW,
                        )

                case_id = derive_case_id("icml", 2026, BlockerCode.NO_PDF)
                retained = repository.get_case(case_id)
                self.assertIsNotNone(retained)
                self.assertEqual(len(repository.case_history(case_id)), 1)
                self.assertEqual(len(repository.case_event_history(case_id)), 1)

                with mock.patch.object(
                    repository, "register_notification_intent", wraps=original
                ) as registration:
                    recovered = integrate_action_intents(
                        repository,
                        (case_action(),),
                        lease=lease,
                        occurred_at=NOW,
                    )
                self.assertEqual(len(recovered.notification_writes), 2)
                self.assertEqual(registration.call_count, 2)
                self.assertTrue(
                    all(item.applied for item in recovered.notification_writes)
                )
                self.assertEqual(
                    tuple(item.replayed for item in recovered.case_writes),
                    (True, False),
                )
                self.assertEqual(len(repository.case_history(case_id)), 1)
                self.assertEqual(len(repository.case_event_history(case_id)), 1)


class ReminderShadowIntegrationTests(unittest.TestCase):
    def test_repository_cases_form_one_digest_and_claimed_slots_are_filtered(self):
        clock = MutableClock()
        policy = load_policy_config()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite3"
            with ControlStateRepository(path, clock=clock) as repository:
                lease = repository.acquire_lease("p3-4-reminders")
                weekly = repository.observe_case(
                    case_observation(
                        venue_id="icml",
                        blocker=BlockerCode.NO_PDF.value,
                        age_days=7,
                    ),
                    lease=lease,
                )
                monthly = repository.observe_case(
                    case_observation(
                        venue_id="ijcai",
                        blocker=BlockerCode.NO_PUBLIC_LIST.value,
                        age_days=30,
                    ),
                    lease=lease,
                )
                closed = repository.observe_case(
                    case_observation(
                        venue_id="aistats",
                        blocker=BlockerCode.UNKNOWN_DOWNLOAD_SOURCE.value,
                        age_days=7,
                    ),
                    lease=lease,
                )
                repository.control_case(
                    closed.record.case_id,
                    CaseControlRequest(
                        event_id="case-control:aistats:2026:resolve",
                        action=CaseControl.RESOLVE,
                        at=timestamp(NOW - timedelta(days=1)),
                        reason="Resolved fixture.",
                    ),
                    lease=lease,
                )

                first = persist_due_digest_shadow(
                    repository,
                    policy=policy,
                    lease=lease,
                    now=NOW,
                    run_ids=("run:p3-4:reminders",),
                )
                self.assertEqual(first.digest.due_count, 2)
                self.assertEqual(
                    tuple(group.cadence.value for group in first.digest.groups),
                    ("weekly", "monthly"),
                )
                self.assertIsNotNone(first.notification_write)
                self.assertTrue(first.notification_write.applied)
                record = first.notification_write.record
                self.assertEqual(record.status, "pending")
                self.assertEqual(record.attempt_count, 0)
                self.assertIn(weekly.record.case_id, record.intent.body)
                self.assertIn(monthly.record.case_id, record.intent.body)
                self.assertNotIn(closed.record.case_id, record.intent.body)

                replay = persist_due_digest_shadow(
                    repository,
                    policy=policy,
                    lease=lease,
                    now=NOW,
                    run_ids=("run:p3-4:reminders",),
                )
                self.assertEqual(replay.digest.due_count, 0)
                self.assertIsNone(replay.notification_write)
                self.assertEqual(len(replay.claimed_source_ids), 2)

                new_case = repository.observe_case(
                    case_observation(
                        venue_id="naacl",
                        blocker=BlockerCode.NO_PDF.value,
                        age_days=7,
                    ),
                    lease=lease,
                )
                incremental = persist_due_digest_shadow(
                    repository,
                    policy=policy,
                    lease=lease,
                    now=NOW,
                    run_ids=("run:p3-4:reminders:incremental",),
                )
                self.assertEqual(incremental.digest.due_count, 1)
                self.assertIn(
                    new_case.record.case_id,
                    incremental.notification_write.record.intent.body,
                )
                self.assertNotEqual(
                    incremental.notification_write.record.notification_id,
                    record.notification_id,
                )
                self.assertEqual(
                    repository.notification_attempt_history(record.notification_id),
                    (),
                )


class ScopeBoundaryTests(unittest.TestCase):
    def test_integration_has_no_transport_network_or_orchestration_import(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        forbidden = {
            "smtplib",
            "socket",
            "http",
            "urllib",
            "requests",
            "prefect",
            "prefect_email",
            "google.cloud",
        }
        self.assertFalse(
            any(
                name == blocked or name.startswith(f"{blocked}.")
                for name in imports
                for blocked in forbidden
            )
        )
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("deliver_notification", source)
        self.assertNotIn("NotificationTransport", source)
        self.assertNotIn(".send(", source)


if __name__ == "__main__":
    unittest.main()
