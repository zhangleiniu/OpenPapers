import ast
import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation.job_queue import JobType, build_job
from automation.job_results import validate_job_manifest
from automation.staging_executor import (
    StagingCheckpoint,
    StagingCheckpointStatus,
    StagingCheckpointStore,
    StagingReason,
)
from automation.staging_validation import (
    CandidateBundle,
    StagingArtifactConflictError,
    StagingValidationConfig,
    StagingValidationError,
    capture_staging_candidate,
    validate_staging_candidate,
)


MODULE = Path(__file__).resolve().parents[1] / "staging_validation.py"
BASE_TIME = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)


def build_scrape_job(
    *,
    suffix="scrape",
    level="archival",
    download_pdfs=True,
    expected_count=1,
    venue_id="icml",
    year=2026,
):
    return build_job(
        request_id=f"request:{venue_id}:{year}:{suffix}",
        job_type=JobType.SCRAPE_EXISTING,
        venue_id=venue_id,
        year=year,
        requested_by="human",
        input_artifact_ids=(f"evidence:{venue_id}:{year}:{suffix}",),
        payload={
            "completeness_level": level,
            "download_pdfs": download_pdfs,
            "expected_count": expected_count,
        },
    )


def build_validation_job(scrape_job, manifest_id, *, suffix="validation", **overrides):
    payload = {
        "candidate_manifest_id": manifest_id,
        "completeness_level": scrape_job["payload"]["completeness_level"],
        "require_pdfs": (
            scrape_job["payload"]["download_pdfs"]
            or scrape_job["payload"]["completeness_level"] == "archival"
        ),
        "expected_count": scrape_job["payload"]["expected_count"],
    }
    payload.update(overrides)
    return build_job(
        request_id=f"request:{scrape_job['venue_id']}:{scrape_job['year']}:{suffix}",
        job_type=JobType.VALIDATE_CANDIDATE,
        venue_id=scrape_job["venue_id"],
        year=scrape_job["year"],
        requested_by="human",
        input_artifact_ids=(manifest_id,),
        payload=payload,
    )


def paper(*, paper_id="paper-1", pdf_path="papers/icml/2026/paper-1.pdf"):
    return {
        "id": paper_id,
        "title": "Fixture title",
        "authors": ["Fixture Author"],
        "abstract": "Fixture abstract",
        "year": 2026,
        "conference": "icml",
        "url": "https://example.invalid/paper-1",
        "bibtex": "@inproceedings{paper-1}",
        "pdf_url": "https://example.invalid/paper-1.pdf",
        "pdf_path": pdf_path,
    }


class TickClock:
    def __init__(self, value=BASE_TIME):
        self.value = value

    def __call__(self):
        value = self.value
        self.value += timedelta(seconds=1)
        return value


class FixtureStaging:
    def __init__(self, test_case, *, job=None, papers=None):
        self.test_case = test_case
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.staging_root = self.root / "staging"
        self.artifact_root = self.root / "artifacts"
        self.canonical_root = self.root / "canonical"
        for path in (self.staging_root, self.artifact_root, self.canonical_root):
            path.mkdir(mode=0o700)
        self.job = job or build_scrape_job()
        self.job_root = self.staging_root / self.job["job_fingerprint"]
        self.data_root = self.job_root / "data"
        (self.job_root / "logs").mkdir(parents=True, mode=0o700)
        self.data_root.mkdir(mode=0o700)
        self.config = StagingValidationConfig(
            staging_root=self.staging_root,
            artifact_root=self.artifact_root,
            canonical_data_root=self.canonical_root,
        )
        self.write_papers(papers if papers is not None else [paper()])
        self.succeed_checkpoint()

    def close(self):
        self.temporary.cleanup()

    def write_papers(self, papers):
        metadata = (
            self.data_root
            / "metadata"
            / self.job["venue_id"]
            / f"{self.job['venue_id']}_{self.job['year']}.json"
        )
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(json.dumps(papers), encoding="utf-8")
        for item in papers:
            pdf_path = item.get("pdf_path")
            if not isinstance(pdf_path, str) or not pdf_path or ".." in pdf_path:
                continue
            relative = pdf_path[5:] if pdf_path.startswith("data/") else pdf_path
            path = self.data_root / relative
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.7\n" + b"x" * 2048)

    def succeed_checkpoint(self):
        store = StagingCheckpointStore(self.job_root, self.job)
        prepared = StagingCheckpoint(
            job_id=self.job["job_id"],
            job_fingerprint=self.job["job_fingerprint"],
            job_type=self.job["job_type"],
            venue_id=self.job["venue_id"],
            year=self.job["year"],
            attempt=0,
            status=StagingCheckpointStatus.PREPARED,
            reason_code=StagingReason.READY,
            updated_at="2026-07-14T18:59:58Z",
        )
        running = StagingCheckpoint(
            **{
                **prepared.__dict__,
                "attempt": 1,
                "status": StagingCheckpointStatus.RUNNING,
                "reason_code": StagingReason.PROCESS_STARTED,
                "updated_at": "2026-07-14T18:59:59Z",
            }
        )
        succeeded = StagingCheckpoint(
            **{
                **running.__dict__,
                "status": StagingCheckpointStatus.PROCESS_SUCCEEDED,
                "reason_code": StagingReason.EXIT_ZERO,
                "updated_at": "2026-07-14T19:00:00Z",
            }
        )
        store.write(prepared, previous=None)
        store.write(running, previous=prepared)
        store.write(succeeded, previous=running)


class StagingValidationTests(unittest.TestCase):
    def fixture(self, **kwargs):
        fixture = FixtureStaging(self, **kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def test_archival_candidate_report_and_manifests_are_strict_and_replayable(self):
        fixture = self.fixture()
        clock = TickClock()

        candidate = capture_staging_candidate(fixture.job, fixture.config, clock=clock)
        validation_job = build_validation_job(
            fixture.job, candidate.manifest["manifest_id"]
        )
        validated = validate_staging_candidate(
            validation_job, fixture.job, candidate, fixture.config, clock=clock
        )
        replay_candidate = capture_staging_candidate(
            deepcopy(fixture.job), fixture.config, clock=TickClock(BASE_TIME + timedelta(days=1))
        )
        replay_validated = validate_staging_candidate(
            deepcopy(validation_job),
            deepcopy(fixture.job),
            replay_candidate,
            fixture.config,
            clock=TickClock(BASE_TIME + timedelta(days=1)),
        )

        self.assertEqual(candidate, replay_candidate)
        self.assertEqual(validated, replay_validated)
        self.assertEqual(validated.report["status"], "valid")
        self.assertEqual(validated.report["issues"], {})
        self.assertEqual(
            validated.report["metrics"], {"paper_count": 1, "valid_pdf_count": 1}
        )
        self.assertEqual(
            [item["artifact_kind"] for item in validated.manifest["artifacts"]],
            ["staging_dataset", "validation_report"],
        )
        validate_job_manifest(candidate.manifest, fixture.job)
        validate_job_manifest(validated.manifest, validation_job)
        self.assertNotIn(str(fixture.root), json.dumps(validated.as_dict()))
        self.assertFalse(any(fixture.canonical_root.iterdir()))

    def test_announced_and_metadata_levels_apply_only_their_required_fields(self):
        announced = paper()
        announced.pop("abstract")
        announced.pop("pdf_url")
        announced.pop("pdf_path")
        for level in ("announced", "metadata"):
            with self.subTest(level=level):
                job = build_scrape_job(
                    suffix=level,
                    level=level,
                    download_pdfs=False,
                    expected_count=1,
                )
                fixture = self.fixture(job=job, papers=[announced])
                candidate = capture_staging_candidate(job, fixture.config, clock=TickClock())
                validation_job = build_validation_job(
                    job, candidate.manifest["manifest_id"], suffix=f"validate-{level}"
                )
                result = validate_staging_candidate(
                    validation_job, job, candidate, fixture.config, clock=TickClock()
                )
                if level == "announced":
                    self.assertEqual(result.report["status"], "valid")
                    self.assertEqual(result.report["issues"], {})
                else:
                    self.assertEqual(result.report["status"], "invalid")
                    self.assertEqual(result.report["issues"], {"missing_abstract": 1})
                self.assertIsNone(result.report["metrics"]["valid_pdf_count"])

    def test_invalid_output_reports_count_metadata_duplicates_and_every_pdf_check(self):
        papers = [
            paper(paper_id="duplicate", pdf_path="papers/icml/2026/valid.pdf"),
            paper(paper_id="duplicate", pdf_path="papers/icml/2026/invalid.pdf"),
            paper(paper_id="undersized", pdf_path="papers/icml/2026/small.pdf"),
            paper(paper_id="missing", pdf_path="papers/icml/2026/missing.pdf"),
            paper(paper_id="no-path", pdf_path=""),
        ]
        papers[-1]["title"] = ""
        papers[-1]["abstract"] = ""
        job = build_scrape_job(suffix="invalid", expected_count=6)
        fixture = self.fixture(job=job, papers=papers)
        invalid_pdf = fixture.data_root / "papers/icml/2026/invalid.pdf"
        invalid_pdf.write_bytes(b"not-pdf" + b"x" * 2048)
        small_pdf = fixture.data_root / "papers/icml/2026/small.pdf"
        small_pdf.write_bytes(b"%PDF-")
        (fixture.data_root / "papers/icml/2026/missing.pdf").unlink()

        candidate = capture_staging_candidate(job, fixture.config, clock=TickClock())
        validation_job = build_validation_job(job, candidate.manifest["manifest_id"])
        result = validate_staging_candidate(
            validation_job, job, candidate, fixture.config, clock=TickClock()
        )

        self.assertEqual(result.report["status"], "invalid")
        self.assertEqual(
            result.report["issues"],
            {
                "duplicate_ids": 1,
                "invalid_pdf_signature": 1,
                "missing_abstract": 1,
                "missing_pdf_file": 1,
                "missing_pdf_path": 1,
                "missing_title": 1,
                "paper_count": 1,
                "undersized_pdf": 1,
            },
        )
        self.assertEqual(result.report["metrics"]["valid_pdf_count"], 1)

    def test_candidate_requires_process_success_and_rejects_symlink_or_special_file(self):
        fixture = self.fixture()
        checkpoint_path = fixture.job_root / "checkpoint.v1.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["status"] = "running"
        checkpoint["reason_code"] = "process_started"
        checkpoint_path.write_text(
            json.dumps(checkpoint, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(StagingValidationError, "process success"):
            capture_staging_candidate(fixture.job, fixture.config, clock=TickClock())

        fixture_time = self.fixture(job=build_scrape_job(suffix="time"))
        with self.assertRaisesRegex(StagingValidationError, "predates"):
            capture_staging_candidate(
                fixture_time.job,
                fixture_time.config,
                clock=TickClock(BASE_TIME - timedelta(seconds=1)),
            )

        fixture2 = self.fixture(job=build_scrape_job(suffix="symlink"))
        target = fixture2.data_root / "target"
        target.write_text("fixture", encoding="utf-8")
        (fixture2.data_root / "link").symlink_to(target)
        with self.assertRaisesRegex(StagingValidationError, "symlink"):
            capture_staging_candidate(fixture2.job, fixture2.config, clock=TickClock())

        if hasattr(os, "mkfifo"):
            fixture3 = self.fixture(job=build_scrape_job(suffix="fifo"))
            os.mkfifo(fixture3.data_root / "pipe")
            with self.assertRaisesRegex(StagingValidationError, "special"):
                capture_staging_candidate(fixture3.job, fixture3.config, clock=TickClock())

    def test_path_escape_identity_downgrade_and_candidate_drift_fail_closed(self):
        escape = paper(pdf_path="../../canonical/secret.pdf")
        fixture = self.fixture(job=build_scrape_job(suffix="escape"), papers=[escape])
        candidate = capture_staging_candidate(fixture.job, fixture.config, clock=TickClock())
        validation_job = build_validation_job(
            fixture.job, candidate.manifest["manifest_id"]
        )
        with self.assertRaisesRegex(StagingValidationError, "pdf_path"):
            validate_staging_candidate(
                validation_job, fixture.job, candidate, fixture.config, clock=TickClock()
            )
        validation_root = (
            fixture.artifact_root
            / fixture.job["job_fingerprint"]
            / "validations"
            / validation_job["job_fingerprint"]
        )
        self.assertFalse((validation_root / "report.v1.json").exists())
        self.assertFalse((validation_root / "manifest.v1.json").exists())

        valid_fixture = self.fixture(job=build_scrape_job(suffix="binding"))
        bound = capture_staging_candidate(
            valid_fixture.job, valid_fixture.config, clock=TickClock()
        )
        downgraded = build_validation_job(
            valid_fixture.job,
            bound.manifest["manifest_id"],
            completeness_level="metadata",
        )
        with self.assertRaisesRegex(StagingValidationError, "bind"):
            validate_staging_candidate(
                downgraded,
                valid_fixture.job,
                bound,
                valid_fixture.config,
                clock=TickClock(),
            )

        metadata = (
            valid_fixture.data_root / "metadata/icml/icml_2026.json"
        )
        metadata.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(StagingArtifactConflictError, "changed"):
            capture_staging_candidate(
                valid_fixture.job, valid_fixture.config, clock=TickClock()
            )

    def test_retained_corruption_foreign_bundle_and_unsafe_roots_fail_closed(self):
        fixture = self.fixture()
        candidate = capture_staging_candidate(fixture.job, fixture.config, clock=TickClock())
        candidate_path = (
            fixture.artifact_root
            / fixture.job["job_fingerprint"]
            / "candidate"
            / "inventory.v1.json"
        )
        candidate_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(StagingArtifactConflictError):
            capture_staging_candidate(fixture.job, fixture.config, clock=TickClock())

        other_job = build_scrape_job(suffix="other")
        other_fixture = self.fixture(job=other_job)
        other_candidate = capture_staging_candidate(
            other_job, other_fixture.config, clock=TickClock()
        )
        validation_job = build_validation_job(
            other_job, other_candidate.manifest["manifest_id"]
        )
        with self.assertRaises(StagingValidationError):
            validate_staging_candidate(
                validation_job,
                other_job,
                CandidateBundle(candidate.inventory, candidate.manifest),
                other_fixture.config,
                clock=TickClock(),
            )

        with self.assertRaisesRegex(StagingValidationError, "disjoint"):
            capture_staging_candidate(
                fixture.job,
                StagingValidationConfig(
                    staging_root=fixture.staging_root,
                    artifact_root=fixture.staging_root / "artifacts",
                    canonical_data_root=fixture.canonical_root,
                ),
                clock=TickClock(),
            )

        private_fixture = self.fixture(job=build_scrape_job(suffix="permissions"))
        private_fixture.artifact_root.chmod(0o755)
        with self.assertRaisesRegex(StagingValidationError, "not private"):
            capture_staging_candidate(
                private_fixture.job, private_fixture.config, clock=TickClock()
            )

    def test_module_has_no_runtime_network_cloud_promotion_or_canonical_write_dependency(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = set()
        called_attributes = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called_attributes.add(node.func.attr)
        forbidden_imports = {
            "subprocess",
            "socket",
            "urllib",
            "requests",
            "prefect",
            "automation.local_service",
            "automation.mac_worker.safety",
            "automation.job_result_consumer",
            "postprocessing.generate_statistics",
        }
        self.assertTrue(imported.isdisjoint(forbidden_imports))
        self.assertTrue(called_attributes.isdisjoint({"publish", "run_staged_scrape"}))


if __name__ == "__main__":
    unittest.main()
