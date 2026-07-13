import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from automation.configuration import load_policy_config
from automation.contracts import (
    ContractName,
    ContractValidationError,
    validate_contract,
)
from automation.domain import LifecycleKind
from automation.scheduling import (
    NextCheckReason,
    compute_next_check,
    schedule_next_check,
)


FIXTURES = Path(__file__).with_name("fixtures") / "phase0"


def load_state() -> dict:
    return json.loads(
        (FIXTURES / "conference-state.v1.json").read_text(encoding="utf-8"))


def milestone(at: str, *, status: str = "verified") -> dict:
    return {
        "at": at,
        "status": status,
        "source_type": "official",
        "source_url": "https://example.test/official-schedule",
        "evidence_ids": [f"evidence:schedule:{at[:10]}"],
        "observed_at": "2026-01-01T00:00:00Z",
    }


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy_config()

    def test_unknown_schedule_uses_low_frequency_fallback(self):
        result = compute_next_check(
            load_state(), self.policy,
            datetime(2026, 1, 1, tzinfo=timezone.utc))

        self.assertEqual(result.at,
                         datetime(2026, 3, 2, tzinfo=timezone.utc))
        self.assertEqual(result.reason,
                         NextCheckReason.UNKNOWN_SCHEDULE_FALLBACK)
        self.assertIsNone(result.milestone)

    def test_expected_release_takes_priority_over_conference_date(self):
        state = load_state()
        state["milestones"]["paper_list_expected"] = milestone(
            "2026-01-10T00:00:00Z")
        state["milestones"]["conference_start"] = milestone(
            "2026-07-01T00:00:00Z")

        result = compute_next_check(
            state, self.policy,
            datetime(2026, 1, 1, tzinfo=timezone.utc))

        self.assertEqual(result.at,
                         datetime(2026, 1, 10, tzinfo=timezone.utc))
        self.assertEqual(result.reason, NextCheckReason.EXPECTED_RELEASE)
        self.assertEqual(result.milestone, "paper_list_expected")

    def test_verified_future_event_wakes_only_near_the_milestone(self):
        state = load_state()
        state["milestones"]["conference_start"] = milestone(
            "2026-07-01T00:00:00Z")

        result = compute_next_check(
            state, self.policy,
            datetime(2026, 5, 1, tzinfo=timezone.utc))

        self.assertEqual(result.at,
                         datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(result.reason,
                         NextCheckReason.BEFORE_VERIFIED_MILESTONE)
        self.assertEqual(result.milestone, "conference_start")

    def test_maximum_silence_guards_a_far_future_milestone(self):
        state = load_state()
        state["milestones"]["conference_start"] = milestone(
            "2026-12-01T00:00:00Z")

        result = compute_next_check(
            state, self.policy,
            datetime(2026, 1, 1, tzinfo=timezone.utc))

        self.assertEqual(result.at,
                         datetime(2026, 4, 1, tzinfo=timezone.utc))
        self.assertEqual(result.reason,
                         NextCheckReason.MAXIMUM_SILENCE_GUARD)

    def test_missing_post_conference_release_uses_backoff(self):
        state = load_state()
        state["milestones"]["conference_end"] = milestone(
            "2026-07-01T00:00:00Z", status="observed")

        result = compute_next_check(
            state, self.policy,
            datetime(2026, 7, 2, tzinfo=timezone.utc))

        self.assertEqual(result.at,
                         datetime(2026, 7, 4, tzinfo=timezone.utc))
        self.assertEqual(
            result.reason,
            NextCheckReason.POST_CONFERENCE_RELEASE_BACKOFF)
        self.assertEqual(result.milestone, "conference_end")

    def test_continuous_publication_ignores_conference_end(self):
        state = load_state()
        state["venue_id"] = "jmlr"
        state["milestones"]["conference_end"] = milestone(
            "2026-01-01T00:00:00Z", status="observed")

        result = compute_next_check(
            state, self.policy,
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            lifecycle_kind=LifecycleKind.CONTINUOUS)

        self.assertEqual(result.reason,
                         NextCheckReason.UNKNOWN_SCHEDULE_FALLBACK)

    def test_schedule_updates_a_copy_and_published_state_has_no_next_check(self):
        state = load_state()
        scheduled = schedule_next_check(
            state, self.policy,
            datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertIsNone(state["next_check_at"])
        self.assertEqual(scheduled["next_check_at"], "2026-03-02T00:00:00Z")
        validate_contract(ContractName.CONFERENCE_STATE, scheduled)

        scheduled["lifecycle_state"] = "published"
        completed = schedule_next_check(
            scheduled, self.policy,
            datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertIsNone(completed["next_check_at"])
        self.assertIsNone(completed["next_check_reason"])

    def test_candidate_or_timezone_free_dates_are_rejected(self):
        state = load_state()
        state["milestones"]["conference_start"] = milestone(
            "2026-07-01T00:00:00Z", status="candidate")
        with self.assertRaises(ContractValidationError):
            compute_next_check(
                state, self.policy,
                datetime(2026, 1, 1, tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValueError, "timezone"):
            compute_next_check(
                load_state(), self.policy, datetime(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
