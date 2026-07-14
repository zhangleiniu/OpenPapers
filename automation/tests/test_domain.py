import json
import unittest
from copy import deepcopy
from pathlib import Path

from automation.domain import (
    ActionType,
    ArtifactKind,
    DuplicateJobResultError,
    EvidenceReplayConflictError,
    InvalidTransitionError,
    JobResultRegistry,
    LifecycleKind,
    LifecycleState,
    OwnershipError,
    SecretBoundaryError,
    TransitionActor,
    TransitionRequest,
    Writer,
    allowed_transitions,
    apply_transition,
    assert_secret_free,
    assert_writer_allowed,
)


FIXTURES = Path(__file__).with_name("fixtures") / "phase0"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def scheduled_request() -> TransitionRequest:
    return TransitionRequest(
        transition_id="transition:icml:2026:scheduled",
        to_state=LifecycleState.SCHEDULED,
        evidence_ids=("evidence:icml:2026:schedule",),
        reason="The official schedule identifies the 2026 conference.",
        actor=TransitionActor.DETERMINISTIC_VERIFIER,
        at="2026-07-13T13:30:00Z",
    )


class TransitionTests(unittest.TestCase):
    def test_transition_is_pure_and_evidence_replay_is_idempotent(self):
        original = load_fixture("conference-state.v1.json")
        before = deepcopy(original)

        first = apply_transition(original, scheduled_request())
        replay = apply_transition(first.state, scheduled_request())

        self.assertTrue(first.applied)
        self.assertFalse(replay.applied)
        self.assertEqual(original, before)
        self.assertEqual(replay.state, first.state)
        self.assertEqual(first.state["lifecycle_state"], "scheduled")
        self.assertEqual(first.state["evidence_ids"],
                         ["evidence:icml:2026:schedule"])

    def test_invalid_skip_and_unauthorized_actor_are_rejected(self):
        state = load_fixture("conference-state.v1.json")
        skip = TransitionRequest(
            transition_id="transition:invalid:skip",
            to_state=LifecycleState.INGESTING,
            evidence_ids=("evidence:invalid:skip",),
            reason="A fixture must not skip verification and queueing.",
            actor=TransitionActor.DETERMINISTIC_VERIFIER,
            at="2026-07-13T13:30:00Z",
        )
        with self.assertRaisesRegex(InvalidTransitionError, "not allowed"):
            apply_transition(state, skip)

        untrusted = TransitionRequest(
            transition_id="transition:invalid:llm",
            to_state=LifecycleState.SCHEDULED,
            evidence_ids=("evidence:invalid:llm",),
            reason="LLM discovery is not transition authority.",
            actor="llm_discovery",
            at="2026-07-13T13:30:00Z",
        )
        with self.assertRaisesRegex(InvalidTransitionError, "cannot authorize"):
            apply_transition(state, untrusted)

    def test_same_evidence_cannot_change_meaning(self):
        first = apply_transition(
            load_fixture("conference-state.v1.json"), scheduled_request())
        conflict = TransitionRequest(
            transition_id="transition:icml:2026:list",
            to_state=LifecycleState.PAPER_LIST_RELEASED,
            evidence_ids=("evidence:icml:2026:schedule",),
            reason="The same evidence is being reused for another fact.",
            actor=TransitionActor.DETERMINISTIC_VERIFIER,
            at="2026-07-13T13:35:00Z",
        )
        with self.assertRaisesRegex(EvidenceReplayConflictError,
                                    "same evidence"):
            apply_transition(first.state, conflict)

    def test_continuous_publication_cannot_end_a_conference(self):
        request = TransitionRequest(
            transition_id="transition:jmlr:2026:ended",
            to_state=LifecycleState.CONFERENCE_ENDED,
            evidence_ids=("evidence:jmlr:2026:volume",),
            reason="This transition is invalid for continuous publication.",
            actor=TransitionActor.DETERMINISTIC_VERIFIER,
            at="2026-07-13T13:30:00Z",
        )
        state = load_fixture("conference-state.v1.json")
        state["venue_id"] = "jmlr"
        with self.assertRaisesRegex(InvalidTransitionError, "continuous"):
            apply_transition(
                state, request, lifecycle_kind=LifecycleKind.CONTINUOUS)

    def test_transition_table_has_no_shell_or_effectful_values(self):
        self.assertIn(LifecycleState.SCHEDULED,
                      allowed_transitions(LifecycleState.UNKNOWN))
        self.assertEqual(
            {action.value for action in ActionType},
            {
                "recheck_at",
                "notify_transition",
                "create_or_update_case",
                "queue_existing_scraper",
                "queue_codex_diagnosis",
                "request_human_review",
                "prepare_promotion_candidate",
            },
        )


class ResultAndBoundaryTests(unittest.TestCase):
    def test_job_results_are_write_once_and_identical_replay_is_safe(self):
        result = load_fixture("job-result.v1.json")
        registry = JobResultRegistry()

        self.assertTrue(registry.accept(result))
        self.assertFalse(registry.accept(deepcopy(result)))
        result["metrics"]["paper_count"] = 101
        with self.assertRaisesRegex(DuplicateJobResultError, "different"):
            registry.accept(result)

    def test_registry_returns_a_defensive_copy(self):
        result = load_fixture("job-result.v1.json")
        registry = JobResultRegistry()
        registry.accept(result)
        loaded = registry.get(result["job_id"])
        loaded["metrics"]["paper_count"] = 0
        self.assertEqual(
            registry.get(result["job_id"])["metrics"]["paper_count"], 100)

    def test_storage_ownership_enforces_the_single_writer_boundary(self):
        assert_writer_allowed(
            Writer.CLOUD_CONTROL_PLANE, ArtifactKind.CONTROL_STATE)
        assert_writer_allowed(
            Writer.LOCAL_CONTROL_PLANE, ArtifactKind.CONTROL_STATE)
        assert_writer_allowed(
            Writer.CLOUD_CONTROL_PLANE, ArtifactKind.VERIFICATION_RESULT)
        assert_writer_allowed(Writer.MAC_WORKER, ArtifactKind.JOB_RESULT)
        with self.assertRaisesRegex(OwnershipError, "cannot write"):
            assert_writer_allowed(Writer.MAC_WORKER, ArtifactKind.CONTROL_STATE)
        with self.assertRaisesRegex(OwnershipError, "cannot write"):
            assert_writer_allowed(
                Writer.LOCAL_CONTROL_PLANE, ArtifactKind.VERIFICATION_RESULT)
        with self.assertRaisesRegex(OwnershipError, "cannot write"):
            assert_writer_allowed(
                Writer.CLOUD_CONTROL_PLANE, ArtifactKind.JOB_RESULT)
        with self.assertRaisesRegex(OwnershipError, "cannot write"):
            assert_writer_allowed(
                Writer.MAC_WORKER, ArtifactKind.VERIFICATION_RESULT)

    def test_credential_shaped_fields_are_rejected_recursively(self):
        assert_secret_free({
            "provider": "fixture",
            "token_budget": 100,
            "nested": [{"value": 1}],
        })
        with self.assertRaisesRegex(SecretBoundaryError, "auth_token"):
            assert_secret_free({"payload": {"auth_token": "not-a-real-secret"}})
        with self.assertRaisesRegex(SecretBoundaryError, "OPENREVIEW_PASSWORD"):
            assert_secret_free({"OPENREVIEW_PASSWORD": "not-a-real-secret"})


if __name__ == "__main__":
    unittest.main()
