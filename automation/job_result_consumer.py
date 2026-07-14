"""Thin P4.4 cloud coordinator for immutable job-result consumption."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from automation.control_state import (
    ControlStateRepository,
    JobResultConsumptionOutcome,
    LeaseHandle,
)
from automation.job_results import StoredObject


class ImmutableResultReader(Protocol):
    """Minimal exact-generation reader used by the cloud consumer."""

    def read_bundle(
        self,
        job: Mapping[str, Any],
    ) -> tuple[StoredObject, StoredObject]:
        """Return the strict manifest and result at observed generations."""


def consume_published_result(
    job: Mapping[str, Any],
    reader: ImmutableResultReader,
    repository: ControlStateRepository,
    *,
    lease: LeaseHandle,
    consumed_at: datetime | str,
) -> JobResultConsumptionOutcome:
    """Read one immutable pair and record exactly one logical consumption."""
    manifest, result = reader.read_bundle(job)
    return repository.consume_job_result(
        job,
        manifest.payload,
        result.payload,
        manifest_name=manifest.name,
        manifest_generation=manifest.generation,
        result_name=result.name,
        result_generation=result.generation,
        lease=lease,
        consumed_at=consumed_at,
    )
