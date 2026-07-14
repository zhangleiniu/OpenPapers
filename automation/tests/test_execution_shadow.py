import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from automation.execution_pipeline import P5ExecutionStatus, run_existing_scraper_pipeline
from automation.execution_shadow import (
    ExecutionShadowConfig,
    ExecutionShadowError,
    LocalImmutableResultStore,
    SandboxedSubprocessLauncher,
    build_pipeline_config,
    build_shadow_job,
    prepare_shadow_root,
    retain_sandbox_profile,
)
from automation.job_results import (
    ImmutableObjectConflictError,
    build_job_manifest,
    build_job_result,
)
from automation.staging_executor import StagedProcessRequest


MODULE = Path(__file__).resolve().parents[1] / "execution_shadow.py"


class NeverCancelled:
    def is_cancelled(self):
        return False


class NoStartLauncher:
    def start(self, request):
        raise AssertionError("disk gate must prevent process start")


class NoPublishStore:
    def publish(self, job, manifest, result):
        raise AssertionError("disk gate must prevent result publication")


class ShadowFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.canonical = self.repository / "data"
        self.shadow = self.root / "shadow"
        self.runtime = self.root / "runtime"
        self.repository.mkdir(mode=0o700)
        self.canonical.mkdir(mode=0o770)
        self.runtime.mkdir(mode=0o700)
        (self.repository / "main.py").write_text("# fixture\n", encoding="utf-8")
        self.python = self.runtime / "python"
        self.python.write_bytes(Path(sys.executable).read_bytes())
        self.python.chmod(0o700)
        self.config = ExecutionShadowConfig(
            repository_root=self.repository,
            python_executable=self.python,
            canonical_data_root=self.canonical,
            shadow_root=self.shadow,
            timeout_seconds=60,
            minimum_free_bytes=100,
            minimum_free_fraction=0.01,
        )

    def prepare(self):
        prepare_shadow_root(
            self.config, venue_id="colt", year=2025, expected_count=181
        )

    def close(self):
        self.temporary.cleanup()


class ExecutionShadowTests(unittest.TestCase):
    def fixture(self):
        fixture = ShadowFixture()
        self.addCleanup(fixture.close)
        return fixture

    def test_marked_private_root_is_exact_replay_and_pipeline_roots_are_disjoint(self):
        fixture = self.fixture()
        fixture.prepare()
        fixture.prepare()
        marker = json.loads(
            (fixture.shadow / ".p5s-existing-scraper-shadow.v1.json").read_text()
        )
        self.assertEqual(marker["venue_id"], "colt")
        self.assertEqual(marker["expected_count"], 181)
        pipeline = build_pipeline_config(fixture.config)
        self.assertEqual(pipeline.worker_safety.data_root, fixture.shadow / "staging")
        self.assertEqual(
            pipeline.staging_validation.artifact_root, fixture.shadow / "artifacts"
        )
        self.assertEqual(
            pipeline.staging_executor.canonical_data_root, fixture.canonical
        )
        job = build_shadow_job(venue_id="colt", year=2025, expected_count=181)
        observation = run_existing_scraper_pipeline(
            job,
            pipeline,
            NoStartLauncher(),
            NoPublishStore(),
            disk_usage=lambda _: SimpleNamespace(total=1000, used=950, free=50),
        )
        self.assertEqual(observation.status, P5ExecutionStatus.RETRY)

    def test_overlapping_unsafe_or_conflicting_roots_fail_closed(self):
        fixture = self.fixture()
        overlap = ExecutionShadowConfig(
            repository_root=fixture.repository,
            python_executable=fixture.python,
            canonical_data_root=fixture.canonical,
            shadow_root=fixture.repository / "shadow",
            timeout_seconds=60,
        )
        with self.assertRaises(ExecutionShadowError):
            prepare_shadow_root(overlap, venue_id="colt", year=2025, expected_count=181)
        fixture.shadow.mkdir(mode=0o700)
        (fixture.shadow / "foreign").mkdir(mode=0o700)
        with self.assertRaises(ExecutionShadowError):
            prepare_shadow_root(
                fixture.config, venue_id="colt", year=2025, expected_count=181
            )
        (fixture.shadow / "foreign").rmdir()
        fixture.prepare()
        with self.assertRaises(ImmutableObjectConflictError):
            prepare_shadow_root(
                fixture.config, venue_id="colt", year=2025, expected_count=180
            )

    def test_shadow_job_is_stable_archival_and_has_no_execution_fields(self):
        first = build_shadow_job(venue_id="colt", year=2025, expected_count=181)
        second = build_shadow_job(venue_id="colt", year=2025, expected_count=181)
        self.assertEqual(first, second)
        self.assertEqual(first["payload"]["completeness_level"], "archival")
        self.assertTrue(first["payload"]["download_pdfs"])
        self.assertEqual(first["payload"]["expected_count"], 181)
        self.assertFalse({"command", "argv", "path", "environment"} & set(first))

    @unittest.skipUnless(sys.platform == "darwin", "macOS sandbox boundary")
    def test_sandbox_allows_only_shadow_and_denies_other_writes(self):
        fixture = self.fixture()
        fixture.prepare()
        profile = retain_sandbox_profile(fixture.config)
        launcher = SandboxedSubprocessLauncher(
            profile, sandbox_executable=Path("/usr/bin/sandbox-exec")
        )
        staging_file = fixture.shadow / "staging" / "allowed"
        guarded_files = (
            fixture.canonical / "denied",
            fixture.repository / "denied",
            fixture.root / "outside-shadow-denied",
        )

        def request(target, log_name):
            return StagedProcessRequest(
                job_id="job:" + "a" * 64,
                argv=("/usr/bin/touch", str(target)),
                cwd=fixture.root,
                environment_items=(("PATH", "/usr/bin"),),
                data_root=fixture.shadow / "staging",
                log_path=fixture.shadow / "sandbox" / log_name,
            )

        allowed = launcher.start(request(staging_file, "allowed.log"))
        self.assertEqual(
            allowed.wait(timeout_seconds=10, cancellation=NeverCancelled()), 0
        )
        self.assertTrue(staging_file.exists())
        for index, target in enumerate(guarded_files):
            denied = launcher.start(request(target, f"denied-{index}.log"))
            exit_code = denied.wait(timeout_seconds=10, cancellation=NeverCancelled())
            self.assertIsNotNone(exit_code)
            self.assertNotEqual(exit_code, 0)
            self.assertFalse(target.exists())

    def test_local_result_store_is_create_only_and_exactly_replayable(self):
        fixture = self.fixture()
        fixture.prepare()
        job = build_shadow_job(venue_id="colt", year=2025, expected_count=181)
        artifact = {
            "artifact_id": "validation:" + "b" * 64,
            "artifact_kind": "validation_report",
            "object_name": "validations/report.v1.json",
            "content_fingerprint": "c" * 64,
            "size_bytes": 10,
        }
        manifest = build_job_manifest(
            job, created_at="2026-07-14T21:00:00Z", artifacts=[artifact]
        )
        result = build_job_result(
            job,
            manifest,
            worker_id="worker:mac-mini:p5s-shadow",
            completed_at="2026-07-14T21:00:01Z",
            status="succeeded",
            error_code=None,
            error_summary=None,
            duration_seconds=1.0,
            paper_count=181,
            valid_pdf_count=181,
        )
        store = LocalImmutableResultStore(fixture.shadow / "results")
        first = store.publish(job, manifest, result)
        second = store.publish(job, manifest, result)
        self.assertEqual(first, second)
        self.assertEqual(first.manifest_generation, 1)
        self.assertEqual(first.result_generation, 1)
        changed = dict(result)
        changed["completed_at"] = datetime.now(timezone.utc).isoformat()
        with self.assertRaises(ValueError):
            store.publish(job, manifest, changed)

    def test_module_has_no_scheduler_cloud_promotion_or_canonical_writer(self):
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("local_service", source)
        self.assertNotIn("gcloud", source)
        self.assertNotIn("statistics", source)
        self.assertNotIn("mustcite", source.lower())
        self.assertNotIn("codex", source.lower())
        self.assertNotIn("shell=True", source)
