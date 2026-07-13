import ast
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.cases import derive_case_id, validate_case_state
from automation.configuration import load_policy_config
from automation.reminders import (
    ReminderCadence,
    ReminderPolicyError,
    build_case_digest,
    evaluate_case_reminder,
)


MODULE = Path(__file__).resolve().parents[1] / "reminders.py"
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def case_state(
    *,
    age_days,
    venue_id="icml",
    year=2026,
    blocker="no_pdf",
    status="open",
    last_checked_at=None,
    snoozed_until=None,
    resolution=None,
):
    meaningful = NOW - timedelta(days=age_days)
    checked = meaningful if last_checked_at is None else last_checked_at
    state = {
        "schema_version": 1,
        "case_id": derive_case_id(venue_id, year, blocker),
        "venue_id": venue_id,
        "year": year,
        "blocker": blocker,
        "status": status,
        "summary": f"Unresolved {blocker} for {venue_id} {year}.",
        "evidence_ids": [f"evidence:{venue_id}:{year}:{blocker}"],
        "first_observed_at": timestamp(meaningful),
        "last_checked_at": timestamp(checked),
        "last_meaningful_change_at": timestamp(meaningful),
        "snoozed_until": (
            timestamp(snoozed_until) if snoozed_until is not None else None
        ),
        "resolution": resolution,
    }
    validate_case_state(state)
    return state


class ReminderPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy_config()

    def test_weekly_slots_cover_exact_weeks_one_through_four(self):
        not_due = evaluate_case_reminder(
            case_state(age_days=6), self.policy, NOW
        )
        self.assertIsNone(not_due.cadence)
        self.assertIsNone(not_due.due_at)

        week_one_state = case_state(
            age_days=7,
            last_checked_at=NOW - timedelta(hours=1),
        )
        week_one = evaluate_case_reminder(week_one_state, self.policy, NOW)
        self.assertEqual(week_one.cadence, ReminderCadence.WEEKLY)
        self.assertEqual(week_one.slot, 1)
        self.assertEqual(week_one.due_at, NOW)
        self.assertEqual(week_one.state["status"], "open")

        week_four = evaluate_case_reminder(
            case_state(age_days=28), self.policy, NOW
        )
        self.assertEqual(week_four.cadence, ReminderCadence.WEEKLY)
        self.assertEqual(week_four.slot, 4)
        self.assertEqual(week_four.due_at, NOW)

    def test_monthly_slots_age_cases_to_stalled(self):
        between_slots = evaluate_case_reminder(
            case_state(age_days=29), self.policy, NOW
        )
        self.assertEqual(between_slots.state["status"], "stalled")
        self.assertTrue(between_slots.status_changed)
        self.assertIsNone(between_slots.cadence)

        month_one = evaluate_case_reminder(
            case_state(age_days=30), self.policy, NOW
        )
        self.assertEqual(month_one.cadence, ReminderCadence.MONTHLY)
        self.assertEqual(month_one.slot, 1)
        self.assertEqual(month_one.due_at, NOW)
        self.assertEqual(month_one.state["status"], "stalled")

        month_two = evaluate_case_reminder(
            case_state(age_days=83), self.policy, NOW
        )
        self.assertEqual(month_two.cadence, ReminderCadence.MONTHLY)
        self.assertEqual(month_two.slot, 2)
        self.assertEqual(month_two.due_at, NOW - timedelta(days=23))

    def test_dormant_threshold_and_quarterly_slots_are_exact(self):
        first = evaluate_case_reminder(
            case_state(age_days=84), self.policy, NOW
        )
        self.assertEqual(first.state["status"], "dormant")
        self.assertEqual(first.cadence, ReminderCadence.DORMANT)
        self.assertEqual(first.slot, 1)
        self.assertEqual(first.due_at, NOW)

        repeat = evaluate_case_reminder(
            case_state(age_days=174), self.policy, NOW
        )
        self.assertEqual(repeat.cadence, ReminderCadence.DORMANT)
        self.assertEqual(repeat.slot, 2)
        self.assertEqual(repeat.due_at, NOW)

    def test_meaningful_change_not_last_check_drives_age(self):
        state = case_state(
            age_days=7,
            last_checked_at=NOW - timedelta(minutes=1),
        )
        assessment = evaluate_case_reminder(state, self.policy, NOW)
        self.assertEqual(assessment.cadence, ReminderCadence.WEEKLY)
        self.assertEqual(assessment.age_days, 7)

        changed = deepcopy(state)
        changed["last_meaningful_change_at"] = timestamp(
            NOW - timedelta(days=1)
        )
        assessment = evaluate_case_reminder(changed, self.policy, NOW)
        self.assertIsNone(assessment.cadence)
        self.assertEqual(assessment.age_days, 1)

        changed["status"] = "stalled"
        assessment = evaluate_case_reminder(changed, self.policy, NOW)
        self.assertEqual(assessment.state["status"], "open")
        self.assertTrue(assessment.status_changed)

    def test_closed_and_snoozed_cases_are_not_due(self):
        for status in ("resolved", "ignored", "wont_fix"):
            state = case_state(
                age_days=90,
                status=status,
                resolution="Maintainer closed this case.",
            )
            assessment = evaluate_case_reminder(state, self.policy, NOW)
            self.assertIsNone(assessment.cadence)
            self.assertEqual(assessment.state["status"], status)

        snoozed = case_state(
            age_days=30,
            status="snoozed",
            snoozed_until=NOW + timedelta(seconds=1),
        )
        waiting = evaluate_case_reminder(snoozed, self.policy, NOW)
        self.assertIsNone(waiting.cadence)
        self.assertEqual(waiting.state["status"], "snoozed")

        expired = evaluate_case_reminder(
            snoozed, self.policy, NOW + timedelta(seconds=1)
        )
        self.assertEqual(expired.state["status"], "stalled")
        self.assertIsNone(expired.state["snoozed_until"])
        self.assertEqual(expired.cadence, ReminderCadence.MONTHLY)

    def test_dormant_state_is_sticky_without_explicit_reactivation(self):
        dormant = case_state(age_days=1, status="dormant")
        assessment = evaluate_case_reminder(dormant, self.policy, NOW)
        self.assertEqual(assessment.state["status"], "dormant")
        self.assertIsNone(assessment.cadence)

    def test_invalid_policy_or_clock_fails_without_mutating_input(self):
        state = case_state(age_days=7)
        original = deepcopy(state)
        with self.assertRaisesRegex(ReminderPolicyError, "timezone"):
            evaluate_case_reminder(state, self.policy, NOW.replace(tzinfo=None))
        with self.assertRaisesRegex(ReminderPolicyError, "precedes"):
            evaluate_case_reminder(state, self.policy, NOW - timedelta(days=8))

        invalid_policy = deepcopy(self.policy)
        invalid_policy["reminders"]["weekly_until_days"] = 90
        with self.assertRaisesRegex(ReminderPolicyError, "windows"):
            evaluate_case_reminder(state, invalid_policy, NOW)
        self.assertEqual(state, original)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy_config()

    def test_one_digest_groups_every_due_case_by_urgency(self):
        weekly_later_id = case_state(
            age_days=7,
            venue_id="neurips",
            blocker="no_pdf",
        )
        weekly_earlier_due = case_state(
            age_days=8,
            venue_id="icml",
            blocker="no_public_list",
        )
        monthly = case_state(
            age_days=30,
            venue_id="aistats",
            blocker="unsupported_scraper",
        )
        dormant = case_state(
            age_days=84,
            venue_id="ijcai",
            blocker="human_review_required",
        )
        not_due = case_state(age_days=1, venue_id="cvpr")
        resolved = case_state(
            age_days=90,
            venue_id="acl",
            status="resolved",
            resolution="Resolved by the maintainer.",
        )

        digest = build_case_digest(
            [monthly, not_due, dormant, weekly_later_id, resolved, weekly_earlier_due],
            self.policy,
            NOW,
        )
        self.assertEqual(digest.generated_at, NOW)
        self.assertEqual(digest.due_count, 4)
        self.assertEqual(
            [group.cadence for group in digest.groups],
            [
                ReminderCadence.WEEKLY,
                ReminderCadence.MONTHLY,
                ReminderCadence.DORMANT,
            ],
        )
        self.assertEqual(
            [item.case_id for item in digest.groups[0].items],
            [
                weekly_earlier_due["case_id"],
                weekly_later_id["case_id"],
            ],
        )
        all_ids = [
            item.case_id
            for group in digest.groups
            for item in group.items
        ]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertNotIn(not_due["case_id"], all_ids)
        self.assertNotIn(resolved["case_id"], all_ids)
        self.assertEqual(
            digest.groups[2].items[0].evidence_ids,
            tuple(dormant["evidence_ids"]),
        )

    def test_digest_replay_is_stable_and_inputs_are_defensive(self):
        states = [case_state(age_days=7), case_state(age_days=30, venue_id="uai")]
        original = deepcopy(states)
        first = build_case_digest(states, self.policy, NOW)
        second = build_case_digest(states, self.policy, NOW)
        self.assertEqual(first, second)
        self.assertEqual(states, original)
        self.assertEqual(
            build_case_digest([], self.policy, NOW).groups,
            (),
        )
        with self.assertRaisesRegex(ReminderPolicyError, "duplicate case"):
            build_case_digest([states[0], states[0]], self.policy, NOW)

    def test_module_has_no_storage_network_or_notification_transport_dependency(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        roots = {name.split(".", 1)[0] for name in imports}
        self.assertTrue(
            {
                "sqlite3",
                "requests",
                "urllib3",
                "prefect",
                "google",
                "smtplib",
                "email",
            }.isdisjoint(roots)
        )


if __name__ == "__main__":
    unittest.main()
