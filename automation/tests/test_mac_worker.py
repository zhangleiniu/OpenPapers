import ast
import inspect
import json
import unittest
from copy import deepcopy
from pathlib import Path

from automation.domain import SecretBoundaryError
from automation.job_queue import (
    JobQueueError,
    JobType,
    build_job,
    build_queue_envelope,
)
from automation.mac_worker.prefect_support import openpapers_mac_fixture_job
from automation.mac_worker.runtime import simulate_queue_envelope


FIXTURES = Path(__file__).with_name("fixtures") / "phase4"
PACKAGE = Path(__file__).resolve().parents[1] / "mac_worker"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_envelope(job_type: JobType) -> dict:
    if job_type is JobType.SCRAPE_EXISTING:
        return load_fixture("scrape-queue-envelope.v1.json")
    if job_type is JobType.VALIDATE_CANDIDATE:
        payload = {
            "candidate_manifest_id": "manifest:icml:2026:candidate",
            "completeness_level": "archival",
            "require_pdfs": True,
            "expected_count": 100,
        }
    else:
        payload = {
            "failure_fingerprint": "a" * 64,
            "snapshot_ids": ["snapshot:icml:2026:failure"],
            "allowed_paths": ["scrapers/icml.py", "automation/tests/fixtures"],
            "max_runtime_minutes": 30,
            "mode": "diagnose_only",
        }
    job = build_job(
        request_id=f"request:icml:2026:{job_type.value}",
        job_type=job_type,
        venue_id="icml",
        year=2026,
        requested_by="human",
        input_artifact_ids=(f"evidence:icml:2026:{job_type.value}",),
        payload=payload,
    )
    return build_queue_envelope(job).as_dict()


class MacFixtureRuntimeTests(unittest.TestCase):
    def test_every_typed_queue_is_revalidated_and_only_simulated(self):
        expected_queues = {
            JobType.SCRAPE_EXISTING: "openpapers-scrape",
            JobType.VALIDATE_CANDIDATE: "openpapers-validation",
            JobType.CODEX_DIAGNOSIS: "openpapers-codex",
        }
        for job_type, queue in expected_queues.items():
            with self.subTest(job_type=job_type):
                envelope = fixture_envelope(job_type)
                observation = simulate_queue_envelope(envelope)

                self.assertEqual(observation.status, "simulated")
                self.assertEqual(
                    observation.reason_code, "fixture_only_no_execution"
                )
                self.assertEqual(observation.job_id, envelope["job"]["job_id"])
                self.assertEqual(observation.job_type, job_type.value)
                self.assertEqual(observation.work_pool_name, "openpapers-mac")
                self.assertEqual(observation.work_queue_name, queue)
                serialized = json.dumps(observation.as_dict(), sort_keys=True)
                for forbidden in (
                    "completed_at",
                    "result_fingerprint",
                    "artifact_ids",
                    "command",
                ):
                    self.assertNotIn(forbidden, serialized)

    def test_replay_is_stable_and_does_not_mutate_the_input(self):
        envelope = fixture_envelope(JobType.SCRAPE_EXISTING)
        original = deepcopy(envelope)

        first = simulate_queue_envelope(envelope)
        second = simulate_queue_envelope(envelope)

        self.assertEqual(first, second)
        self.assertEqual(envelope, original)

    def test_forged_misrouted_arbitrary_or_secret_input_fails_closed(self):
        envelope = fixture_envelope(JobType.SCRAPE_EXISTING)
        mutations = []

        forged = deepcopy(envelope)
        forged["job"]["job_id"] = "job:" + "0" * 64
        mutations.append((forged, JobQueueError))

        misrouted = deepcopy(envelope)
        misrouted["work_queue_name"] = "openpapers-codex"
        mutations.append((misrouted, JobQueueError))

        arbitrary = deepcopy(envelope)
        arbitrary["job"]["payload"]["command"] = "python main.py icml 2026"
        mutations.append((arbitrary, ValueError))

        secret = deepcopy(envelope)
        secret["job"]["payload"]["api_token"] = "fixture-secret"
        mutations.append((secret, SecretBoundaryError))

        for candidate, error in mutations:
            with self.subTest(error=error), self.assertRaises(error):
                simulate_queue_envelope(candidate)

    def test_prefect_flow_accepts_exact_parameter_and_delegates_to_simulator(self):
        envelope = fixture_envelope(JobType.SCRAPE_EXISTING)

        result = openpapers_mac_fixture_job.fn(envelope)

        self.assertEqual(result, simulate_queue_envelope(envelope).as_dict())
        self.assertEqual(
            tuple(inspect.signature(openpapers_mac_fixture_job.fn).parameters),
            ("queue_envelope",),
        )
        self.assertEqual(openpapers_mac_fixture_job.name, "openpapers-mac-fixture-job")
        self.assertFalse(openpapers_mac_fixture_job.persist_result)
        self.assertFalse(openpapers_mac_fixture_job.cache_result_in_memory)

    def test_package_has_no_execution_storage_or_deployed_flow_dependency(self):
        forbidden_imports = {
            "subprocess",
            "main",
            "scrapers",
            "postprocessing",
            "sqlite3",
            "google",
            "control_state",
            "prefect_flows",
            "notifications",
        }
        for path in PACKAGE.glob("*.py"):
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imports = {
                    node.module.split(".", 1)[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module
                }
                imports.update(
                    alias.name.split(".", 1)[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                )
                self.assertTrue(forbidden_imports.isdisjoint(imports))


if __name__ == "__main__":
    unittest.main()
