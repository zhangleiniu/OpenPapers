import ast
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.cases import (
    CaseControl,
    CaseControlRequest,
    CaseDomainError,
    CaseObservation,
    control_case,
    observe_case,
    validate_case_state,
)
from automation.control_state import (
    CaseEventConflictError,
    ControlStateError,
    ControlStateRepository,
    LeaseHandle,
    LeaseLostError,
    StoredDataError,
)


FIXTURES = Path(__file__).with_name("fixtures")
MODULE = Path(__file__).resolve().parents[1] / "cases.py"
NOW = datetime(2026, 7, 13, 22, 30, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, *, seconds):
        self.value += timedelta(seconds=seconds)


def case_fixture():
    return json.loads(
        (FIXTURES / "phase0" / "case-state.v1.json").read_text(encoding="utf-8")
    )


def observation(
    event_id="case-event:icml:2026:no-pdf:1",
    *,
    at="2026-07-13T22:30:00Z",
    blocker="no_pdf",
    summary="The accepted list is public but archival PDFs are unavailable.",
    evidence_ids=("verification:icml:2026:list",),
):
    return CaseObservation(
        event_id=event_id,
        venue_id="icml",
        year=2026,
        blocker=blocker,
        summary=summary,
        evidence_ids=evidence_ids,
        observed_at=at,
    )


class CaseDomainTests(unittest.TestCase):
    def test_observations_deduplicate_and_track_meaningful_change(self):
        created = observe_case(None, observation())
        self.assertTrue(created.changed)
        self.assertTrue(created.meaningful_change)
        self.assertEqual(created.state["case_id"], "case:icml:2026:no-pdf")
        self.assertEqual(created.state["status"], "open")

        checked = observe_case(
            created.state,
            observation(
                "case-event:icml:2026:no-pdf:2",
                at="2026-07-14T22:30:00Z",
            ),
        )
        self.assertTrue(checked.changed)
        self.assertFalse(checked.meaningful_change)
        self.assertEqual(checked.state["last_checked_at"], "2026-07-14T22:30:00Z")
        self.assertEqual(
            checked.state["last_meaningful_change_at"], "2026-07-13T22:30:00Z"
        )

        changed = observe_case(
            checked.state,
            observation(
                "case-event:icml:2026:no-pdf:3",
                at="2026-07-15T22:30:00Z",
                evidence_ids=(
                    "verification:icml:2026:list",
                    "verification:icml:2026:pdf",
                ),
            ),
        )
        self.assertTrue(changed.meaningful_change)
        self.assertEqual(
            changed.state["last_meaningful_change_at"], "2026-07-15T22:30:00Z"
        )
        self.assertEqual(
            changed.state["evidence_ids"],
            ["verification:icml:2026:list", "verification:icml:2026:pdf"],
        )

    def test_new_evidence_reactivates_only_dormant_cases(self):
        dormant = case_fixture()
        dormant["status"] = "dormant"
        dormant["last_checked_at"] = "2026-07-13T21:00:00Z"
        dormant["last_meaningful_change_at"] = "2026-07-13T21:00:00Z"
        dormant["summary"] = "PDFs remain unavailable."
        validate_case_state(dormant)
        reactivated = observe_case(
            dormant,
            observation(
                at="2026-07-13T22:30:00Z",
                evidence_ids=(
                    "evidence:icml:2026:list",
                    "verification:icml:2026:new",
                ),
            ),
        )
        self.assertTrue(reactivated.reactivated)
        self.assertEqual(reactivated.state["status"], "open")

        ignored = deepcopy(reactivated.state)
        ignored["status"] = "ignored"
        ignored["resolution"] = "Maintainer chose not to track this source."
        validate_case_state(ignored)
        still_ignored = observe_case(
            ignored,
            observation(
                "case-event:icml:2026:no-pdf:closed",
                at="2026-07-14T22:30:00Z",
                evidence_ids=(*ignored["evidence_ids"], "verification:newer"),
            ),
        )
        self.assertFalse(still_ignored.reactivated)
        self.assertEqual(still_ignored.state["status"], "ignored")
        self.assertIn("verification:newer", still_ignored.state["evidence_ids"])

    def test_controls_enforce_state_and_time_semantics(self):
        state = observe_case(None, observation()).state
        snoozed = control_case(
            state,
            CaseControlRequest(
                event_id="case-control:icml:2026:snooze",
                action=CaseControl.SNOOZE,
                at="2026-07-14T22:30:00Z",
                reason="Wait for the archival release.",
                snoozed_until="2026-07-21T22:30:00Z",
            ),
        )
        self.assertEqual(snoozed.state["status"], "snoozed")
        self.assertEqual(snoozed.state["snoozed_until"], "2026-07-21T22:30:00Z")
        self.assertIsNone(snoozed.state["resolution"])

        observed_after_expiry = observe_case(
            snoozed.state,
            observation(
                "case-event:icml:2026:no-pdf:after-snooze",
                at="2026-07-22T22:30:00Z",
                evidence_ids=(
                    "verification:icml:2026:list",
                    "verification:icml:2026:after-snooze",
                ),
            ),
        )
        self.assertTrue(observed_after_expiry.meaningful_change)
        self.assertEqual(observed_after_expiry.state["status"], "snoozed")

        active = control_case(
            snoozed.state,
            CaseControlRequest(
                event_id="case-control:icml:2026:reactivate",
                action="reactivate",
                at="2026-07-15T22:30:00Z",
                reason="A new source needs review.",
            ),
        )
        self.assertTrue(active.reactivated)
        self.assertEqual(active.state["status"], "open")
        self.assertIsNone(active.state["snoozed_until"])

        resolved = control_case(
            active.state,
            CaseControlRequest(
                event_id="case-control:icml:2026:resolve",
                action="resolve",
                at="2026-07-16T22:30:00Z",
                reason="Archival PDFs are now available.",
            ),
        )
        self.assertEqual(resolved.state["status"], "resolved")
        self.assertEqual(resolved.state["resolution"], "Archival PDFs are now available.")

        with self.assertRaisesRegex(CaseDomainError, "active case"):
            control_case(
                resolved.state,
                CaseControlRequest(
                    event_id="case-control:icml:2026:resolve-again",
                    action="resolve",
                    at="2026-07-17T22:30:00Z",
                    reason="Duplicate closure.",
                ),
            )
        with self.assertRaisesRegex(CaseDomainError, "future"):
            control_case(
                state,
                CaseControlRequest(
                    event_id="case-control:icml:2026:bad-snooze",
                    action="snooze",
                    at="2026-07-14T22:30:00Z",
                    snoozed_until="2026-07-14T22:30:00Z",
                ),
            )
        with self.assertRaisesRegex(CaseDomainError, "regress"):
            observe_case(
                state,
                observation(
                    "case-event:icml:2026:no-pdf:old",
                    at="2026-07-12T22:30:00Z",
                ),
            )

    def test_module_has_no_storage_network_or_notification_dependency(self):
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
            {"sqlite3", "requests", "urllib3", "prefect", "google", "smtplib"}.isdisjoint(
                roots
            )
        )


class PersistentCaseTests(unittest.TestCase):
    def test_cases_survive_reopen_and_terminal_cases_leave_default_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            clock = MutableClock()
            with ControlStateRepository(path, clock=clock) as store:
                lease = store.acquire_lease("case-flow")
                first = store.observe_case(observation(), lease=lease)
                replay = store.observe_case(observation(), lease=lease)
                checked = store.observe_case(
                    observation(
                        "case-event:icml:2026:no-pdf:2",
                        at="2026-07-14T22:30:00Z",
                    ),
                    lease=lease,
                )
                second = store.observe_case(
                    observation(
                        "case-event:icml:2026:no-list:1",
                        blocker="no_public_list",
                        summary="No authoritative public list is available.",
                    ),
                    lease=lease,
                )
                self.assertTrue(first.applied)
                self.assertFalse(replay.applied)
                self.assertTrue(replay.replayed)
                self.assertTrue(checked.applied)
                self.assertFalse(checked.event.meaningful_change)
                self.assertNotEqual(first.record.case_id, second.record.case_id)
                self.assertEqual(len(store.case_history(first.record.case_id)), 2)
                self.assertEqual(len(store.case_event_history(first.record.case_id)), 2)

                clock.advance(seconds=1)
                resolved = store.control_case(
                    first.record.case_id,
                    CaseControlRequest(
                        event_id="case-control:icml:2026:resolve",
                        action="resolve",
                        at="2026-07-15T22:30:00Z",
                        reason="The archive is complete.",
                    ),
                    lease=lease,
                )
                self.assertTrue(resolved.applied)
                self.assertEqual(resolved.record.state["status"], "resolved")
                self.assertEqual(
                    [item.case_id for item in store.list_cases()],
                    [second.record.case_id],
                )
                self.assertEqual(len(store.list_cases(include_closed=True)), 2)

            with ControlStateRepository(path) as reopened:
                current = reopened.get_case(first.record.case_id)
                self.assertEqual(current.state["status"], "resolved")
                self.assertEqual(len(reopened.case_history(first.record.case_id)), 3)
                current.state["summary"] = "caller mutation"
                self.assertNotEqual(
                    reopened.get_case(first.record.case_id).state["summary"],
                    "caller mutation",
                )

    def test_controls_and_event_conflicts_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=clock
            ) as store:
                lease = store.acquire_lease("case-flow")
                created = store.observe_case(observation(), lease=lease)
                snooze = CaseControlRequest(
                    event_id="case-control:icml:2026:snooze",
                    action="snooze",
                    at="2026-07-14T22:30:00Z",
                    reason="Wait one week.",
                    snoozed_until="2026-07-21T22:30:00Z",
                )
                first = store.control_case(created.record.case_id, snooze, lease=lease)
                replay = store.control_case(created.record.case_id, snooze, lease=lease)
                self.assertTrue(first.applied)
                self.assertTrue(replay.replayed)
                self.assertFalse(replay.applied)
                self.assertEqual(len(store.case_event_history(created.record.case_id)), 2)

                conflicting = CaseControlRequest(
                    event_id=snooze.event_id,
                    action="ignore",
                    at=snooze.at,
                    reason="Changed meaning.",
                )
                with self.assertRaises(CaseEventConflictError):
                    store.control_case(
                        created.record.case_id, conflicting, lease=lease
                    )
                self.assertEqual(store.get_case(created.record.case_id).state["status"], "snoozed")

                reactivated = store.control_case(
                    created.record.case_id,
                    CaseControlRequest(
                        event_id="case-control:icml:2026:reactivate",
                        action="reactivate",
                        at="2026-07-15T22:30:00Z",
                        reason="Resume active review.",
                    ),
                    lease=lease,
                )
                ignored = store.control_case(
                    created.record.case_id,
                    CaseControlRequest(
                        event_id="case-control:icml:2026:ignore",
                        action="ignore",
                        at="2026-07-16T22:30:00Z",
                        reason="The maintainer accepts this unresolved state.",
                    ),
                    lease=lease,
                )
                self.assertTrue(reactivated.event.reactivated)
                self.assertEqual(ignored.record.state["status"], "ignored")
                self.assertEqual(len(store.case_event_history(created.record.case_id)), 4)

    def test_case_mutation_requires_the_live_repository_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                with self.assertRaises(LeaseLostError):
                    store.observe_case(
                        observation(),
                        lease=LeaseHandle(
                            "missing", "not-current", "2099-01-01T00:00:00Z"
                        ),
                    )
                self.assertEqual(store.list_cases(include_closed=True), ())

    def test_compound_write_rollback_and_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with ControlStateRepository(
                Path(directory) / "state.sqlite3", clock=MutableClock()
            ) as store:
                lease = store.acquire_lease("case-flow")
                store._connection.execute("""
                    CREATE TRIGGER reject_case_event
                    BEFORE INSERT ON case_event_history
                    BEGIN
                        SELECT RAISE(ABORT, 'forced case rollback');
                    END
                """)
                with self.assertRaisesRegex(ControlStateError, "transaction failed"):
                    store.observe_case(observation(), lease=lease)
                self.assertEqual(store.list_cases(include_closed=True), ())
                count = store._connection.execute(
                    "SELECT COUNT(*) FROM case_state_history"
                ).fetchone()[0]
                self.assertEqual(count, 0)

                store._connection.execute("DROP TRIGGER reject_case_event")
                created = store.observe_case(observation(), lease=lease)
                event_fingerprint = created.event.event_fingerprint
                store._connection.execute(
                    "UPDATE case_event_history SET event_fingerprint = ?",
                    ("f" * 64,),
                )
                with self.assertRaisesRegex(StoredDataError, "event fingerprint"):
                    store.case_event_history(created.record.case_id)
                store._connection.execute(
                    "UPDATE case_event_history SET event_fingerprint = ?",
                    (event_fingerprint,),
                )
                store._connection.execute(
                    "UPDATE case_state_current SET state_fingerprint = ?",
                    ("0" * 64,),
                )
                with self.assertRaisesRegex(StoredDataError, "fingerprint"):
                    store.get_case(created.record.case_id)


if __name__ == "__main__":
    unittest.main()
