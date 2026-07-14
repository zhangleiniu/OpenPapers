"""Pure P4.1 typed-job routing and injected Prefect submission boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from automation.contracts import (
    ContractName,
    artifact_fingerprint,
    validate_contract,
)
from automation.domain import ActionType, assert_secret_free
from automation.lifecycle import ActionIntent, QueueExistingScraperPayload


PREFECT_WORK_POOL_NAME = "openpapers-mac"
PREFECT_WORK_POOL_TYPE = "process"


class JobQueueError(ValueError):
    """Raised when a job or queue handoff violates the P4.1 protocol."""


class JobType(str, Enum):
    """Existing closed job vocabulary from the versioned job contract."""

    SCRAPE_EXISTING = "scrape_existing"
    VALIDATE_CANDIDATE = "validate_candidate"
    CODEX_DIAGNOSIS = "codex_diagnosis"


class WorkQueueName(str, Enum):
    """Capability-separated queues in the dedicated Mac process pool."""

    SCRAPE = "openpapers-scrape"
    VALIDATION = "openpapers-validation"
    CODEX = "openpapers-codex"


_QUEUE_FOR_JOB_TYPE: dict[JobType, WorkQueueName] = {
    JobType.SCRAPE_EXISTING: WorkQueueName.SCRAPE,
    JobType.VALIDATE_CANDIDATE: WorkQueueName.VALIDATION,
    JobType.CODEX_DIAGNOSIS: WorkQueueName.CODEX,
}


@dataclass(frozen=True)
class QueueBlueprint:
    """One local queue specification; it does not create a Prefect resource."""

    name: WorkQueueName
    job_type: JobType


@dataclass(frozen=True)
class WorkPoolBlueprint:
    """Reviewed local shape of the future Prefect process work pool."""

    name: str
    work_pool_type: str
    queues: tuple[QueueBlueprint, ...]


@dataclass(frozen=True)
class QueueEnvelope:
    """A validated immutable handoff from cloud routing to orchestration."""

    work_pool_name: str
    work_queue_name: WorkQueueName
    job: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return a defensive JSON-compatible representation."""
        return {
            "schema_version": 1,
            "work_pool_name": self.work_pool_name,
            "work_queue_name": self.work_queue_name.value,
            "job": deepcopy(self.job),
        }


@dataclass(frozen=True)
class SubmissionReceipt:
    """Bounded cloud-submission result without Prefect model leakage."""

    job_id: str
    flow_run_id: str
    work_pool_name: str
    work_queue_name: str


class CloudJobSubmitter(Protocol):
    """Effect boundary used by cloud callers and fake tests."""

    async def submit(
        self,
        envelope: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> SubmissionReceipt:
        """Submit one validated envelope using its immutable job identity."""


class PrefectDeploymentClient(Protocol):
    """Minimal Prefect 3 client surface required by the P4.1 adapter."""

    async def read_deployment(self, deployment_id: UUID) -> Any:
        """Read the deployment so its configured pool and queue can be checked."""

    async def create_flow_run_from_deployment(
        self,
        deployment_id: UUID,
        *,
        parameters: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        work_queue_name: str | None = None,
    ) -> Any:
        """Create or replay one deployment flow run."""


def work_pool_blueprint() -> WorkPoolBlueprint:
    """Return the inert dedicated-pool and typed-queue protocol blueprint."""
    queues = tuple(
        QueueBlueprint(name=queue, job_type=job_type)
        for job_type, queue in _QUEUE_FOR_JOB_TYPE.items()
    )
    return WorkPoolBlueprint(
        name=PREFECT_WORK_POOL_NAME,
        work_pool_type=PREFECT_WORK_POOL_TYPE,
        queues=queues,
    )


def _resolve_job_type(value: JobType | str) -> JobType:
    try:
        return JobType(value)
    except ValueError as exc:
        raise JobQueueError(f"unsupported job type: {value!r}") from exc


def queue_for_job_type(job_type: JobType | str) -> WorkQueueName:
    """Return the one fixed queue authorized for a typed job."""
    try:
        resolved = JobType(job_type)
    except ValueError as exc:
        raise JobQueueError(f"unsupported job type: {job_type!r}") from exc
    return _QUEUE_FOR_JOB_TYPE[resolved]


def _identity_fields(job: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in job.items()
        if key not in {"job_id", "job_fingerprint"}
    }


def validate_job_identity(job: Mapping[str, Any]) -> None:
    """Validate a v2 job and recompute its full immutable identity."""
    assert_secret_free(job)
    validate_contract(ContractName.JOB, job)
    if job["schema_version"] != 2:
        raise JobQueueError("the P4.1 queue boundary accepts only v2 jobs")
    fingerprint = artifact_fingerprint(_identity_fields(job))
    if job["job_fingerprint"] != fingerprint:
        raise JobQueueError("job_fingerprint does not match immutable job fields")
    if job["job_id"] != f"job:{fingerprint}":
        raise JobQueueError("job_id does not match the immutable job fingerprint")
    _resolve_job_type(job["job_type"])


def build_job(
    *,
    request_id: str,
    job_type: JobType | str,
    venue_id: str,
    year: int,
    requested_by: str,
    input_artifact_ids: Sequence[str],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one strict v2 job whose ID is derived from all semantics."""
    resolved_type = _resolve_job_type(job_type)
    artifact_ids = tuple(input_artifact_ids)
    if not artifact_ids or len(set(artifact_ids)) != len(artifact_ids):
        raise JobQueueError("jobs require unique, non-empty input artifact IDs")
    job: dict[str, Any] = {
        "schema_version": 2,
        "request_id": request_id,
        "job_type": resolved_type.value,
        "venue_id": venue_id,
        "year": year,
        "requested_by": requested_by,
        "input_artifact_ids": sorted(artifact_ids),
        "payload": deepcopy(dict(payload)),
    }
    fingerprint = artifact_fingerprint(job)
    job["job_id"] = f"job:{fingerprint}"
    job["job_fingerprint"] = fingerprint
    validate_job_identity(job)
    return deepcopy(job)


def build_scrape_job_from_action(action: ActionIntent) -> dict[str, Any]:
    """Convert only an existing P2.5 scraper intent into an immutable job."""
    if not isinstance(action, ActionIntent):
        raise JobQueueError("scrape job construction requires an ActionIntent")
    if (
        action.action_type is not ActionType.QUEUE_EXISTING_SCRAPER
        or not isinstance(action.payload, QueueExistingScraperPayload)
    ):
        raise JobQueueError(
            "only queue_existing_scraper actions can create scrape jobs"
        )
    if action.payload.readiness != "pdf_ready":
        raise JobQueueError("only verified pdf_ready actions can create scrape jobs")
    return build_job(
        request_id=action.action_id,
        job_type=JobType.SCRAPE_EXISTING,
        venue_id=action.venue_id,
        year=action.year,
        requested_by="action_router",
        input_artifact_ids=action.evidence_ids,
        payload={
            "completeness_level": "archival",
            "download_pdfs": True,
            "expected_count": None,
        },
    )


def validate_queue_envelope(envelope: Mapping[str, Any]) -> None:
    """Reject schema, identity, pool, or typed-queue drift before I/O."""
    assert_secret_free(envelope)
    validate_contract(ContractName.JOB_QUEUE_ENVELOPE, envelope)
    job = envelope["job"]
    validate_job_identity(job)
    expected_queue = queue_for_job_type(job["job_type"])
    if envelope["work_pool_name"] != PREFECT_WORK_POOL_NAME:
        raise JobQueueError("queue envelope does not target the dedicated work pool")
    if envelope["work_queue_name"] != expected_queue.value:
        raise JobQueueError("work queue does not match the typed job")


def build_queue_envelope(job: Mapping[str, Any]) -> QueueEnvelope:
    """Bind one immutable v2 job to its fixed pool and queue."""
    validate_job_identity(job)
    envelope = QueueEnvelope(
        work_pool_name=PREFECT_WORK_POOL_NAME,
        work_queue_name=queue_for_job_type(job["job_type"]),
        job=deepcopy(dict(job)),
    )
    validate_queue_envelope(envelope.as_dict())
    return envelope


def _validate_receipt(
    receipt: SubmissionReceipt,
    envelope: Mapping[str, Any],
) -> None:
    if not isinstance(receipt, SubmissionReceipt):
        raise JobQueueError("cloud submitter returned an invalid receipt")
    expected = (
        envelope["job"]["job_id"],
        envelope["work_pool_name"],
        envelope["work_queue_name"],
    )
    actual = (
        receipt.job_id,
        receipt.work_pool_name,
        receipt.work_queue_name,
    )
    try:
        UUID(receipt.flow_run_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise JobQueueError(
            "cloud submission receipt has an invalid flow run ID"
        ) from exc
    if actual != expected:
        raise JobQueueError("cloud submission receipt does not match the queued job")


async def submit_job(
    job: Mapping[str, Any],
    submitter: CloudJobSubmitter,
) -> SubmissionReceipt:
    """Validate and submit one job without changing OpenPapers state."""
    envelope = build_queue_envelope(job).as_dict()
    receipt = await submitter.submit(
        envelope,
        idempotency_key=job["job_id"],
    )
    _validate_receipt(receipt, envelope)
    return receipt


class PrefectDeploymentSubmitter:
    """Prefect 3 deployment adapter over an injected, never-created client."""

    def __init__(
        self,
        client: PrefectDeploymentClient,
        deployment_ids: Mapping[WorkQueueName | str, UUID | str],
    ) -> None:
        self._client = client
        self._deployment_ids: dict[WorkQueueName, UUID] = {}
        for queue, deployment_id in deployment_ids.items():
            try:
                resolved_queue = WorkQueueName(queue)
                resolved_id = UUID(str(deployment_id))
            except (ValueError, TypeError) as exc:
                raise JobQueueError("invalid Prefect queue/deployment mapping") from exc
            self._deployment_ids[resolved_queue] = resolved_id

    async def submit(
        self,
        envelope: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> SubmissionReceipt:
        """Create an idempotent run in the deployment's configured work pool."""
        validate_queue_envelope(envelope)
        job = envelope["job"]
        if idempotency_key != job["job_id"]:
            raise JobQueueError("Prefect idempotency key must equal the job ID")
        queue = WorkQueueName(envelope["work_queue_name"])
        deployment_id = self._deployment_ids.get(queue)
        if deployment_id is None:
            raise JobQueueError(f"no Prefect deployment configured for {queue.value}")
        deployment = await self._client.read_deployment(deployment_id)
        if (
            getattr(deployment, "work_pool_name", None) != PREFECT_WORK_POOL_NAME
            or getattr(deployment, "work_queue_name", None) != queue.value
        ):
            raise JobQueueError(
                "Prefect deployment is not assigned to the required pool and queue"
            )
        flow_run = await self._client.create_flow_run_from_deployment(
            deployment_id,
            parameters={"queue_envelope": deepcopy(dict(envelope))},
            idempotency_key=idempotency_key,
            work_queue_name=queue.value,
        )
        raw_flow_run_id = getattr(flow_run, "id", None)
        try:
            flow_run_id = str(UUID(str(raw_flow_run_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise JobQueueError(
                "Prefect returned a flow run without an ID"
            ) from exc
        return SubmissionReceipt(
            job_id=job["job_id"],
            flow_run_id=flow_run_id,
            work_pool_name=PREFECT_WORK_POOL_NAME,
            work_queue_name=queue.value,
        )
