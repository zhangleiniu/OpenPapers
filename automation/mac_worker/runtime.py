"""Pure fake-job consumer for the P4.2 Mac worker boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from automation.job_queue import validate_queue_envelope


@dataclass(frozen=True)
class FixtureJobObservation:
    """A non-result observation proving that a typed fixture reached the Mac side."""

    status: str
    reason_code: str
    job_id: str
    job_type: str
    venue_id: str
    year: int
    work_pool_name: str
    work_queue_name: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible copy with no result or artifact claim."""
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "job_id": self.job_id,
            "job_type": self.job_type,
            "venue_id": self.venue_id,
            "year": self.year,
            "work_pool_name": self.work_pool_name,
            "work_queue_name": self.work_queue_name,
        }


def simulate_queue_envelope(
    queue_envelope: Mapping[str, Any],
) -> FixtureJobObservation:
    """Validate one P4.1 envelope and observe it without executing any job."""
    envelope = deepcopy(dict(queue_envelope))
    validate_queue_envelope(envelope)
    job = envelope["job"]
    return FixtureJobObservation(
        status="simulated",
        reason_code="fixture_only_no_execution",
        job_id=job["job_id"],
        job_type=job["job_type"],
        venue_id=job["venue_id"],
        year=job["year"],
        work_pool_name=envelope["work_pool_name"],
        work_queue_name=envelope["work_queue_name"],
    )
