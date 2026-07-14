"""Pure P5.1 registry from immutable typed jobs to approved entry points."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from automation.job_queue import JobType, validate_job_identity


class CommandRegistryError(ValueError):
    """Raised when a typed job has no approved repository command."""


class RepositoryEntryPoint(str, Enum):
    """Fixed repository programs approved for Phase 5 selection."""

    SCRAPER = "main.py"
    VALIDATOR = "postprocessing/validate_year.py"


class DataRootPolicy(str, Enum):
    """Execution-root requirement retained for the later staging executor."""

    ISOLATED_STAGING_REQUIRED = "isolated_staging_required"


@dataclass(frozen=True)
class ApprovedCommandSpec:
    """Inert, job-bound command selection with no interpreter or environment."""

    job_id: str
    job_type: JobType
    entry_point: RepositoryEntryPoint
    arguments: tuple[str, ...]
    data_root_policy: DataRootPolicy = DataRootPolicy.ISOLATED_STAGING_REQUIRED

    def as_dict(self) -> dict[str, Any]:
        """Return a defensive JSON-compatible representation."""
        return {
            "job_id": self.job_id,
            "job_type": self.job_type.value,
            "entry_point": self.entry_point.value,
            "arguments": list(self.arguments),
            "data_root_policy": self.data_root_policy.value,
        }


_FIXED_ENTRY_POINT_FOR_JOB_TYPE: dict[JobType, RepositoryEntryPoint] = {
    JobType.SCRAPE_EXISTING: RepositoryEntryPoint.SCRAPER,
    JobType.VALIDATE_CANDIDATE: RepositoryEntryPoint.VALIDATOR,
}


def _literal_value(value: str, *, field: str) -> str:
    """Reject execution syntax in values that become positional arguments."""
    if (
        not value
        or value.startswith("-")
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or any(marker in value for marker in ("$", "%", "~"))
    ):
        raise CommandRegistryError(f"{field} is not a literal argument value")
    return value


def _scrape_arguments(job: Mapping[str, Any]) -> tuple[str, ...]:
    payload = job["payload"]
    arguments = [
        _literal_value(job["venue_id"], field="venue_id"),
        str(job["year"]),
        "--require-complete",
        "--completeness-level",
        _literal_value(payload["completeness_level"], field="completeness_level"),
    ]
    if not payload["download_pdfs"]:
        arguments.append("--no-pdfs")
    return tuple(arguments)


def _validation_arguments(job: Mapping[str, Any]) -> tuple[str, ...]:
    payload = job["payload"]
    arguments = [
        _literal_value(job["venue_id"], field="venue_id"),
        str(job["year"]),
        "--level",
        _literal_value(payload["completeness_level"], field="completeness_level"),
    ]
    if payload["require_pdfs"]:
        arguments.append("--require-pdfs")
    if payload["expected_count"] is not None:
        arguments.extend(("--expected-count", str(payload["expected_count"])))
    return tuple(arguments)


_ARGUMENT_BUILDER_FOR_JOB_TYPE = {
    JobType.SCRAPE_EXISTING: _scrape_arguments,
    JobType.VALIDATE_CANDIDATE: _validation_arguments,
}


def resolve_approved_command(job: Mapping[str, Any]) -> ApprovedCommandSpec:
    """Resolve one strict v2 job without accepting or executing a command."""
    try:
        candidate = deepcopy(dict(job))
        validate_job_identity(candidate)
        job_type = JobType(candidate["job_type"])
    except (TypeError, ValueError, KeyError) as exc:
        raise CommandRegistryError("job failed approved-command validation") from exc

    entry_point = _FIXED_ENTRY_POINT_FOR_JOB_TYPE.get(job_type)
    argument_builder = _ARGUMENT_BUILDER_FOR_JOB_TYPE.get(job_type)
    if entry_point is None or argument_builder is None:
        raise CommandRegistryError(
            f"job type is not approved for Phase 5: {job_type.value}"
        )

    spec = ApprovedCommandSpec(
        job_id=candidate["job_id"],
        job_type=job_type,
        entry_point=entry_point,
        arguments=argument_builder(candidate),
    )
    return spec
