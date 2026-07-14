import ast
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.cases import CaseObservation
from automation.configuration import load_policy_config, load_venue_catalog
from automation.control_state import (
    ControlStateRepository,
    SchedulerWakeupConflictError,
)
from automation.domain import ActionType, BlockerCode, Writer
from automation.local_control_plane import (
    LocalControlCompositionError,
    VerificationBundle,
    run_local_control_wakeup,
)
from automation.verification import build_verification_result


FIXTURES = Path(__file__).with_name("fixtures")
MODULE = Path(__file__).resolve().parents[1] / "local_control_plane.py"
NOW = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_bundle():
    discovery = load_json(
        FIXTURES / "phase0" / "discovery-result.v1.json"
    )
    request = load_json(
        FIXTURES / "phase2" / "verification-request.v1.json"
    )
    retained_result = load_json(
        FIXTURES / "phase2" / "verification-result.v1.json"
    )
    result = build_verification_result(
        request,
        discovery,
        overall_status="partially_verified",
        verified_at=retained_result["verified_at"],
        source_observations=retained_result["source_observations"],
        findings=retained_result["findings"],
        verified_facets=retained_result["verified_facets"],
        verified_milestones=retained_result["verified_milestones"],
        uncertainties=("Sanitized fixture requires deterministic review.",),
    )
    return discovery, VerificationBundle(request=request, result=result)


def due_state(*, consumed=False):
    state = load_json(FIXTURES / "phase0" / "conference-state.v1.json")
    state["next_check_at"] = "2026-07-13T14:00:00Z"
    state["next_check_reason"] = "unknown_schedule_fallback"
    state["updated_at"] = "2026-07-13T13:00:00Z"
    if consumed:
        _, bundle = fixture_bundle()
        state["evidence_ids"].append(bundle.result["verification_id"])
    return state


def seed_local_state(path, *, state=None, old_case=False):
    clock = MutableClock()
    with ControlStateRepository(
        path,
        writer=Writer.LOCAL_CONTROL_PLANE,
        clock=clock,
    ) as repository:
        lease = repository.acquire_lease("p4-l2-fixture-seed")
        try:
            repository.store_conference_state(
                state or due_state(),
                expected_revision=0,
                lease=lease,
                stored_at=NOW - timedelta(days=1),
            )
            if old_case:
                repository.observe_case(
                    CaseObservation(
                        event_id="case-event:p4-l2-old-case",
                        venue_id="aistats",
                        year=2026,
                        blocker=BlockerCode.NO_PDF,
                        summary="Sanitized old unresolved fixture.",
                        evidence_ids=("snapshot:p4-l2-old-case",),
                        observed_at="2026-07-07T14:00:00Z",
                    ),
                    lease=lease,
                )
        finally:
            repository.release_lease(lease)


class FakeDiscovery:
    def __init__(self, result, *, state_path=None, events=None):
        self.result = result
        self.state_path = state_path
        self.events = events if events is not None else []
        self.calls = []

    def discover(self, request):
        self.calls.append(request)
        self.events.append("discovery")
        if self.state_path is not None:
            with ControlStateRepository(
                self.state_path,
                writer=Writer.LOCAL_CONTROL_PLANE,
                clock=MutableClock(),
            ) as repository:
                wakeup = repository.list_scheduler_wakeups()[0]
                selections = repository.list_due_work_selections()
                if wakeup.status != "active" or len(selections) != 1:
                    raise AssertionError("fake effect ran outside an active plan")
        return deepcopy(self.result)


class FakeVerification:
    def __init__(self, bundles, *, events=None):
        self.bundles = bundles
        self.events = events if events is not None else []
        self.calls = []

    def verify(self, discovery, *, observed_at):
        self.calls.append((deepcopy(discovery), observed_at))
        self.events.append("verification")
        return deepcopy(self.bundles)


class LocalControlCompositionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_venue_catalog()
        self.policy = load_policy_config()

    def test_due_fixture_composes_state_cases_reminders_and_inert_actions(self):
        discovery, bundle = fixture_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            seed_local_state(path, old_case=True)
            events = []
            discovery_effect = FakeDiscovery(
                discovery, state_path=path, events=events
            )
            verification_effect = FakeVerification((bundle,), events=events)

            outcome = run_local_control_wakeup(
                path,
                scheduled_for=NOW,
                clock=MutableClock(),
                discovery_effect=discovery_effect,
                verification_effect=verification_effect,
                catalog=self.catalog,
                policy=self.policy,
            )

            self.assertEqual(events, ["discovery", "verification"])
            self.assertFalse(outcome.replayed)
            self.assertEqual(outcome.scheduler.record.status, "completed")
            self.assertEqual(len(outcome.selections), 1)
            selected = outcome.selections[0]
            action_types = {item.action_type for item in selected.actions}
            self.assertIn(ActionType.RECHECK_AT, action_types)
            self.assertIn(ActionType.NOTIFY_TRANSITION, action_types)
            self.assertIn(ActionType.CREATE_OR_UPDATE_CASE, action_types)
            self.assertIn(ActionType.REQUEST_HUMAN_REVIEW, action_types)
            self.assertNotIn(ActionType.QUEUE_EXISTING_SCRAPER, action_types)
            self.assertEqual(len(selected.action_integration), 1)
            integration = selected.action_integration[0]
            self.assertGreaterEqual(len(integration.case_writes), 1)
            self.assertIn(
                next(
                    action.action_id
                    for action in selected.actions
                    if action.action_type is ActionType.RECHECK_AT
                ),
                integration.ignored_action_ids,
            )
            self.assertIsNotNone(outcome.digest)
            self.assertEqual(outcome.digest.digest.due_count, 1)
            self.assertIsNotNone(outcome.digest.notification_write)
            self.assertEqual(
                outcome.digest.notification_write.record.status, "pending"
            )
            self.assertEqual(
                outcome.digest.notification_write.record.attempt_count, 0
            )

            with ControlStateRepository(
                path,
                writer=Writer.LOCAL_CONTROL_PLANE,
                clock=MutableClock(),
            ) as repository:
                state = repository.get_conference_state("icml", 2026)
                self.assertEqual(state.revision, selected.final_state_revision)
                self.assertEqual(state.state["lifecycle_state"], "scheduled")
                self.assertNotEqual(
                    state.state["next_check_at"],
                    selected.selection.next_check_at,
                )
                self.assertEqual(len(repository.list_cases()), 2)
                notification_ids = [
                    item.record.notification_id
                    for item in integration.notification_writes
                ]
                notification_ids.append(
                    outcome.digest.notification_write.record.notification_id
                )
                notifications = tuple(
                    repository.get_notification(notification_id)
                    for notification_id in notification_ids
                )
                self.assertTrue(notifications)
                self.assertTrue(
                    all(item.status == "pending" for item in notifications)
                )
                self.assertTrue(
                    all(item.attempt_count == 0 for item in notifications)
                )
                self.assertTrue(all(
                    repository.notification_attempt_history(item.notification_id)
                    == ()
                    for item in notifications
                ))

            replay = run_local_control_wakeup(
                path,
                scheduled_for=NOW,
                clock=MutableClock(NOW + timedelta(minutes=1)),
                discovery_effect=discovery_effect,
                verification_effect=verification_effect,
                catalog=self.catalog,
                policy=self.policy,
            )
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.selections, ())
            self.assertIsNone(replay.digest)
            self.assertEqual(len(discovery_effect.calls), 1)
            self.assertEqual(len(verification_effect.calls), 1)

    def test_invalid_fake_identity_leaves_planned_wakeup_ambiguous(self):
        discovery, bundle = fixture_bundle()
        discovery["venue_id"] = "aistats"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            seed_local_state(path)
            fake_discovery = FakeDiscovery(discovery)
            fake_verification = FakeVerification((bundle,))

            with self.assertRaisesRegex(
                LocalControlCompositionError, "identity"
            ):
                run_local_control_wakeup(
                    path,
                    scheduled_for=NOW,
                    clock=MutableClock(),
                    discovery_effect=fake_discovery,
                    verification_effect=fake_verification,
                    catalog=self.catalog,
                    policy=self.policy,
                )

            with ControlStateRepository(
                path,
                writer=Writer.LOCAL_CONTROL_PLANE,
                clock=MutableClock(NOW + timedelta(minutes=1)),
            ) as repository:
                self.assertEqual(
                    repository.list_scheduler_wakeups()[0].status, "active"
                )
                self.assertEqual(len(repository.list_due_work_selections()), 1)
                self.assertEqual(repository.replay_verifications(), ())
            with self.assertRaisesRegex(
                SchedulerWakeupConflictError, "ambiguously interrupted"
            ):
                run_local_control_wakeup(
                    path,
                    scheduled_for=NOW + timedelta(minutes=1),
                    clock=MutableClock(NOW + timedelta(minutes=1)),
                    discovery_effect=fake_discovery,
                    verification_effect=fake_verification,
                    catalog=self.catalog,
                    policy=self.policy,
                )
            self.assertEqual(len(fake_discovery.calls), 1)
            self.assertEqual(fake_verification.calls, [])

    def test_empty_overlimit_and_unchanged_schedule_fail_before_completion(self):
        discovery, bundle = fixture_bundle()
        scenarios = (
            ((), 16, "no deterministic evidence", due_state()),
            ((bundle, bundle), 1, "exceeded the bundle limit", due_state()),
            ((bundle, bundle), 16, "duplicate identity", due_state()),
            ((bundle,), 16, "did not advance", due_state(consumed=True)),
        )
        for bundles, limit, message, state in scenarios:
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "state.sqlite3"
                seed_local_state(path, state=state)
                with self.assertRaisesRegex(LocalControlCompositionError, message):
                    run_local_control_wakeup(
                        path,
                        scheduled_for=NOW,
                        clock=MutableClock(),
                        discovery_effect=FakeDiscovery(discovery),
                        verification_effect=FakeVerification(bundles),
                        catalog=self.catalog,
                        policy=self.policy,
                        verification_bundle_limit=limit,
                    )
                with ControlStateRepository(
                    path,
                    writer=Writer.LOCAL_CONTROL_PLANE,
                    clock=MutableClock(),
                ) as repository:
                    self.assertEqual(
                        repository.list_scheduler_wakeups()[0].status, "active"
                    )

    def test_invalid_effect_configuration_is_rejected_before_state(self):
        discovery, bundle = fixture_bundle()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self.assertRaisesRegex(
                LocalControlCompositionError, "provide discover"
            ):
                run_local_control_wakeup(
                    path,
                    scheduled_for=NOW,
                    clock=MutableClock(),
                    discovery_effect=object(),
                    verification_effect=FakeVerification((bundle,)),
                    catalog=self.catalog,
                    policy=self.policy,
                )
            self.assertFalse(path.exists())

            with self.assertRaisesRegex(
                LocalControlCompositionError, "provide verify"
            ):
                run_local_control_wakeup(
                    path,
                    scheduled_for=NOW,
                    clock=MutableClock(),
                    discovery_effect=FakeDiscovery(discovery),
                    verification_effect=object(),
                    catalog=self.catalog,
                    policy=self.policy,
                )
            self.assertFalse(path.exists())

    def test_module_has_no_live_delivery_execution_or_orchestration_import(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        source = MODULE.read_text(encoding="utf-8")
        for forbidden in (
            "prefect",
            "google",
            "resend",
            "urllib",
            "requests",
            "subprocess",
            "automation.job_queue",
            "automation.job_results",
            "automation.mac_worker",
        ):
            self.assertNotIn(forbidden, imported)
            self.assertNotIn(forbidden, source)
        self.assertNotIn("getenv", source)
        self.assertNotIn("deliver_notification", source)


if __name__ == "__main__":
    unittest.main()
