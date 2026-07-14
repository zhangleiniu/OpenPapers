import ast
import json
import unittest
from copy import deepcopy
from pathlib import Path

from automation.command_registry import (
    ApprovedCommandSpec,
    CommandRegistryError,
    DataRootPolicy,
    RepositoryEntryPoint,
    resolve_approved_command,
)
from automation.contracts import artifact_fingerprint
from automation.job_queue import JobType, build_job


MODULE = Path(__file__).resolve().parents[1] / "command_registry.py"


def build_scrape_job(**payload_overrides):
    payload = {
        "completeness_level": "archival",
        "download_pdfs": True,
        "expected_count": None,
        **payload_overrides,
    }
    return build_job(
        request_id="request:icml:2026:scrape",
        job_type=JobType.SCRAPE_EXISTING,
        venue_id="icml",
        year=2026,
        requested_by="human",
        input_artifact_ids=("evidence:icml:2026:pdf-ready",),
        payload=payload,
    )


def build_validation_job(**payload_overrides):
    payload = {
        "candidate_manifest_id": "manifest:icml:2026:candidate",
        "completeness_level": "archival",
        "require_pdfs": True,
        "expected_count": 100,
        **payload_overrides,
    }
    return build_job(
        request_id="request:icml:2026:validate",
        job_type=JobType.VALIDATE_CANDIDATE,
        venue_id="icml",
        year=2026,
        requested_by="human",
        input_artifact_ids=("manifest:icml:2026:candidate",),
        payload=payload,
    )


def reidentify(job):
    candidate = deepcopy(job)
    identity_fields = {
        key: deepcopy(value)
        for key, value in candidate.items()
        if key not in {"job_id", "job_fingerprint"}
    }
    fingerprint = artifact_fingerprint(identity_fields)
    candidate["job_fingerprint"] = fingerprint
    candidate["job_id"] = f"job:{fingerprint}"
    return candidate


class ApprovedCommandRegistryTests(unittest.TestCase):
    def test_scrape_job_maps_to_fixed_repository_entry_point(self):
        job = build_scrape_job()

        spec = resolve_approved_command(job)

        self.assertEqual(
            spec,
            ApprovedCommandSpec(
                job_id=job["job_id"],
                job_type=JobType.SCRAPE_EXISTING,
                entry_point=RepositoryEntryPoint.SCRAPER,
                arguments=(
                    "icml",
                    "2026",
                    "--require-complete",
                    "--completeness-level",
                    "archival",
                ),
            ),
        )
        self.assertEqual(
            spec.data_root_policy, DataRootPolicy.ISOLATED_STAGING_REQUIRED
        )

    def test_scrape_flags_are_derived_only_from_closed_payload(self):
        spec = resolve_approved_command(
            build_scrape_job(
                completeness_level="metadata",
                download_pdfs=False,
                expected_count=42,
            )
        )

        self.assertEqual(
            spec.arguments,
            (
                "icml",
                "2026",
                "--require-complete",
                "--completeness-level",
                "metadata",
                "--no-pdfs",
            ),
        )
        self.assertNotIn("42", spec.arguments)

    def test_validation_job_maps_to_fixed_independent_validator(self):
        job = build_validation_job()

        spec = resolve_approved_command(job)

        self.assertEqual(spec.entry_point, RepositoryEntryPoint.VALIDATOR)
        self.assertEqual(
            spec.arguments,
            (
                "icml",
                "2026",
                "--level",
                "archival",
                "--require-pdfs",
                "--expected-count",
                "100",
            ),
        )
        self.assertNotIn(job["payload"]["candidate_manifest_id"], spec.arguments)

    def test_validation_optional_flags_are_omitted_by_typed_values(self):
        spec = resolve_approved_command(
            build_validation_job(
                completeness_level="announced",
                require_pdfs=False,
                expected_count=None,
            )
        )

        self.assertEqual(
            spec.arguments, ("icml", "2026", "--level", "announced")
        )

    def test_replay_is_stable_and_input_and_output_are_defensive(self):
        job = build_scrape_job()
        original = deepcopy(job)

        first = resolve_approved_command(job)
        second = resolve_approved_command(job)
        serialized = first.as_dict()
        serialized["arguments"].append("--verbose")

        self.assertEqual(first, second)
        self.assertEqual(job, original)
        self.assertNotIn("--verbose", first.arguments)
        self.assertEqual(first.as_dict()["entry_point"], "main.py")
        self.assertEqual(
            first.as_dict()["data_root_policy"], "isolated_staging_required"
        )

    def test_codex_job_is_not_approved_by_phase5(self):
        job = build_job(
            request_id="request:icml:2026:codex",
            job_type=JobType.CODEX_DIAGNOSIS,
            venue_id="icml",
            year=2026,
            requested_by="human",
            input_artifact_ids=("failure:icml:2026:parser",),
            payload={
                "failure_fingerprint": "a" * 64,
                "snapshot_ids": ["snapshot:icml:2026:failure"],
                "allowed_paths": ["scrapers/icml.py"],
                "max_runtime_minutes": 30,
                "mode": "diagnose_only",
            },
        )

        with self.assertRaisesRegex(CommandRegistryError, "not approved"):
            resolve_approved_command(job)

    def test_legacy_and_forged_jobs_fail_closed(self):
        legacy_path = (
            MODULE.parent / "tests" / "fixtures" / "phase0" / "scrape-job.v1.json"
        )
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        forged = build_scrape_job()
        forged["job_id"] = "job:" + "0" * 64

        for candidate in (legacy, forged):
            with self.subTest(schema=candidate["schema_version"]), self.assertRaises(
                CommandRegistryError
            ):
                resolve_approved_command(candidate)

        for candidate in (None, "python main.py icml 2026"):
            with self.subTest(candidate=type(candidate).__name__), self.assertRaises(
                CommandRegistryError
            ):
                resolve_approved_command(candidate)

    def test_arbitrary_execution_fields_fail_even_with_recomputed_identity(self):
        mutations = {
            "shell": ("payload", "shell", "python main.py icml 2026"),
            "command": ("payload", "command", "python main.py"),
            "path": ("payload", "path", "../../main.py"),
            "flags": ("payload", "flags", ["--verbose"]),
            "environment": ("payload", "environment", {"VENUE": "icml"}),
            "argv": (None, "argv", ["main.py", "--help"]),
        }
        for name, (container, key, value) in mutations.items():
            candidate = build_scrape_job()
            target = candidate if container is None else candidate[container]
            target[key] = value
            candidate = reidentify(candidate)
            with self.subTest(name=name), self.assertRaises(CommandRegistryError):
                resolve_approved_command(candidate)

    def test_path_flag_and_environment_expansion_values_fail_closed(self):
        for venue_id in ("../../icml", "--help", "$VENUE", "~user", "icml/path"):
            candidate = build_scrape_job()
            candidate["venue_id"] = venue_id
            candidate = reidentify(candidate)
            with self.subTest(venue_id=venue_id), self.assertRaises(
                CommandRegistryError
            ):
                resolve_approved_command(candidate)

        expanded = build_scrape_job()
        expanded["payload"]["completeness_level"] = "${LEVEL}"
        with self.assertRaises(CommandRegistryError):
            resolve_approved_command(reidentify(expanded))

    def test_module_has_no_executor_shell_path_or_environment_capability(self):
        source = MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
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
        self.assertTrue(
            {
                "subprocess",
                "os",
                "shlex",
                "pathlib",
                "main",
                "scrapers",
                "postprocessing",
                "prefect",
                "google",
            }.isdisjoint(imports)
        )
        for forbidden in (
            "shell=True",
            "os.environ",
            "getenv(",
            "expandvars",
            "expanduser",
            "subprocess.",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
