import ast
import json
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from automation.contracts import (
    ContractName,
    ContractValidationError,
    validate_contract,
)
from automation.domain import ActionType, SecretBoundaryError
from automation.job_queue import (
    PREFECT_WORK_POOL_NAME,
    JobQueueError,
    JobType,
    PrefectDeploymentSubmitter,
    SubmissionReceipt,
    WorkQueueName,
    build_job,
    build_queue_envelope,
    build_scrape_job_from_action,
    queue_for_job_type,
    submit_job,
    validate_job_identity,
    validate_queue_envelope,
    work_pool_blueprint,
)
from automation.lifecycle import (
    ActionIntent,
    QueueExistingScraperPayload,
    RecheckPayload,
)


FIXTURES = Path(__file__).with_name("fixtures") / "phase4"
MODULE = Path(__file__).resolve().parents[1] / "job_queue.py"
SCRAPE_DEPLOYMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
FLOW_RUN_ID = UUID("22222222-2222-4222-8222-222222222222")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def scrape_action() -> ActionIntent:
    return ActionIntent(
        action_id="action:icml:2026:pdf-ready",
        action_type=ActionType.QUEUE_EXISTING_SCRAPER,
        venue_id="icml",
        year=2026,
        evidence_ids=("evidence:icml:2026:pdf-ready",),
        payload=QueueExistingScraperPayload(
            readiness="pdf_ready",
            scraper_module="scrapers.icml",
            scraper_class="ICMLScraper",
        ),
    )


class FakePrefectClient:
    def __init__(
        self,
        *,
        flow_run_id=FLOW_RUN_ID,
        work_pool_name=PREFECT_WORK_POOL_NAME,
        work_queue_name=WorkQueueName.SCRAPE.value,
    ):
        self.flow_run_id = flow_run_id
        self.deployment = SimpleNamespace(
            work_pool_name=work_pool_name,
            work_queue_name=work_queue_name,
        )
        self.read_calls = []
        self.create_calls = []

    async def read_deployment(self, deployment_id):
        self.read_calls.append(deployment_id)
        return self.deployment

    async def create_flow_run_from_deployment(self, deployment_id, **kwargs):
        self.create_calls.append((deployment_id, deepcopy(kwargs)))
        return SimpleNamespace(id=self.flow_run_id)


class RecordingSubmitter:
    def __init__(self, receipt=None):
        self.calls = []
        self.receipt = receipt

    async def submit(self, envelope, *, idempotency_key):
        self.calls.append((deepcopy(dict(envelope)), idempotency_key))
        if self.receipt is not None:
            return self.receipt
        return SubmissionReceipt(
            job_id=envelope["job"]["job_id"],
            flow_run_id=str(FLOW_RUN_ID),
            work_pool_name=envelope["work_pool_name"],
            work_queue_name=envelope["work_queue_name"],
        )


class JobIdentityAndQueueTests(unittest.TestCase):
    def test_blueprint_has_dedicated_process_pool_and_typed_queues(self):
        blueprint = work_pool_blueprint()

        self.assertEqual(blueprint.name, "openpapers-mac")
        self.assertEqual(blueprint.work_pool_type, "process")
        self.assertEqual(
            {
                (queue.job_type.value, queue.name.value)
                for queue in blueprint.queues
            },
            {
                ("scrape_existing", "openpapers-scrape"),
                ("validate_candidate", "openpapers-validation"),
                ("codex_diagnosis", "openpapers-codex"),
            },
        )

    def test_action_builds_stable_v2_scrape_job_without_execution_fields(self):
        first = build_scrape_job_from_action(scrape_action())
        replay = build_scrape_job_from_action(scrape_action())

        self.assertEqual(first, replay)
        self.assertEqual(first["job_id"], f"job:{first['job_fingerprint']}")
        self.assertEqual(first["request_id"], scrape_action().action_id)
        self.assertEqual(first["job_type"], "scrape_existing")
        self.assertEqual(
            first["payload"],
            {
                "completeness_level": "archival",
                "download_pdfs": True,
                "expected_count": None,
            },
        )
        serialized = json.dumps(first, sort_keys=True)
        for forbidden in (
            "command",
            "environment",
            "scraper_module",
            "scraper_class",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_identity_changes_with_request_evidence_or_payload(self):
        baseline = build_scrape_job_from_action(scrape_action())
        changed_request = build_job(
            request_id="action:icml:2026:another-request",
            job_type=JobType.SCRAPE_EXISTING,
            venue_id="icml",
            year=2026,
            requested_by="action_router",
            input_artifact_ids=("evidence:icml:2026:pdf-ready",),
            payload=baseline["payload"],
        )
        changed_evidence = build_job(
            request_id=scrape_action().action_id,
            job_type=JobType.SCRAPE_EXISTING,
            venue_id="icml",
            year=2026,
            requested_by="action_router",
            input_artifact_ids=("evidence:icml:2026:new-pdf-ready",),
            payload=baseline["payload"],
        )
        changed_payload = build_job(
            request_id=scrape_action().action_id,
            job_type=JobType.SCRAPE_EXISTING,
            venue_id="icml",
            year=2026,
            requested_by="action_router",
            input_artifact_ids=("evidence:icml:2026:pdf-ready",),
            payload={**baseline["payload"], "expected_count": 100},
        )

        self.assertEqual(
            len(
                {
                    baseline["job_id"],
                    changed_request["job_id"],
                    changed_evidence["job_id"],
                    changed_payload["job_id"],
                }
            ),
            4,
        )

    def test_job_builder_rejects_empty_or_duplicate_artifact_identity(self):
        for artifact_ids in ((), ("evidence:duplicate", "evidence:duplicate")):
            with self.subTest(artifact_ids=artifact_ids), self.assertRaisesRegex(
                JobQueueError, "unique, non-empty"
            ):
                build_job(
                    request_id="request:icml:2026:scrape",
                    job_type=JobType.SCRAPE_EXISTING,
                    venue_id="icml",
                    year=2026,
                    requested_by="human",
                    input_artifact_ids=artifact_ids,
                    payload={
                        "completeness_level": "archival",
                        "download_pdfs": True,
                        "expected_count": None,
                    },
                )

    def test_forged_identity_unknown_fields_and_secrets_fail_closed(self):
        job = build_scrape_job_from_action(scrape_action())
        forged = deepcopy(job)
        forged["job_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(JobQueueError, "job_fingerprint"):
            validate_job_identity(forged)

        command = deepcopy(job)
        command["payload"]["command"] = "python main.py icml 2026"
        with self.assertRaises(ContractValidationError):
            validate_job_identity(command)

        secret = deepcopy(job)
        secret["payload"]["auth_token"] = "fixture-secret"
        with self.assertRaises(SecretBoundaryError):
            validate_job_identity(secret)

    def test_v1_job_remains_contract_valid_but_cannot_cross_queue_boundary(self):
        path = FIXTURES.parent / "phase0" / "scrape-job.v1.json"
        legacy = json.loads(path.read_text(encoding="utf-8"))

        validate_contract(ContractName.JOB, legacy)
        with self.assertRaisesRegex(JobQueueError, "only v2"):
            validate_job_identity(legacy)

    def test_each_job_type_has_one_fixed_queue(self):
        self.assertEqual(
            queue_for_job_type(JobType.SCRAPE_EXISTING), WorkQueueName.SCRAPE)
        self.assertEqual(
            queue_for_job_type(JobType.VALIDATE_CANDIDATE),
            WorkQueueName.VALIDATION,
        )
        self.assertEqual(
            queue_for_job_type(JobType.CODEX_DIAGNOSIS), WorkQueueName.CODEX)
        with self.assertRaises(JobQueueError):
            queue_for_job_type("arbitrary_shell")

    def test_envelope_rejects_queue_drift_and_defensively_copies_job(self):
        job = build_scrape_job_from_action(scrape_action())
        envelope = build_queue_envelope(job)
        job["payload"]["expected_count"] = 999

        serialized = envelope.as_dict()
        self.assertEqual(serialized["work_pool_name"], PREFECT_WORK_POOL_NAME)
        self.assertEqual(serialized["work_queue_name"], "openpapers-scrape")
        self.assertIsNone(serialized["job"]["payload"]["expected_count"])
        serialized["work_queue_name"] = "openpapers-codex"
        with self.assertRaisesRegex(JobQueueError, "does not match"):
            validate_queue_envelope(serialized)

    def test_phase4_fixtures_have_recomputable_identity_and_queue(self):
        job = load_fixture("scrape-job.v2.json")
        envelope = load_fixture("scrape-queue-envelope.v1.json")

        validate_job_identity(job)
        validate_queue_envelope(envelope)
        self.assertEqual(envelope["job"], job)

    def test_non_scrape_action_and_unverified_readiness_are_rejected(self):
        wrong = ActionIntent(
            action_id="action:icml:2026:recheck",
            action_type=ActionType.RECHECK_AT,
            venue_id="icml",
            year=2026,
            evidence_ids=("evidence:icml:2026:pdf-ready",),
            payload=RecheckPayload(
                at="2026-07-14T00:00:00Z", reason="recheck"
            ),
        )
        with self.assertRaisesRegex(JobQueueError, "queue_existing_scraper"):
            build_scrape_job_from_action(wrong)

        action = scrape_action()
        unverified = ActionIntent(
            action_id=action.action_id,
            action_type=action.action_type,
            venue_id=action.venue_id,
            year=action.year,
            evidence_ids=action.evidence_ids,
            payload=QueueExistingScraperPayload(
                readiness="metadata_ready",
                scraper_module=action.payload.scraper_module,
                scraper_class=action.payload.scraper_class,
            ),
        )
        with self.assertRaisesRegex(JobQueueError, "pdf_ready"):
            build_scrape_job_from_action(unverified)

    def test_module_has_no_worker_storage_command_or_live_client_dependency(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
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
                "prefect",
                "google",
                "sqlite3",
                "subprocess",
                "main",
                "scrapers",
            }.isdisjoint(imports)
        )


class CloudSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_passes_job_id_as_idempotency_key(self):
        job = build_scrape_job_from_action(scrape_action())
        submitter = RecordingSubmitter()

        receipt = await submit_job(job, submitter)

        envelope, key = submitter.calls[0]
        self.assertEqual(key, job["job_id"])
        self.assertEqual(envelope["job"], job)
        self.assertEqual(receipt.job_id, job["job_id"])

    async def test_invalid_job_fails_before_submitter_call(self):
        job = build_scrape_job_from_action(scrape_action())
        job["payload"]["command"] = "do not run"
        submitter = RecordingSubmitter()

        with self.assertRaises(ContractValidationError):
            await submit_job(job, submitter)

        self.assertEqual(submitter.calls, [])

    async def test_mismatched_receipt_fails_closed(self):
        job = build_scrape_job_from_action(scrape_action())
        submitter = RecordingSubmitter(
            SubmissionReceipt(
                job_id="job:" + "0" * 64,
                flow_run_id=str(FLOW_RUN_ID),
                work_pool_name=PREFECT_WORK_POOL_NAME,
                work_queue_name=WorkQueueName.SCRAPE.value,
            )
        )

        with self.assertRaisesRegex(JobQueueError, "does not match"):
            await submit_job(job, submitter)

    async def test_prefect_adapter_uses_fixed_deployment_queue_and_parameters(self):
        job = build_scrape_job_from_action(scrape_action())
        client = FakePrefectClient()
        adapter = PrefectDeploymentSubmitter(
            client, {WorkQueueName.SCRAPE: SCRAPE_DEPLOYMENT_ID}
        )

        receipt = await submit_job(job, adapter)

        self.assertEqual(receipt.flow_run_id, str(FLOW_RUN_ID))
        self.assertEqual(client.read_calls, [SCRAPE_DEPLOYMENT_ID])
        deployment_id, kwargs = client.create_calls[0]
        self.assertEqual(deployment_id, SCRAPE_DEPLOYMENT_ID)
        self.assertEqual(kwargs["idempotency_key"], job["job_id"])
        self.assertEqual(kwargs["work_queue_name"], WorkQueueName.SCRAPE.value)
        self.assertEqual(kwargs["parameters"]["queue_envelope"]["job"], job)

    async def test_exact_replay_uses_the_same_prefect_idempotency_request(self):
        job = build_scrape_job_from_action(scrape_action())
        client = FakePrefectClient()
        adapter = PrefectDeploymentSubmitter(
            client, {WorkQueueName.SCRAPE: SCRAPE_DEPLOYMENT_ID}
        )

        first = await submit_job(job, adapter)
        replay = await submit_job(deepcopy(job), adapter)

        self.assertEqual(first, replay)
        self.assertEqual(len(client.read_calls), 2)
        self.assertEqual(len(client.create_calls), 2)
        self.assertEqual(
            [call[1]["idempotency_key"] for call in client.create_calls],
            [job["job_id"], job["job_id"]],
        )

    async def test_missing_deployment_or_wrong_key_makes_no_prefect_call(self):
        job = build_scrape_job_from_action(scrape_action())
        envelope = build_queue_envelope(job).as_dict()
        client = FakePrefectClient()
        adapter = PrefectDeploymentSubmitter(client, {})

        with self.assertRaisesRegex(JobQueueError, "no Prefect deployment"):
            await submit_job(job, adapter)
        with self.assertRaisesRegex(JobQueueError, "idempotency key"):
            await adapter.submit(envelope, idempotency_key="job:" + "0" * 64)

        self.assertEqual(client.read_calls, [])
        self.assertEqual(client.create_calls, [])

    async def test_misconfigured_prefect_pool_or_queue_fails_before_create(self):
        job = build_scrape_job_from_action(scrape_action())
        for pool, queue in (
            ("wrong-pool", WorkQueueName.SCRAPE.value),
            (PREFECT_WORK_POOL_NAME, WorkQueueName.CODEX.value),
        ):
            with self.subTest(pool=pool, queue=queue):
                client = FakePrefectClient(
                    work_pool_name=pool,
                    work_queue_name=queue,
                )
                adapter = PrefectDeploymentSubmitter(
                    client, {WorkQueueName.SCRAPE: SCRAPE_DEPLOYMENT_ID}
                )

                with self.assertRaisesRegex(JobQueueError, "required pool and queue"):
                    await submit_job(job, adapter)

                self.assertEqual(client.read_calls, [SCRAPE_DEPLOYMENT_ID])
                self.assertEqual(client.create_calls, [])

    async def test_prefect_response_without_id_fails_without_false_receipt(self):
        job = build_scrape_job_from_action(scrape_action())
        client = FakePrefectClient(flow_run_id=None)
        adapter = PrefectDeploymentSubmitter(
            client, {WorkQueueName.SCRAPE: SCRAPE_DEPLOYMENT_ID}
        )

        with self.assertRaisesRegex(JobQueueError, "without an ID"):
            await submit_job(job, adapter)

        self.assertEqual(client.read_calls, [SCRAPE_DEPLOYMENT_ID])
        self.assertEqual(len(client.create_calls), 1)


if __name__ == "__main__":
    unittest.main()
