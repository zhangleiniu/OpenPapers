import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.upgrade_safety import (
    UpgradeSafetyError,
    UpgradeStage,
    fresh_bounded_wake,
    rollback_plan,
    validate_stage_transition,
    verify_runtime,
)


COMMIT = "a" * 40
NOW = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def inventory(root: Path) -> list[tuple[str, str]]:
    return [
        (
            path.relative_to(root).as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    ]


def write_manifest(root: Path, manifest: Path) -> None:
    items = inventory(root)
    digest = hashlib.sha256(
        json.dumps(items, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "commit": COMMIT,
        "runtime_file_count": len(items),
        "runtime_sha256": digest,
    }), encoding="utf-8")


def wake(observed_at: datetime, *, status: str = "completed") -> dict[str, object]:
    scheduled_for = observed_at.replace(minute=17, second=0, microsecond=0)
    if scheduled_for > observed_at:
        scheduled_for -= timedelta(hours=1)
    return {
        "status": status,
        "code": "no_due_work" if status == "completed" else "effect_failed",
        "scheduled_for": timestamp(scheduled_for),
        "observed_at": timestamp(observed_at),
        "selection_count": 0,
        "health_ready": status == "completed",
    }


class RuntimeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        (self.runtime / "automation").mkdir(parents=True)
        (self.runtime / "automation" / "__init__.py").write_text(
            "", encoding="utf-8"
        )
        (self.runtime / "automation" / "service.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        for directory in (self.runtime, self.runtime / "automation"):
            directory.chmod(0o755)
        for path in self.runtime.rglob("*.py"):
            path.chmod(0o644)
        self.manifest = self.root / "manifest.json"
        write_manifest(self.runtime, self.manifest)

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_candidate_and_service_readable_stage_pass(self):
        result = verify_runtime(
            self.runtime, self.manifest, expected_commit=COMMIT,
            require_service_readable=True,
        )
        self.assertEqual(result.file_count, 2)
        self.assertEqual(result.commit, COMMIT)

    def test_post_manifest_mutation_and_generated_bytecode_are_rejected(self):
        (self.runtime / "automation" / "service.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(UpgradeSafetyError, "manifest"):
            verify_runtime(self.runtime, self.manifest)

        (self.runtime / "automation" / "service.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        cache = self.runtime / "automation" / "__pycache__"
        cache.mkdir()
        (cache / "service.cpython-312.pyc").write_bytes(b"generated")
        with self.assertRaisesRegex(UpgradeSafetyError, "bytecode"):
            verify_runtime(self.runtime, self.manifest)

    def test_symlink_and_non_service_readable_modes_are_rejected(self):
        link = self.runtime / "automation" / "linked.py"
        link.symlink_to(self.runtime / "automation" / "service.py")
        with self.assertRaisesRegex(UpgradeSafetyError, "symlink"):
            verify_runtime(self.runtime, self.manifest)
        link.unlink()

        target = self.runtime / "automation" / "service.py"
        target.chmod(0o600)
        with self.assertRaisesRegex(UpgradeSafetyError, "cannot read"):
            verify_runtime(
                self.runtime, self.manifest, require_service_readable=True,
            )


class FreshRecordTests(unittest.TestCase):
    def test_capped_history_accepts_fresh_latest_record_without_growth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.json"
            records = [
                wake(NOW - timedelta(minutes=128 - index))
                for index in range(128)
            ]
            records = [*records[1:], wake(NOW + timedelta(minutes=1))]
            self.assertEqual(len(records), 128)
            path.write_text(json.dumps({
                "schema_version": 1, "records": records,
            }), encoding="utf-8")
            result = fresh_bounded_wake(
                path, started_at=timestamp(NOW),
                checked_at=timestamp(NOW + timedelta(minutes=2)),
            )
            self.assertEqual(result.observed_at, timestamp(NOW + timedelta(minutes=1)))
            self.assertEqual(result.code, "no_due_work")

    def test_stale_failed_and_noncanonical_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "records": [wake(NOW - timedelta(minutes=1))],
            }), encoding="utf-8")
            with self.assertRaisesRegex(UpgradeSafetyError, "no fresh"):
                fresh_bounded_wake(path, started_at=timestamp(NOW))

            failed = wake(NOW + timedelta(minutes=1), status="failed")
            path.write_text(json.dumps({
                "schema_version": 1, "records": [failed],
            }), encoding="utf-8")
            with self.assertRaisesRegex(UpgradeSafetyError, "healthily"):
                fresh_bounded_wake(path, started_at=timestamp(NOW))

            failed["observed_at"] = "+002026-07-18T14:01:00Z"
            path.write_text(json.dumps({
                "schema_version": 1, "records": [failed],
            }), encoding="utf-8")
            with self.assertRaises(UpgradeSafetyError):
                fresh_bounded_wake(path, started_at=timestamp(NOW))

            future = wake(NOW + timedelta(minutes=2))
            path.write_text(json.dumps({
                "schema_version": 1, "records": [future],
            }), encoding="utf-8")
            with self.assertRaisesRegex(UpgradeSafetyError, "future"):
                fresh_bounded_wake(
                    path, started_at=timestamp(NOW),
                    checked_at=timestamp(NOW + timedelta(minutes=1)),
                )


class RollbackPhaseTests(unittest.TestCase):
    def test_every_phase_has_a_valid_ordered_recovery_plan(self):
        for stage in UpgradeStage:
            with self.subTest(stage=stage.name):
                plan = rollback_plan(
                    stage, backup_exists=stage >= UpgradeStage.BACKUP_READY,
                )
                self.assertEqual(plan[-1], (
                    "restart_original_services"
                    if stage >= UpgradeStage.SERVICES_STOPPED
                    else "quarantine_uninstalled_candidates"
                ))
                if stage < UpgradeStage.BACKUP_READY:
                    self.assertNotIn("restore_state_and_records", plan)
                else:
                    self.assertIn("restore_state_and_records", plan)

    def test_missing_backup_and_nonconsecutive_transition_fail_closed(self):
        with self.assertRaisesRegex(UpgradeSafetyError, "requires an exact backup"):
            rollback_plan(UpgradeStage.RUNTIME_SWAPPED, backup_exists=False)
        validate_stage_transition(
            UpgradeStage.PREFLIGHT, UpgradeStage.SERVICES_STOPPED,
        )
        with self.assertRaisesRegex(UpgradeSafetyError, "exactly once"):
            validate_stage_transition(
                UpgradeStage.PREFLIGHT, UpgradeStage.BACKUP_READY,
            )
        with self.assertRaisesRegex(UpgradeSafetyError, "exactly once"):
            validate_stage_transition(
                UpgradeStage.BACKUP_READY, UpgradeStage.SERVICES_STOPPED,
            )


if __name__ == "__main__":
    unittest.main()
