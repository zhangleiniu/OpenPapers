import ast
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from automation.domain import OwnershipError, Writer
from automation.job_results import (
    GcsImmutableResultStore,
    ImmutableObjectConflictError,
    JobResultError,
    build_job_manifest,
    build_job_result,
    manifest_object_name,
    result_object_name,
    validate_result_bundle,
)


FIXTURES = Path(__file__).with_name("fixtures") / "phase4"
MODULE = Path(__file__).resolve().parents[1] / "job_results.py"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def result_bundle():
    return (
        load_fixture("scrape-job.v2.json"),
        load_fixture("job-manifest.v1.json"),
        load_fixture("job-result.v2.json"),
    )


class FakePreconditionFailed(Exception):
    pass


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.generation = None

    def upload_from_string(self, data, *, content_type, if_generation_match):
        self.bucket.calls.append(
            ("upload", self.name, content_type, if_generation_match)
        )
        failure = self.bucket.fail_upload_once.pop(self.name, None)
        if failure is not None:
            raise failure
        if if_generation_match != 0:
            raise AssertionError("fake requires create-only upload")
        if self.name in self.bucket.objects:
            raise FakePreconditionFailed()
        generation = self.bucket.next_generation
        self.bucket.next_generation += 1
        self.bucket.objects[self.name] = (generation, bytes(data))
        self.generation = generation

    def reload(self):
        self.bucket.calls.append(("reload", self.name))
        if self.name not in self.bucket.objects:
            raise FileNotFoundError(self.name)
        self.generation = self.bucket.objects[self.name][0]

    def download_as_bytes(self, *, if_generation_match):
        self.bucket.calls.append(("download", self.name, if_generation_match))
        generation, data = self.bucket.objects[self.name]
        if self.bucket.mutate_before_download == self.name:
            generation += 1
            self.bucket.objects[self.name] = (generation, data + b" ")
            self.bucket.mutate_before_download = None
        current_generation, current_data = self.bucket.objects[self.name]
        if if_generation_match != current_generation:
            raise FakePreconditionFailed()
        return current_data


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.next_generation = 1
        self.fail_upload_once = {}
        self.mutate_before_download = None

    def blob(self, name):
        return FakeBlob(self, name)


def fake_store(bucket=None):
    bucket = bucket or FakeBucket()
    return bucket, GcsImmutableResultStore(
        bucket, precondition_error_types=(FakePreconditionFailed,)
    )


class ResultContractTests(unittest.TestCase):
    def test_builders_reproduce_strict_sanitized_fixtures(self):
        job, expected_manifest, expected_result = result_bundle()
        manifest = build_job_manifest(
            job,
            created_at="2026-07-13T14:00:00Z",
            artifacts=[deepcopy(expected_manifest["artifacts"][0])],
        )
        result = build_job_result(
            job,
            manifest,
            worker_id="worker:mac-mini:fixture",
            completed_at="2026-07-13T14:02:00Z",
            status="succeeded",
            error_code=None,
            error_summary=None,
            duration_seconds=120.0,
            paper_count=100,
            valid_pdf_count=100,
        )
        self.assertEqual(manifest, expected_manifest)
        self.assertEqual(result, expected_result)
        validate_result_bundle(job, manifest, result)

    def test_forged_identity_fingerprints_and_manifest_links_fail_closed(self):
        job, manifest, result = result_bundle()
        candidates = []
        forged = deepcopy(manifest)
        forged["manifest_fingerprint"] = "f" * 64
        candidates.append((forged, result, "manifest_fingerprint"))
        forged_result = deepcopy(result)
        forged_result["result_fingerprint"] = "f" * 64
        candidates.append((manifest, forged_result, "result_fingerprint"))
        wrong_link = deepcopy(result)
        wrong_link["manifest_id"] = "manifest:" + "f" * 64
        candidates.append((manifest, wrong_link, "identity"))
        wrong_job = deepcopy(job)
        wrong_job["venue_id"] = "neurips"
        candidates.append((manifest, result, "job_fingerprint"))
        for candidate_manifest, candidate_result, message in candidates:
            with self.subTest(message=message), self.assertRaisesRegex(
                (JobResultError, ValueError), message
            ):
                validate_result_bundle(
                    wrong_job if message == "job_fingerprint" else job,
                    candidate_manifest,
                    candidate_result,
                )

    def test_status_time_artifact_and_secret_semantics_are_closed(self):
        job, manifest, result = result_bundle()
        empty_manifest = build_job_manifest(
            job, created_at="2026-07-13T14:00:00Z", artifacts=[]
        )
        with self.assertRaisesRegex(JobResultError, "at least one artifact"):
            build_job_result(
                job,
                empty_manifest,
                worker_id="worker:fixture",
                completed_at="2026-07-13T14:01:00Z",
                status="succeeded",
                error_code=None,
                error_summary=None,
                duration_seconds=1,
                paper_count=0,
                valid_pdf_count=0,
            )
        early = deepcopy(result)
        early["completed_at"] = "2026-07-13T13:59:00Z"
        early["result_fingerprint"] = "0" * 64
        from automation.contracts import artifact_fingerprint

        early["result_fingerprint"] = artifact_fingerprint(
            {key: value for key, value in early.items() if key != "result_fingerprint"}
        )
        with self.assertRaisesRegex(JobResultError, "before"):
            validate_result_bundle(job, manifest, early)
        secret = deepcopy(manifest)
        secret["artifacts"][0]["api_key"] = "fixture"
        with self.assertRaisesRegex(ValueError, "api_key"):
            validate_result_bundle(job, secret, result)


class ImmutableGcsProtocolTests(unittest.TestCase):
    def test_manifest_precedes_result_and_exact_replay_is_idempotent(self):
        job, manifest, result = result_bundle()
        bucket, store = fake_store()

        first = store.publish(job, manifest, result)
        replay = store.publish(job, manifest, result)

        self.assertEqual(first, replay)
        uploads = [call for call in bucket.calls if call[0] == "upload"]
        self.assertEqual(
            [(call[1], call[3]) for call in uploads],
            [
                (manifest_object_name(job["job_id"]), 0),
                (result_object_name(job["job_id"]), 0),
                (manifest_object_name(job["job_id"]), 0),
                (result_object_name(job["job_id"]), 0),
            ],
        )
        self.assertEqual(len(bucket.objects), 2)
        read_manifest, read_result = store.read_bundle(job)
        self.assertEqual(read_manifest.payload, manifest)
        self.assertEqual(read_result.payload, result)
        downloads = [call for call in bucket.calls if call[0] == "download"]
        self.assertTrue(all(call[2] >= 1 for call in downloads))

    def test_conflict_never_overwrites_and_wrong_writer_is_rejected(self):
        job, manifest, result = result_bundle()
        bucket, store = fake_store()
        store.publish(job, manifest, result)
        result_name = result_object_name(job["job_id"])
        generation, original = bucket.objects[result_name]
        bucket.objects[result_name] = (generation, original.replace(b"120.0", b"121.0"))

        with self.assertRaisesRegex(ImmutableObjectConflictError, "different"):
            store.publish(job, manifest, result)
        self.assertEqual(bucket.objects[result_name][1], original.replace(b"120.0", b"121.0"))
        with self.assertRaises(OwnershipError):
            store.publish(
                job, manifest, result, writer=Writer.CLOUD_CONTROL_PLANE
            )

    def test_manifest_only_partial_publish_recovers_on_retry(self):
        job, manifest, result = result_bundle()
        bucket, store = fake_store()
        result_name = result_object_name(job["job_id"])
        bucket.fail_upload_once[result_name] = RuntimeError("fixture outage")

        with self.assertRaisesRegex(RuntimeError, "fixture outage"):
            store.publish(job, manifest, result)
        self.assertIn(manifest_object_name(job["job_id"]), bucket.objects)
        self.assertNotIn(result_name, bucket.objects)

        receipt = store.publish(job, manifest, result)
        self.assertEqual(receipt.result_name, result_name)
        self.assertIn(result_name, bucket.objects)

    def test_generation_change_during_read_fails_without_torn_payload(self):
        job, manifest, result = result_bundle()
        bucket, store = fake_store()
        store.publish(job, manifest, result)
        bucket.mutate_before_download = manifest_object_name(job["job_id"])

        with self.assertRaises(FakePreconditionFailed):
            store.read_bundle(job)


class ScopeBoundaryTests(unittest.TestCase):
    def test_module_constructs_no_client_control_state_or_command(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("automation.control_state", imports)
        self.assertNotIn("subprocess", imports)
        self.assertNotIn("prefect", imports)
        self.assertNotIn("storage.Client(", source)


if __name__ == "__main__":
    unittest.main()
